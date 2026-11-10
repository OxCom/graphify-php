"""Stacked `#[Route]` attributes, the composed `route_path`, and `#[IsGranted]` guard edges.

Symfony's `AttributeClassLoader` reads every Route attribute on a method as a separate route,
takes the class prefix from the FIRST class-level Route only, and, for an invokable controller
with no method routes, turns every class-level Route into a route for `__invoke`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php import resolver  # noqa: E402
from graphify_php.graph_adapter import (  # noqa: E402
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
    ROUTE_RELATION,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax import routes as routes_module  # noqa: E402
from graphify_php.syntax.file_scan import scan_source  # noqa: E402

TEAM_CONTROLLER = r"""<?php
namespace App\Controller\Front\TimeTracking;
use Symfony\Component\Routing\Attribute\Route;
use Symfony\Component\Security\Http\Attribute\IsGranted;
#[Route('/front')]
#[IsGranted('ROLE_USER')]
class TeamController extends AbstractController
{
    #[Route(
        '/teams/time-tracker',
        name: 'time-tracker-teams-dashboard',
        methods: ['GET'],
    )]
    #[Route(
        '/teams/time-tracker/date/{date}',
        name: 'time-tracker-teams-dashboard-date',
        requirements: ['date' => '\d{4}-\d{2}-\d{2}'],
        methods: 'GET',
    )]
    #[IsGranted('ROLE_HR')]
    public function teams() {}
    #[IsGranted(new Expression(self::READ_ROLES), subject: 'team')]
    #[Route(path: '/x', name: 'x')]
    public function x() {}
}
"""

CLASS_FQN = "App\\Controller\\Front\\TimeTracking\\TeamController"


def facts(php: str, path: str = "Controller.php"):
    return scan_source(path, php.encode("utf-8"))


def test_stacked_route_groups_each_become_a_route():
    found = facts(TEAM_CONTROLLER).routes

    by_path = {r.path or "": r for r in found}
    assert sorted(by_path) == [
        "/front/teams/time-tracker",
        "/front/teams/time-tracker/date/{date}",
        "/front/x",
    ]
    assert by_path["/front/teams/time-tracker"].name == "time-tracker-teams-dashboard"
    assert by_path["/front/teams/time-tracker"].methods == ("GET",)
    assert by_path["/front/teams/time-tracker"].controller_method == "teams"
    dated = by_path["/front/teams/time-tracker/date/{date}"]
    assert dated.name == "time-tracker-teams-dashboard-date"
    assert dated.methods == ("GET",)
    assert dated.controller_method == "teams"
    assert by_path["/front/x"].name == "x"
    assert by_path["/front/x"].controller_method == "x"
    assert all(r.controller_class == CLASS_FQN for r in found)
    # Distinct declarations on one method must not collide on one node.
    assert len({r.node_key for r in found}) == 3


def test_two_routes_in_one_attribute_group_both_count():
    found = facts(
        """<?php
class C {
    #[Route('/a'), Route('/b')]
    public function run() {}
}
"""
    ).routes

    assert [r.path for r in found] == ["/a", "/b"]


def test_only_the_first_class_route_is_the_prefix():
    found = facts(
        """<?php
#[Route('/first')]
#[Route('/second')]
class C {
    #[Route('/run')]
    public function run() {}
}
"""
    ).routes

    assert [r.path for r in found] == ["/first/run"]


def test_invokable_class_with_two_class_routes_yields_two_invoke_routes():
    found = facts(
        """<?php
#[Route('/one', name: 'one')]
#[Route('/two', name: 'two')]
class Invokable {
    public function __invoke() {}
}
"""
    ).routes

    assert [(r.path, r.name, r.controller_method) for r in found] == [
        ("/one", "one", "__invoke"),
        ("/two", "two", "__invoke"),
    ]
    assert all(r.class_path is None for r in found)


def test_guards_read_from_class_and_methods():
    guards = facts(TEAM_CONTROLLER).guards

    by_holder = {(g.holder_method, g.label): g for g in guards}
    assert len(guards) == 3

    user = by_holder[(None, "ROLE_USER")]
    assert user.literal is True
    assert user.holder_class == CLASS_FQN
    assert user.node_key == "guard#ROLE_USER"

    hr = by_holder[("teams", "ROLE_HR")]
    assert hr.literal is True
    assert hr.subject == ""

    expression = by_holder[("x", "new Expression(self::READ_ROLES)")]
    assert expression.literal is False
    assert expression.subject == "'team'"
    assert expression.node_key == f"{CLASS_FQN}::guard#new Expression(self::READ_ROLES)"


def test_named_attribute_argument_and_missing_argument():
    guards = facts(
        """<?php
abstract class Base {
    #[IsGranted(attribute: 'ROLE_ADMIN', subject: 'post')]
    public function a() {}
    #[IsGranted]
    public function b() {}
}
"""
    ).guards

    assert [(g.holder_method, g.label, g.literal, g.subject, g.reason) for g in guards] == [
        ("a", "ROLE_ADMIN", True, "'post'", ""),
        ("b", "", False, "", routes_module.GUARD_NO_ATTRIBUTE_ARG),
    ]


def _graph(path: str):
    """graphify's shape: file contains class, class `method` edges to `.name()` nodes."""
    nodes = [
        {"id": "team_file", "label": "TeamController.php", "source_file": path},
        {
            "id": "team_class",
            "label": "TeamController",
            "_callable": True,
            "_callable_class": True,
            "source_file": path,
            "_origin": "ast",
        },
    ]
    edges = [{"source": "team_file", "target": "team_class", "relation": "contains"}]
    for method in ("teams", "x"):
        nodes.append(
            {
                "id": f"team_{method}",
                "label": f".{method}()",
                "_callable": True,
                "source_file": path,
                "_origin": "ast",
            }
        )
        edges.append({"source": "team_class", "target": f"team_{method}", "relation": "method"})
    return nodes, edges


def test_run_routes_publishes_route_path_and_guard_edges(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    php = tmp_path / "TeamController.php"
    php.write_text(TEAM_CONTROLLER, encoding="utf-8")
    path = os.path.realpath(php)
    nodes, edges = _graph(path)

    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource()],
        repo_root=str(tmp_path),
    )

    assert record.state == "completed", record.reason
    routes = [n for n in nodes if n.get("_kind") == ROUTE_NODE_KIND]
    by_path = {n["route_path"]: n for n in routes}
    assert sorted(by_path) == [
        "/front/teams/time-tracker",
        "/front/teams/time-tracker/date/{date}",
        "/front/x",
    ]
    dated = by_path["/front/teams/time-tracker/date/{date}"]
    route_pairs = {(e["source"], e["target"]) for e in edges if e.get("relation") == ROUTE_RELATION}
    assert (dated["id"], "team_teams") in route_pairs

    guard_nodes = {n["id"]: n for n in nodes if n.get("_kind") == GUARD_NODE_KIND}
    guarded = {
        (e["source"], guard_nodes[e["target"]]["label"], e["guard_scope"])
        for e in edges
        if e.get("relation") == GUARD_RELATION
    }
    assert guarded == {
        ("team_teams", "ROLE_HR", "method"),
        ("team_class", "ROLE_USER", "class"),
        ("team_x", "new Expression(self::READ_ROLES)", "method"),
    }
    expression = next(
        n for n in guard_nodes.values() if n["label"] == "new Expression(self::READ_ROLES)"
    )
    assert expression["guard_literal"] is False
    assert expression["guard_subject"] == "'team'"
    assert expression["guard_attribute"] == "IsGranted"
    guard_edge = next(e for e in edges if e.get("relation") == GUARD_RELATION)
    assert guard_edge["_resolver"] == "graphify-php"
    assert guard_edge["context"] == "guard"
    assert guard_edge["_via"] == "attribute"
    assert guard_edge["confidence_score"] == 1.0

    assert record.guard_declarations == 3
    assert record.guard_edges_added == 3
    assert record.edges_added == 3  # route edges only


def test_guard_without_holder_in_graph_adds_no_node(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    php = tmp_path / "TeamController.php"
    php.write_text(TEAM_CONTROLLER, encoding="utf-8")
    path = os.path.realpath(php)
    nodes, edges = [], []

    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource()],
        repo_root=str(tmp_path),
    )

    assert not [n for n in nodes if n.get("_kind") == GUARD_NODE_KIND]
    assert record.guard_edges_added == 0
    assert record.unresolved[resolver.REASON_GUARD_NO_HOLDER] == 3


def test_same_class_constant_path_resolves():
    found = facts(
        """<?php
#[Route(self::BASE)]
class C {
    private const string PATH = '/api/v1/goals';
    const BASE = '/front', OTHER = '/o';
    #[Route(self::PATH . '/{goal}' . '/summary')]
    public function summary() {}
    #[Route(path: self::PATH, name: 'list')]
    public function list() {}
}
"""
    ).routes

    assert [(r.path, r.reason) for r in found] == [
        ("/front/api/v1/goals/{goal}/summary", ""),
        ("/front/api/v1/goals", ""),
    ]


def test_inherited_static_and_non_literal_constants_stay_refused():
    found = facts(
        """<?php
class C extends Base {
    const DYNAMIC = PREFIX . '/x';
    const TWICE = '/a';
    const TWICE = '/b';
    #[Route(self::INHERITED . '/x')]
    public function a() {}
    #[Route(static::TWICE)]
    public function b() {}
    #[Route(self::DYNAMIC)]
    public function c() {}
    #[Route(self::TWICE)]
    public function d() {}
    #[Route(Other::PATH)]
    public function e() {}
}
"""
    ).routes

    assert [(r.controller_method, r.path, r.reason) for r in found] == [
        (method, None, routes_module.PATH_NOT_LITERAL) for method in "abcde"
    ]


def test_bare_constant_prefix_is_refused_not_dropped():
    found = facts(
        """<?php
#[Route(PREFIX)]
class C {
    #[Route('/x')]
    public function x() {}
}
"""
    ).routes

    assert [(r.path, r.method_path, r.reason) for r in found] == [
        (None, "/x", routes_module.PREFIX_NOT_LITERAL)
    ]


def test_trait_self_constant_is_refused_never_the_traits_own():
    # In trait code `self` is the using class, so the trait's own `X` proves nothing.
    found = facts(
        """<?php
trait T {
    const X = '/t';
    #[Route(self::X . '/z')]
    public function z() {}
    #[Route('/literal')]
    public function w() {}
}
"""
    ).routes

    assert [(r.controller_method, r.path, r.reason) for r in found] == [
        ("z", None, routes_module.PATH_NOT_LITERAL),
        ("w", "/literal", ""),
    ]
