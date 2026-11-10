"""`checks_access` edges from handler calls, and `guarded_by` edges from `access_control` rules.

A `checks_access` edge is read only from `$this->...` calls written directly in a route
handler's body. An `access_control` edge goes from a route node to a rule only when that rule
is proved to be the first one Symfony applies to every URL of the route.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php import resolver  # noqa: E402
from graphify_php.graph_adapter import (  # noqa: E402
    CHECK_RELATION,
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax import access_control as ac  # noqa: E402
from graphify_php.syntax import routes as routes_module  # noqa: E402
from graphify_php.syntax.file_scan import scan_source  # noqa: E402

CONTROLLERS = r"""<?php
namespace App\Controller;
class HrController
{
    #[Route('/hr')]
    public function hr()
    {
        if ($this->isGranted('ROLE_HR', $team)) {}
        $this->isGranted('ROLE_HR');
        $this->denyAccessUnlessGranted('ROLE_X');
        $this?->canView($u);
        $this->assertTeamAccess();
        $this->isGranted(self::ROLE, $x);
        $fn = function () { $this->isGranted('ROLE_CLOSURE'); };
        $g = fn() => $this->canClosure();
        $o = new class { public function z() { $this->isGranted('ROLE_ANON'); } };
        $other->isGranted('ROLE_OTHER');
        $this->helper();
        $this->isGranted();
        $this->mayAccessThing();
    }
    public function helper() { $this->isGranted('ROLE_HELPER'); $this->canHelp(); }
}
class OtherController
{
    #[IsGranted('ROLE_HR')]
    #[Route('/other')]
    public function show() {}
    #[Route('/open')]
    public function open() {}
    #[Route('/api/{id}')]
    public function api() {}
    #[Route('/free')]
    public function free() {}
}
#[Route(PREFIX)]
class PartialController
{
    #[Route('/x')]
    public function x() {}
}
"""

SECURITY_YAML = """\
security:
    access_control:
        - { path: ^/teams, allow_if: "is_granted('ROLE_A')" }
        - { path: ^/open, roles: ROLE_USER }
        - { path: ^/api, roles: [ROLE_API, ROLE_B], methods: [POST] }
when@test:
    security:
        password_hashers:
            Symfony\\Component\\Security\\Core\\User\\PasswordAuthenticatedUserInterface:
                algorithm: auto
                cost: 4
"""

HR = "App\\Controller\\HrController"


def checks(php: str = CONTROLLERS):
    return scan_source("Controllers.php", php.encode("utf-8")).access_checks


def test_direct_this_calls_in_handler_only():
    found = [(c.check_call, c.label, c.literal, c.subject, c.reason) for c in checks()]

    assert found == [
        ("isGranted", "ROLE_HR", True, "$team", ""),
        ("isGranted", "ROLE_HR", True, "", ""),
        ("denyAccessUnlessGranted", "ROLE_X", True, "", ""),
        ("canView", "canView", False, "", ""),
        ("assertTeamAccess", "assertTeamAccess", False, "", ""),
        ("isGranted", "self::ROLE", False, "$x", ""),
        ("isGranted", "", False, "", routes_module.CHECK_NO_ATTRIBUTE_ARG),
    ]
    assert {c.holder_method for c in checks()} == {"hr"}


def test_node_keys_and_conditional():
    by_call = {}
    for check in checks():
        by_call.setdefault(check.check_call, check)

    assert by_call["isGranted"].node_key == "guard#ROLE_HR"
    assert by_call["isGranted"].conditional is True
    assert by_call["denyAccessUnlessGranted"].conditional is False
    assert by_call["canView"].conditional is None
    assert by_call["canView"].node_key == f"{HR}::check#canView"
    expression = next(c for c in checks() if c.label == "self::ROLE")
    assert expression.node_key == f"{HR}::guard#self::ROLE"


def test_patterns_overridden_by_environment(monkeypatch):
    monkeypatch.setenv(routes_module.ACCESS_CHECK_PATTERNS_ENV, r"^mayAccess, ^assert")

    names = [c.check_call for c in checks()]

    assert "mayAccessThing" in names
    assert "assertTeamAccess" in names
    assert "canView" not in names


def test_invalid_pattern_falls_back_to_defaults_and_is_counted(monkeypatch, tmp_path):
    monkeypatch.setenv(routes_module.ACCESS_CHECK_PATTERNS_ENV, "([")
    php = tmp_path / "Controllers.php"
    php.write_text(CONTROLLERS, encoding="utf-8")

    names = [c.check_call for c in checks()]
    resolution = TreeSitterRouteSource().declarations(str(tmp_path), ["Controllers.php"])

    assert "canView" in names
    assert resolution.unresolved[routes_module.ACCESS_CHECK_PATTERNS_INVALID] == 1


def test_spread_first_argument_is_refused():
    found = checks(
        """<?php
class C {
    #[Route('/c')]
    public function c() { $this->isGranted(...$args); }
}
"""
    )

    assert [c.reason for c in found] == [routes_module.CHECK_SPREAD_ARGUMENT]


def test_invokable_handler_is_read():
    found = checks(
        """<?php
#[Route('/inv')]
class Inv {
    public function __invoke() { $this->denyAccessUnlessGranted('ROLE_I'); }
}
"""
    )

    assert [(c.holder_method, c.label) for c in found] == [("__invoke", "ROLE_I")]


RULES = [
    ac.AccessRule.from_config(i, entry)
    for i, entry in enumerate(
        [
            {"path": "^/$", "roles": []},
            {"path": r"^/teams/time-tracker/\d+", "allow_if": "X"},
            {"path": r"^/teams/appraisals/\d+/users/\d+(/review|/feedback)?$", "allow_if": "Y"},
            {"path": "^/teams", "allow_if": "Z"},
            {"path": "^/api/v1/system/health/ping", "roles": []},
            {"path": "^/api/v1/system", "roles": ["ROLE_SYSTEM"]},
            {"path": "^/api/v1/admin", "roles": ["ROLE_SUPER_ADMIN"]},
        ]
    )
]


def first(template: str):
    rule, reason = ac.first_rule(template, RULES)
    return (rule.index if rule is not None else None), reason


def test_first_rule_on_reference_rules():
    assert first("/teams/time-tracker") == (3, "")
    assert first("/teams/time-tracker/date/{date}") == (3, "")
    assert first("/teams/appraisals/{appraisal}/users/{user}") == (None, ac.REASON_UNDECIDED)
    assert first("/api/v1/system/health/ping") == (4, "")
    assert first("/api/v1/system/{x}") == (None, ac.REASON_UNDECIDED)
    assert first("/api/v1/admin/users/{id}") == (6, "")
    assert first("/") == (0, "")
    assert first("/other") == (None, ac.REASON_NO_RULE)


def test_path_verdict_refuses_what_it_cannot_prove():
    # Unanchored, a container parameter, a PCRE-only escape, a top-level alternation.
    assert ac.path_verdict("/a/{x}", "/a") == ac.UNDECIDED
    assert ac.path_verdict("/a", "^%prefix%") == ac.UNDECIDED
    assert ac.path_verdict("/a", r"^/a\z") == ac.UNDECIDED
    assert ac.path_verdict("/a/{x}", "^/b|^/a") == ac.UNDECIDED
    # A quantifier after the literal applies to its last character.
    assert ac.path_verdict("/team/{x}", "^/teams?") == ac.UNDECIDED
    # An optional digit does not make a non-digit disjoint.
    assert ac.path_verdict("/a/b/{x}", r"^/a/\d*") == ac.UNDECIDED
    # The separator before an optional placeholder may be absent from the URL.
    assert ac.path_verdict("/blog/{page}", "^/blog/") == ac.UNDECIDED
    assert ac.path_verdict("/blog/{page}", "^/blog") == ac.MATCH


def test_rule_with_other_matchers_is_only_ever_disjoint():
    rule = ac.AccessRule.from_config(0, {"path": "^/api", "methods": ["POST"]})

    assert ac.rule_verdict("/api/x", rule) == ac.UNDECIDED
    assert ac.rule_verdict("/web/x", rule) == ac.DISJOINT
    assert ac.rule_verdict("/x", ac.AccessRule.from_config(0, {"roles": "R"})) == ac.UNDECIDED


def test_rule_labels():
    assert ac.AccessRule.from_config(0, {"path": "^/", "roles": ["A", "B"]}).label == (
        "roles: [A, B]"
    )
    assert ac.AccessRule.from_config(0, {"path": "^/"}).label == "roles: []"
    assert ac.AccessRule.from_config(0, {"path": "^/", "allow_if": "x"}).label == "allow_if: x"
    assert ac.AccessRule.from_config(0, {"roles": "A", "allow_if": "x"}).label == (
        "roles: [A]; allow_if: x"
    )


def _graph(path: str):
    """graphify's shape: file contains classes, each with `method` edges to `.name()` nodes."""
    nodes: list[dict] = [{"id": "file", "label": "Controllers.php", "source_file": path}]
    edges: list[dict] = []
    classes = {
        "HrController": ("hr", "helper"),
        "OtherController": ("show", "open", "api", "free"),
        "PartialController": ("x",),
    }
    for class_name, methods in classes.items():
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


def _write(root, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _run(
    tmp_path, monkeypatch, security: str | None = SECURITY_YAML, files=None, php_text=CONTROLLERS
):
    _write(tmp_path, files or {})
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    monkeypatch.delenv(routes_module.ACCESS_CHECK_PATTERNS_ENV, raising=False)
    php = tmp_path / "Controllers.php"
    php.write_text(php_text, encoding="utf-8")
    if security is not None:
        config = tmp_path / "config" / "packages"
        config.mkdir(parents=True, exist_ok=True)
        (config / "security.yaml").write_text(security, encoding="utf-8")
    path = os.path.realpath(php)
    nodes, edges = _graph(path)
    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        nodes,
        edges,
        sources=[TreeSitterRouteSource()],
        repo_root=str(tmp_path),
    )
    return record, nodes, edges


def test_run_routes_publishes_checks_and_access_control(tmp_path, monkeypatch):
    record, nodes, edges = _run(tmp_path, monkeypatch)

    assert record.state == "completed", record.reason
    guards = {n["id"]: n for n in nodes if n.get("_kind") == GUARD_NODE_KIND}
    routes = {n["id"]: n for n in nodes if n.get("_kind") == ROUTE_NODE_KIND}

    check_edges = [e for e in edges if e.get("relation") == CHECK_RELATION]
    by_label = {guards[e["target"]]["label"]: e for e in check_edges}
    assert sorted(by_label) == [
        "ROLE_HR",
        "ROLE_X",
        "assertTeamAccess",
        "canView",
        "self::ROLE",
    ]
    assert all(e["source"] == "HrController.hr" for e in check_edges)
    hr = by_label["ROLE_HR"]
    assert (hr["conditional"], hr["check_call"], hr["guard_attribute"]) == (
        True,
        "isGranted",
        "isGranted",
    )
    assert hr["_via"] == "call"
    assert hr["context"] == "access_check"
    assert hr["_resolver"] == "graphify-php"
    assert by_label["ROLE_X"]["conditional"] is False
    assert by_label["canView"]["conditional"] is None
    assert {label: e["check_kind"] for label, e in by_label.items()} == {
        "ROLE_HR": "framework",
        "ROLE_X": "framework",
        "self::ROLE": "framework",
        "assertTeamAccess": "pattern",
        "canView": "pattern",
    }
    canview_node = guards[by_label["canView"]["target"]]
    assert canview_node["guard_attribute"] == "method"
    assert canview_node["guard_literal"] is False

    # One node for `#[IsGranted('ROLE_HR')]` and `$this->isGranted('ROLE_HR')`.
    attribute_edge = next(
        e for e in edges if e.get("relation") == GUARD_RELATION and e.get("_via") == "attribute"
    )
    assert attribute_edge["source"] == "OtherController.show"
    assert attribute_edge["target"] == hr["target"]
    assert attribute_edge["guard_attribute"] == "IsGranted"
    assert guards[hr["target"]]["guard_attribute"] == "IsGranted"

    control = [e for e in edges if e.get("_via") == "access_control"]
    assert len(control) == 1
    edge = control[0]
    assert routes[edge["source"]]["route_path"] == "/open"
    assert edge["relation"] == GUARD_RELATION
    assert edge["guard_scope"] == "route"
    assert edge["guard_attribute"] == "access_control"
    assert edge["source_file"] == "config/packages/security.yaml"
    rule_node = guards[edge["target"]]
    assert rule_node["label"] == "roles: [ROLE_USER]"
    assert rule_node["access_control_path"] == "^/open"
    assert rule_node["access_control_index"] == 1
    assert rule_node["guard_literal"] is True

    # hr, helper-less: hr, show, open, api, free, x. Unguarded: api, free, x.
    assert record.handlers_total == 6
    # Unchanged under the framework-only rule: hr also has `isGranted` / `denyAccessUnlessGranted`.
    assert record.handlers_unguarded == 3
    # No `guarded_by`: hr (framework check), api, free, x (no check).
    assert record.handlers_check_framework == 1
    assert record.handlers_check_pattern_only == 0
    assert record.handlers_unchecked == 3
    assert record.routes_access_control_undecided == 1
    assert record.checks_access_declarations == 7
    assert record.checks_access_edges_added == 5
    assert record.access_control_edges_added == 1
    assert record.unresolved[ac.REASON_UNDECIDED] == 1
    assert record.unresolved[ac.REASON_NO_RULE] == 3  # /hr, /other, /free
    assert record.unresolved[ac.REASON_NO_PATH] == 1  # /x under a non-literal prefix


def test_missing_security_yaml_adds_nothing_and_fails_nothing(tmp_path, monkeypatch):
    record, _, edges = _run(tmp_path, monkeypatch, security=None)

    assert record.state == "completed", record.reason
    assert not [e for e in edges if e.get("_via") == "access_control"]
    assert record.access_control_edges_added == 0
    assert record.handlers_unguarded == 4  # open is no longer covered
    assert record.handlers_check_framework == 1  # hr
    assert record.handlers_check_pattern_only == 0
    assert record.handlers_unchecked == 4  # open, api, free, x
    assert not any(reason.startswith("access-control") for reason in record.unresolved)


PATTERN_ONLY = r"""<?php
namespace App\Controller;
class OtherController
{
    #[Route('/free')]
    public function free() { $this->canRetry(); }
    #[Route('/open')]
    public function open() {}
}
"""


def test_pattern_only_check_leaves_handler_unguarded(tmp_path, monkeypatch):
    record, nodes, edges = _run(tmp_path, monkeypatch, security=None, php_text=PATTERN_ONLY)

    assert record.state == "completed", record.reason
    check_edges = [e for e in edges if e.get("relation") == CHECK_RELATION]
    assert [(e["source"], e["check_call"], e["check_kind"]) for e in check_edges] == [
        ("OtherController.free", "canRetry", "pattern")
    ]
    assert record.handlers_total == 2
    assert record.handlers_check_framework == 0
    assert record.handlers_check_pattern_only == 1  # free
    assert record.handlers_unchecked == 1  # open
    assert record.handlers_unguarded == 2


# cem.hub's routing config, near-verbatim: every prefixed import is a bundle resource, and the
# attribute imports carry no prefix.
HUB_ROUTING = {
    "config/routes.yaml": """\
controllers:
    resource: ../src/Controller/
    type: attribute
    exclude:
        - '../src/Controller/Rest/QA'
        - '../src/Controller/Rest/QA/**/*'
""",
    "config/routes/framework.yaml": """\
when@dev:
    _errors:
        resource: '@FrameworkBundle/Resources/config/routing/errors.php'
        prefix: /_error
""",
    "config/routes/pentatrion_vite.yaml": """\
when@dev:
    _pentatrion_vite:
        prefix: /build
        resource: "@PentatrionViteBundle/Resources/config/routing.yaml"
    _profiler_vite:
        path: /_profiler/vite
        defaults:
            _controller: Pentatrion\\ViteBundle\\Controller\\ProfilerController::info
""",
    "config/routes/empty.yaml": "# nothing here yet\n",
    "config/routes/qa_api.php": """\
<?php
use Symfony\\Component\\Routing\\Loader\\Configurator\\RoutingConfigurator;
return static function (RoutingConfigurator $routes): void {
    $routes->import('../../src/Controller/Rest/QA/', 'attribute');
    $routes->import('../../src/Controller/Front/QA/', 'attribute');
};
""",
    "config/packages/framework.yaml": "framework:\n    secret: '%env(APP_SECRET)%'\n",
}

ROUTES_6 = 6  # hr, show, open, api, free, x


def test_hub_shaped_routing_and_security_still_give_edges(tmp_path, monkeypatch):
    assert ac.config_blocker(str(tmp_path)) == ""
    record, _, edges = _run(tmp_path, monkeypatch, files=HUB_ROUTING)

    assert record.state == "completed", record.reason
    assert record.access_control_edges_added == 1
    assert len([e for e in edges if e.get("_via") == "access_control"]) == 1
    assert ac.REASON_ROUTE_PREFIX_UNREAD not in record.unresolved
    assert ac.REASON_OVERRIDDEN_ELSEWHERE not in record.unresolved


PREFIX_HITS = {
    "attribute-import": {
        "config/routes/api.yaml": (
            "api: {resource: ../src/Controller/Api/, type: attribute, prefix: /api}\n"
        )
    },
    "annotation-in-when": {
        "config/routes.yaml": (
            "when@prod:\n  c: {resource: ../src/Controller/, type: annotation, prefix: /p}\n"
        )
    },
    "directory-no-type": {"config/routes.yaml": "c: {resource: ../src/Ctl/, prefix: /p}\n"},
    "project-php-file": {"config/routes.yaml": "c: {resource: ../src/R.php, prefix: /p}\n"},
    "unparseable-yaml": {"config/routes/broken.yaml": "a: [unclosed\n"},
    "php-prefix-call": {
        "config/routes/x.php": "<?php $routes->import('../src/C/', 'attribute')->prefix('/a');\n"
    },
    "xml-prefix-attribute": {
        "config/routes.xml": '<routes><import resource="../src/C/" prefix="/a"/></routes>\n'
    },
}


def test_route_prefix_refuses_every_access_control_edge(tmp_path, monkeypatch):
    for name, files in PREFIX_HITS.items():
        root = tmp_path / name
        root.mkdir()
        _write(root, {**HUB_ROUTING, **files})
        assert ac.config_blocker(str(root)) == ac.REASON_ROUTE_PREFIX_UNREAD, name

    record, _, edges = _run(tmp_path, monkeypatch, files=PREFIX_HITS["attribute-import"])

    assert record.state == "completed", record.reason
    assert record.access_control_edges_added == 0
    assert not [e for e in edges if e.get("_via") == "access_control"]
    assert record.unresolved[ac.REASON_ROUTE_PREFIX_UNREAD] == ROUTES_6
    assert ac.REASON_NO_RULE not in record.unresolved


OVERRIDE_HITS = {
    "when-in-security-yaml": {
        "config/packages/security.yaml": SECURITY_YAML
        + "when@prod:\n    security:\n        access_control:\n            - { path: ^/ }\n"
    },
    "per-env-file": {
        "config/packages/prod/security.yaml": (
            "security:\n    access_control:\n        - { path: ^/, roles: R }\n"
        )
    },
    "when-in-other-file": {
        "config/packages/extra.yml": "when@dev:\n    security:\n        access_control: []\n"
    },
    "security-php": {
        "config/packages/security.php": "<?php $security->accessControl()->path('^/');\n"
    },
    "security-xml": {"config/packages/security.xml": "<config><access_control/></config>\n"},
    "unparseable-mentioning": {"config/packages/bad.yaml": "access_control: [unclosed\n"},
}


def test_access_control_declared_elsewhere_refuses_every_edge(tmp_path, monkeypatch):
    for name, files in OVERRIDE_HITS.items():
        root = tmp_path / name
        root.mkdir()
        _write(root, {"config/packages/security.yaml": SECURITY_YAML})
        _write(root, files)
        assert ac.config_blocker(str(root)) == ac.REASON_OVERRIDDEN_ELSEWHERE, name

    # An unparseable file that never names access_control proves nothing either way.
    quiet = tmp_path / "quiet"
    _write(quiet, {"config/packages/bad.yaml": "framework: [unclosed\n"})
    assert ac.config_blocker(str(quiet)) == ""

    record, _, edges = _run(
        tmp_path,
        monkeypatch,
        security=OVERRIDE_HITS["when-in-security-yaml"]["config/packages/security.yaml"],
    )

    assert record.state == "completed", record.reason
    assert record.access_control_edges_added == 0
    assert not [e for e in edges if e.get("_via") == "access_control"]
    assert record.unresolved[ac.REASON_OVERRIDDEN_ELSEWHERE] == ROUTES_6
