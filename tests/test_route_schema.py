"""Route/guard graph contract `route_schema` 2: templates, guard kinds, effective guards.

A route node carries `route_template`, `declared_in`, `effective_guards`, `authorization_state`,
`authorization_reason` and `route_schema`; guard nodes and edges carry `guard_kind`, guard
nodes `guard_key`. Symfony's existing fields and node ids do not change.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php import resolver  # noqa: E402
from graphify_php.frameworks import symfony  # noqa: E402
from graphify_php.frameworks.provider import config_rule_key  # noqa: E402
from graphify_php.graph_adapter import (  # noqa: E402
    CHECK_RELATION,
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
    ROUTE_RELATION,
    GraphRouteSink,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax import routes as routes_module  # noqa: E402
from graphify_php.syntax.routes import (  # noqa: E402
    TEMPLATE_MULTI_SEGMENT,
    TEMPLATE_REQUIREMENT_UNREAD,
    TEMPLATE_UNPARSED,
    RouteDeclaration,
    brace_template,
    requirement_may_match_slash,
)

USES = (
    "use Symfony\\Component\\Routing\\Attribute\\Route; "
    "use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;"
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/a/{x}", ("/a/{x}", "")),
        ("/a/{x?}", ("/a/{x?}", "")),
        ("/a/{x<\\d+>}", ("/a/{x}", "")),
        ("/list/{page<\\d+>?1}", ("/list/{page?}", "")),
        ("/list/{page?1}", ("/list/{page?}", "")),
        ("/a/{!x}", ("/a/{x}", "")),
        ("/blog/{slug:post}", ("/blog/{slug}", "")),
        ("a/{x}", ("/a/{x}", "")),
        # Static text after it: RouteCompiler keeps the placeholder mandatory.
        ("/a/{x?}/edit", ("/a/{x}/edit", "")),
        # Only a separator between two optional placeholders keeps the run going.
        ("/a/{x?}.{_format?json}", ("/a/{x?}.{_format?}", "")),
        ("/a/{x?}-v-{y?}", ("/a/{x}-v-{y?}", "")),
        ("/a/{!x?}", ("/a/{x}", "")),
        ("/a/{x<.+>}", ("", TEMPLATE_MULTI_SEGMENT)),
        ("/a/{x<[^/]+>}", ("/a/{x}", "")),
        ("/a/{x", ("", TEMPLATE_UNPARSED)),
    ],
)
def test_symfony_template_inline(path, expected):
    assert symfony.symfony_template(path) == expected


def test_symfony_template_requirements():
    assert symfony.symfony_template("/f/{path}", {"path": ".+"}) == ("", TEMPLATE_MULTI_SEGMENT)
    assert symfony.symfony_template("/f/{id}", {"id": "\\d+"}) == ("/f/{id}", "")
    # A requirement for a name the path does not have changes nothing.
    assert symfony.symfony_template("/f/{id}", {"other": ".+"}) == ("/f/{id}", "")
    assert symfony.symfony_template("/f/{id}", {"id": None}) == (
        "/f/{id}",
        TEMPLATE_REQUIREMENT_UNREAD,
    )
    assert symfony.symfony_template("/f/{id}", requirements_unread=True) == (
        "/f/{id}",
        TEMPLATE_REQUIREMENT_UNREAD,
    )
    # A multi-segment requirement wins over an unread one.
    assert symfony.symfony_template("/f/{a}/{b}", {"a": None, "b": ".*"}) == (
        "",
        TEMPLATE_MULTI_SEGMENT,
    )
    assert symfony.symfony_template("/l/{page}", defaults=("page",)) == ("/l/{page?}", "")


@pytest.mark.parametrize(
    ("regex", "may_match"),
    [
        ("\\d+", False),
        ("[a-z0-9-]+", False),
        ("[\\w.]+", False),
        ("\\d{4}-\\d{2}-\\d{2}", False),
        ("en|fr", False),
        ("(?:a|b)+", False),
        ("[^/]+", False),
        ("[^/.]++", False),
        (".+", True),
        (".*", True),
        ("\\S+", True),
        ("[^x]+", True),
        ("[!-~]+", True),
        ("/", True),
        ("\\/", True),
        ("\\x2f", True),
        ("\\p{L}+", True),
        ("(?=a)", True),
        ("[a", True),
    ],
)
def test_requirement_may_match_slash(regex, may_match):
    assert requirement_may_match_slash(regex) is may_match


def test_brace_template_shared_helper():
    assert brace_template("users/{id}/{tab?}") == ("/users/{id}/{tab?}", "")
    assert brace_template("/u/{id}", optional=("id",)) == ("/u/{id?}", "")
    assert brace_template("/u/{any}", multi_segment=("any",)) == ("", TEMPLATE_MULTI_SEGMENT)
    assert brace_template("/u/{a-b}") == ("", TEMPLATE_UNPARSED)


def test_attribute_requirements_reach_the_declaration():
    php = f"""<?php {USES}
#[Route('/c', requirements: ['id' => '\\d+'])]
class C
{{
    #[Route('/{{id}}/{{page<\\d+>?1}}')]
    public function a() {{}}
    #[Route('/files/{{path}}', requirements: ['path' => '.+'])]
    public function b() {{}}
    #[Route('/s/{{slug}}', requirements: ['slug' => Requirement::ASCII_SLUG])]
    public function c() {{}}
    #[Route('/m/{{id}}', requirements: ['id' => '.+'])]
    public function d() {{}}
}}
"""
    result = symfony.scan("C.php", php.encode("utf-8"))
    found = {r.controller_method: (r.template, r.template_reason) for r in result.routes}
    assert found == {
        "a": ("/c/{id}/{page?}", ""),
        "b": ("", TEMPLATE_MULTI_SEGMENT),
        "c": ("/c/s/{slug}", TEMPLATE_REQUIREMENT_UNREAD),
        # The method's requirement replaces the class's one for the same name.
        "d": ("", TEMPLATE_MULTI_SEGMENT),
    }
    assert result.reasons == {TEMPLATE_MULTI_SEGMENT: 2, TEMPLATE_REQUIREMENT_UNREAD: 1}


@pytest.mark.parametrize(
    ("roles", "allow_if", "kind"),
    [
        (("IS_AUTHENTICATED_FULLY",), None, "authentication"),
        (("IS_AUTHENTICATED_REMEMBERED", "IS_AUTHENTICATED"), None, "authentication"),
        ((), None, "public"),
        (("PUBLIC_ACCESS",), None, "public"),
        (("ROLE_ADMIN",), None, "authorization"),
        (("ROLE_ADMIN", "ROLE_HR"), None, "authorization"),
        # Any one role lets the request through, so the weakest decides.
        (("ROLE_ADMIN", "IS_AUTHENTICATED_FULLY"), None, "authentication"),
        (("ROLE_ADMIN", "PUBLIC_ACCESS"), None, "public"),
        ((), "is_granted('ROLE_A')", "authorization"),
        ((), 'is_granted("ROLE_A") and request.isMethod("GET")', "authorization"),
        ((), "not is_granted('ROLE_A')", "unknown"),
        ((), "!is_granted('ROLE_A')", "unknown"),
        ((), "is_granted('IS_AUTHENTICATED_FULLY')", "unknown"),
        ((), "is_granted('PUBLIC_ACCESS')", "unknown"),
        ((), "is_granted('ROLE_A') or request.getClientIp() == '127.0.0.1'", "unknown"),
        ((), "not (is_granted('ROLE_A'))", "unknown"),
        ((), "user.isAdmin()", "unknown"),
        (("ROLE_B",), "is_granted('ROLE_A')", "authorization"),
        (("IS_AUTHENTICATED_FULLY",), "is_granted('ROLE_A')", "authentication"),
        (("ROLE_B",), "user.isAdmin()", "unknown"),
        (
            (),
            "not is_granted('ROLE_CONTRACTOR') and is_granted('ROLE_EMPLOYEE') and "
            "(is_granted('ROLE_LEAD') or is_granted('ROLE_HR'))",
            "authorization",
        ),
        ((), "is_granted('ROLE_A') or is_granted('ROLE_B')", "unknown"),
        ((), "(is_granted('ROLE_A'))", "authorization"),
        ((), "is_fully_authenticated() and is_granted('ROLE_A')", "authorization"),
        ((), "is_granted('IS_AUTHENTICATED_FULLY') and is_fully_authenticated()", "unknown"),
        ((), 'is_granted(\'ROLE_A\') && "x" == "or"', "authorization"),
    ],
)
def test_access_rule_kind(roles, allow_if, kind):
    assert symfony.access_rule_kind(roles, allow_if) == kind


@pytest.mark.parametrize(
    ("label", "literal", "kind"),
    [
        ("ROLE_HR", True, "authorization"),
        ("POST_EDIT", True, "authorization"),
        ("IS_AUTHENTICATED_FULLY", True, "authentication"),
        ("PUBLIC_ACCESS", True, "public"),
        ("new Expression(self::READ_ROLES)", False, "unknown"),
        ("self::ROLE", False, "unknown"),
    ],
)
def test_label_kind(label, literal, kind):
    assert symfony.label_kind(label, literal) == kind


def test_config_rule_key():
    assert config_rule_key("acme", "config/auth.php", 3) == "acme:config#config/auth.php:3"


CONTROLLERS = r"""<?php
namespace App\Controller; use Symfony\Component\Routing\Attribute\Route; use Symfony\Component\Security\Http\Attribute\IsGranted;
#[IsGranted('ROLE_ADMIN')]
class AdminController
{
    #[Route('/admin/{id<\d+>}')]
    #[IsGranted('ROLE_EDIT')]
    public function edit() { $this->denyAccessUnlessGranted('ROLE_EDIT'); $this->canEdit(); }
    #[Route('/admin/list/{page<\d+>?1}')]
    public function list() {}
}
class OpenController
{
    #[Route('/pub')]
    public function pub() {}
    #[Route('/me')]
    public function me() {}
    #[Route('/expr')]
    #[IsGranted(new Expression('is_granted("ROLE_A")'))]
    public function expr() {}
    #[Route('/files/{path}', requirements: ['path' => '.+'])]
    public function files() {}
    #[Route('/c/{slug}', requirements: ['slug' => Requirement::ASCII_SLUG])]
    public function slug() {}
    #[Route('/auth')]
    #[IsGranted('IS_AUTHENTICATED_FULLY')]
    public function auth() {}
    #[Route('/api/{id}')]
    public function api() {}
}
"""  # noqa: E501

SECURITY_YAML = """\
security:
    access_control:
        - { path: ^/admin, roles: ROLE_USER }
        - { path: ^/pub, roles: PUBLIC_ACCESS }
        - { path: ^/me, roles: IS_AUTHENTICATED_FULLY }
        - { path: ^/api, roles: ROLE_API, methods: [POST] }
"""

CLASSES = {
    "AdminController": ("edit", "list"),
    "OpenController": ("pub", "me", "expr", "files", "slug", "auth", "api"),
}


def _graph(path: str):
    nodes: list[dict] = [{"id": "file", "label": "Controllers.php", "source_file": path}]
    edges: list[dict] = []
    for class_name, methods in CLASSES.items():
        nodes.append(
            {
                "id": class_name,
                "label": class_name,
                "_callable": True,
                "_callable_class": True,
                "source_file": path,
                "_origin": "ast",
            }
        )
        edges.append({"source": "file", "target": class_name, "relation": "contains"})
        for method in methods:
            nid = f"{class_name}.{method}"
            nodes.append(
                {
                    "id": nid,
                    "label": f".{method}()",
                    "_callable": True,
                    "source_file": path,
                    "_origin": "ast",
                }
            )
            edges.append({"source": class_name, "target": nid, "relation": "method"})
    return nodes, edges


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    monkeypatch.delenv(routes_module.ACCESS_CHECK_PATTERNS_ENV, raising=False)
    php = tmp_path / "Controllers.php"
    php.write_text(CONTROLLERS, encoding="utf-8")
    config = tmp_path / "config" / "packages"
    config.mkdir(parents=True)
    (config / "security.yaml").write_text(SECURITY_YAML, encoding="utf-8")
    path = os.path.realpath(php)
    nodes, edges = _graph(path)
    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource(providers=(symfony.PROVIDER,))],
        repo_root=str(tmp_path),
    )
    assert record.state == "completed", record.reason
    routes = {n["route_path"]: n for n in nodes if n.get("_kind") == ROUTE_NODE_KIND}
    guards = {n["id"]: n for n in nodes if n.get("_kind") == GUARD_NODE_KIND}
    return record, routes, guards, edges, path


def _labels(route: dict, guards: dict) -> list[str]:
    return sorted(guards[g]["label"] for g in route["effective_guards"])


def test_route_node_fields(built):
    _, routes, _, _, path = built
    edit = routes["/admin/{id<\\d+>}"]
    assert edit["route_template"] == "/admin/{id}"
    # graphify relativizes `source_file` after the resolvers; `declared_in` is relative already.
    assert edit["source_file"] == path
    assert edit["declared_in"] == os.path.relpath(path, os.path.realpath(os.path.dirname(path)))
    assert not edit["declared_in"].startswith("/")
    assert edit["route_schema"] == 2
    assert routes["/admin/list/{page<\\d+>?1}"]["route_template"] == "/admin/list/{page?}"
    assert routes["/files/{path}"]["route_template"] == ""
    assert routes["/files/{path}"]["route_template_reason"] == "route-template-multi-segment"
    assert edit["route_template_reason"] == ""
    assert routes["/c/{slug}"]["route_template"] == "/c/{slug}"
    # New fields follow every existing one, in this order.
    assert list(edit)[-8:] == [
        "route_template",
        "route_template_reason",
        "declared_in",
        "effective_guards",
        "authorization_state",
        "authorization_reason",
        "route_schema",
        "handler_kind",
    ]
    assert edit["handler_kind"] == "method"


def assert_handler_kind_invariant(nodes: list[dict], edges: list[dict]) -> None:
    """`handler_kind == "method"` iff a `handled_by` edge leaves the route node."""
    handled = {e["source"] for e in edges if e.get("relation") == ROUTE_RELATION}
    for node in nodes:
        if node.get("_kind") != ROUTE_NODE_KIND:
            continue
        assert node["handler_kind"] in ("method", "closure", "unresolved"), node
        assert (node["handler_kind"] == "method") == (node["id"] in handled), node


def test_handler_kind_method_on_every_bound_route(built):
    _, routes, _, edges, _ = built
    assert {r["handler_kind"] for r in routes.values()} == {"method"}
    assert_handler_kind_invariant(list(routes.values()), edges)


def test_handler_kind_unresolved_when_the_method_node_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    php = tmp_path / "Controllers.php"
    php.write_text(CONTROLLERS, encoding="utf-8")
    path = os.path.realpath(php)
    nodes, edges = _graph(path)
    nodes = [n for n in nodes if n["id"] != "OpenController.api"]
    edges = [e for e in edges if e["target"] != "OpenController.api"]
    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource(providers=(symfony.PROVIDER,))],
        repo_root=str(tmp_path),
    )
    assert record.state == "completed", record.reason
    assert record.unresolved[resolver.REASON_ROUTE_NO_CONTROLLER] == 1
    routes = {n["route_path"]: n for n in nodes if n.get("_kind") == ROUTE_NODE_KIND}
    assert routes["/api/{id}"]["handler_kind"] == "unresolved"
    assert routes["/pub"]["handler_kind"] == "method"
    assert not [
        e
        for e in edges
        if e.get("relation") == ROUTE_RELATION and e["source"] == routes["/api/{id}"]["id"]
    ]
    assert_handler_kind_invariant(nodes, edges)


def test_handler_kind_method_wins_on_a_shared_route_node():
    """Two declarations of one node key: "method" once either one produced the edge."""
    nodes: list[dict] = [{"id": "m", "label": ".a()"}]
    edges: list[dict] = []
    sink = GraphRouteSink(nodes, edges)
    declaration = RouteDeclaration("App\\C", "a", "C.php", 3, path="/a")
    nid = sink.add_route(declaration, None)
    assert nodes[-1]["handler_kind"] == "unresolved"
    assert sink.add_route(declaration, "m") == nid
    assert nodes[-1]["handler_kind"] == "method"
    # A later declaration without the handler does not take "method" back: the edge stays.
    sink.add_route(declaration, None)
    assert nodes[-1]["handler_kind"] == "method"
    assert_handler_kind_invariant(nodes, edges)


def test_effective_guards_union_route_method_class(built):
    _, routes, guards, _, _ = built
    edit = routes["/admin/{id<\\d+>}"]
    assert edit["effective_guards"] == sorted(edit["effective_guards"])
    # access_control, the method's #[IsGranted] and the class's; never the checks_access call.
    assert _labels(edit, guards) == ["ROLE_ADMIN", "ROLE_EDIT", "roles: [ROLE_USER]"]
    assert _labels(routes["/admin/list/{page<\\d+>?1}"], guards) == [
        "ROLE_ADMIN",
        "roles: [ROLE_USER]",
    ]
    assert routes["/files/{path}"]["effective_guards"] == []


def test_authorization_state(built):
    record, routes, _, _, _ = built
    states = {p: (n["authorization_state"], n["authorization_reason"]) for p, n in routes.items()}
    assert states == {
        "/admin/{id<\\d+>}": ("covered", ""),
        "/admin/list/{page<\\d+>?1}": ("covered", ""),
        "/pub": ("not_proven", ""),
        "/me": ("not_proven", ""),
        "/expr": ("undecided", "guard-kind-unknown"),
        "/files/{path}": ("not_proven", ""),
        "/c/{slug}": ("not_proven", ""),
        "/auth": ("not_proven", ""),
        "/api/{id}": ("undecided", "access-control-undecided"),
    }
    assert record.authorization_state == {"covered": 2, "not_proven": 5, "undecided": 2}
    assert record.authorization_reason == {
        "guard-kind-unknown": 1,
        "access-control-undecided": 1,
    }
    # Only an authorization guard makes a handler guarded: /auth's authentication one does not.
    assert record.handlers_total == 9
    assert record.handlers_unguarded == 7


def test_guard_kind_and_key_on_nodes_and_edges(built):
    _, routes, guards, edges, _ = built
    by_label = {n["label"]: n for n in guards.values()}
    assert by_label["ROLE_EDIT"]["guard_kind"] == "authorization"
    assert by_label["ROLE_EDIT"]["guard_key"] == "guard#ROLE_EDIT"
    assert by_label["IS_AUTHENTICATED_FULLY"]["guard_kind"] == "authentication"
    assert by_label["roles: [PUBLIC_ACCESS]"]["guard_kind"] == "public"
    assert by_label["roles: [PUBLIC_ACCESS]"]["guard_key"] == "access_control#1"
    assert by_label["roles: [IS_AUTHENTICATED_FULLY]"]["guard_kind"] == "authentication"
    expression = by_label["new Expression('is_granted(\"ROLE_A\")')"]
    assert expression["guard_kind"] == "unknown"
    assert expression["guard_key"] == (
        "App\\Controller\\OpenController::guard#new Expression('is_granted(\"ROLE_A\")')"
    )
    assert by_label["canEdit"]["guard_kind"] == "unknown"
    assert by_label["canEdit"]["guard_key"] == "App\\Controller\\AdminController::check#canEdit"
    assert list(by_label["ROLE_EDIT"])[-2:] == ["guard_kind", "guard_key"]

    for edge in edges:
        if edge.get("relation") in (GUARD_RELATION, CHECK_RELATION):
            assert edge["guard_kind"] == guards[edge["target"]]["guard_kind"]
            assert list(edge)[-1] == "guard_kind"
    checks = {guards[e["target"]]["label"]: e for e in edges if e["relation"] == CHECK_RELATION}
    assert checks["ROLE_EDIT"]["guard_kind"] == "authorization"
    assert checks["canEdit"]["guard_kind"] == "unknown"


def test_status_record_route_schema_and_template_reasons(built):
    record, *_ = built
    assert record.route_schema == 2
    assert record.unresolved["tree_sitter_routes:route-template-multi-segment"] == 1
    assert record.unresolved["tree_sitter_routes:route-template-requirement-unread"] == 1
    written = resolver.status.read()
    row = next(r for r in written["resolvers"] if r["name"] == "php_routes")
    assert row["route_schema"] == 2
    assert row["authorization_state"] == {"covered": 2, "not_proven": 5, "undecided": 2}


def test_blocked_access_control_makes_routes_undecided(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    php = tmp_path / "Controllers.php"
    php.write_text(CONTROLLERS, encoding="utf-8")
    config = tmp_path / "config" / "packages"
    config.mkdir(parents=True)
    (config / "security.yaml").write_text(SECURITY_YAML, encoding="utf-8")
    routes_dir = tmp_path / "config" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "app.yaml").write_text(
        "app:\n    resource: ../../src/Controller/\n    type: attribute\n    prefix: /v1\n",
        encoding="utf-8",
    )
    path = os.path.realpath(php)
    nodes, edges = _graph(path)
    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource(providers=(symfony.PROVIDER,))],
        repo_root=str(tmp_path),
    )
    routes = {n["route_path"]: n for n in nodes if n.get("_kind") == ROUTE_NODE_KIND}
    assert routes["/pub"]["authorization_state"] == "undecided"
    assert routes["/pub"]["authorization_reason"] == "access-control-blocked"
    # A proved authorization guard still covers the route.
    assert routes["/admin/{id<\\d+>}"]["authorization_state"] == "covered"
    assert record.authorization_reason["access-control-blocked"] == 7


def test_repo_relative_never_returns_a_host_path(tmp_path):
    from graphify_php.graph_adapter import repo_relative

    inside = tmp_path / "src" / "A.php"
    assert repo_relative(str(inside), str(tmp_path)) == os.path.join("src", "A.php")
    assert repo_relative("src/A.php", str(tmp_path)) == os.path.join("src", "A.php")
    assert repo_relative("/elsewhere/B.php", str(tmp_path)) == ""
    assert repo_relative("", str(tmp_path)) == ""
