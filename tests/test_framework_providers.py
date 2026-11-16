"""The framework provider seam: detection, strict attribute matching, and a second provider.

The fake `acme` provider below is what a phase-2 provider looks like: it is written against
`frameworks.provider` and the `syntax.routes` helpers only, and nothing shared is edited for it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php import resolver  # noqa: E402
from graphify_php.frameworks import REGISTRY, symfony  # noqa: E402
from graphify_php.frameworks.detect import (  # noqa: E402
    REASON_NONE,
    REASON_NONE_BELOW_ROOT,
    select_providers,
)
from graphify_php.frameworks.matching import ATTRIBUTE_PARENT_UNKNOWN  # noqa: E402
from graphify_php.frameworks.provider import (  # noqa: E402
    FrameworkProvider,
    ProviderContext,
    ProviderResult,
    RouteGuard,
)
from graphify_php.graph_adapter import (  # noqa: E402
    CHECK_RELATION,
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
    ROUTE_RELATION,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax import nodes as syntax_nodes  # noqa: E402
from graphify_php.syntax.routes import (  # noqa: E402
    GUARD_KIND_MIDDLEWARE,
    STATIC_RECEIVER,
    THIS_RECEIVER,
    AccessGuard,
    GrantCall,
    RouteDeclaration,
    arguments,
    class_constant_name,
    collect_access_checks,
    string_value,
)


def routes_of(php: str, extra: dict[str, str] | None = None):
    sources = {"C.php": php.encode("utf-8")}
    for path, text in (extra or {}).items():
        sources[path] = text.encode("utf-8")
    return symfony.PROVIDER.collect(ProviderContext.from_sources(sources))


# --- strict FQCN matching ------------------------------------------------------------------


def test_aliased_import_matches():
    result = routes_of(
        "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route as R;\n"
        "class C { #[R('/a')] public function a() {} }\n"
    )
    assert [r.path for r in result.routes] == ["/a"]


@pytest.mark.parametrize(
    "use",
    [
        "use Symfony\\Component\\Routing\\Attribute\\{Route};",
        "use Symfony\\Component\\{Routing\\Attribute\\Route as Rt};",
    ],
    ids=["grouped", "grouped-nested-alias"],
)
def test_grouped_use_matches(use):
    name = "Rt" if " as Rt" in use else "Route"
    result = routes_of(
        f"<?php namespace App; {use}\nclass C {{ #[{name}('/g')] public function g() {{}} }}\n"
    )
    assert [r.path for r in result.routes] == ["/g"]


def test_grouped_function_import_binds_no_class():
    result = routes_of(
        "<?php namespace App; use function Symfony\\Component\\Routing\\Attribute\\{Route};\n"
        "class C { #[Route('/f')] public function f() {} }\n"
    )
    assert result.routes == []


def test_fully_qualified_attribute_needs_no_import():
    result = routes_of(
        "<?php namespace App;\n"
        "class C {\n"
        "#[\\Symfony\\Component\\Routing\\Attribute\\Route('/q')] public function q() {}\n"
        "}\n"
    )
    assert [r.path for r in result.routes] == ["/q"]


def test_unimported_short_name_is_not_a_route_and_is_counted():
    result = routes_of(
        "<?php namespace App;\nclass C { #[Route('/x')] #[Other] public function x() {} }\n"
    )
    assert result.routes == []
    assert result.attributes_short_name_only == 1
    assert result.attributes_mapped == 0


def test_attribute_name_and_alias_fold_case():
    result = routes_of(
        "<?php namespace App;\n"
        "use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;\n"
        "class C { #[isGranted('ROLE_X')] public function e() {} }\n"
    )
    assert [(g.label, g.holder_method, g.guard_attribute) for g in result.guards] == [
        ("ROLE_X", "e", "IsGranted")
    ]


def test_repo_subclass_of_route_matches():
    attribute = (
        "<?php namespace App\\Routing; use Symfony\\Component\\Routing\\Attribute\\Route;\n"
        "class ApiRoute extends Route {}\n"
    )
    result = routes_of(
        "<?php namespace App; use App\\Routing\\ApiRoute;\n"
        "class C { #[ApiRoute('/api')] public function a() {} }\n",
        {"ApiRoute.php": attribute},
    )
    assert [r.path for r in result.routes] == ["/api"]


def test_unknown_vendor_parent_is_refused_with_a_reason():
    attribute = (
        "<?php namespace App\\Routing; use Vendor\\Lib\\BaseRoute;\n"
        "class ApiRoute extends BaseRoute {}\n"
    )
    result = routes_of(
        "<?php namespace App; use App\\Routing\\ApiRoute;\n"
        "class C { #[ApiRoute('/api')] public function a() {} }\n",
        {"ApiRoute.php": attribute},
    )
    assert result.routes == []
    assert result.reasons == {ATTRIBUTE_PARENT_UNKNOWN: 1}


# --- detection -----------------------------------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def test_direct_require_selects_symfony(tmp_path):
    _write(tmp_path, {"composer.json": json.dumps({"require": {"symfony/security-bundle": "*"}})})
    assert select_providers(str(tmp_path), REGISTRY) == ((symfony.PROVIDER,), "")


def test_require_dev_counts(tmp_path):
    _write(tmp_path, {"composer.json": json.dumps({"require-dev": {"symfony/routing": "*"}})})
    assert select_providers(str(tmp_path), REGISTRY)[0] == (symfony.PROVIDER,)


def test_lock_is_not_read(tmp_path):
    lock = {"packages": [{"name": "symfony/routing"}, {"name": "symfony/http-kernel"}]}
    _write(
        tmp_path,
        {
            "composer.json": json.dumps({"require": {"laravel/framework": "^11"}}),
            "composer.lock": json.dumps(lock),
        },
    )
    selected, reason = select_providers(str(tmp_path), REGISTRY)
    assert [p.name for p in selected] == ["laravel"]
    assert reason == ""


def test_no_manifest_anywhere(tmp_path):
    assert select_providers(str(tmp_path), REGISTRY) == ((), REASON_NONE)


def test_manifest_below_root_only(tmp_path):
    _write(tmp_path, {"app/backend/composer.json": "{}", "vendor/x/composer.json": "{}"})
    assert select_providers(str(tmp_path), REGISTRY) == ((), REASON_NONE_BELOW_ROOT)


def test_manifest_below_depth_three_is_not_seen(tmp_path):
    _write(tmp_path, {"a/b/c/d/composer.json": "{}"})
    assert select_providers(str(tmp_path), REGISTRY) == ((), REASON_NONE)


# --- the pass ------------------------------------------------------------------------------


def _graph(classes: list[tuple[str, str, list[str]]]):
    """graphify's shape for each `(file, class FQN, methods)`."""
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for path, fqcn, methods in classes:
        short = fqcn.rsplit("\\", 1)[-1]
        cid = f"c_{short}"
        all_nodes.append({"id": f"f_{short}", "label": os.path.basename(path), "source_file": path})
        all_nodes.append(
            {
                "id": cid,
                "label": short,
                "_callable": True,
                "_callable_class": True,
                "source_file": path,
                "_origin": "ast",
            }
        )
        all_edges.append({"source": f"f_{short}", "target": cid, "relation": "contains"})
        for method in methods:
            all_nodes.append(
                {
                    "id": f"{cid}_{method}",
                    "label": f".{method}()",
                    "_callable": True,
                    "source_file": path,
                    "_origin": "ast",
                }
            )
            all_edges.append({"source": cid, "target": f"{cid}_{method}", "relation": "method"})
    return all_nodes, all_edges


def _run(tmp_path, monkeypatch, files: dict[str, str], classes, providers=None):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    _write(tmp_path, files)
    paths = {name: os.path.realpath(tmp_path / name) for name in files if name.endswith(".php")}
    all_nodes, all_edges = _graph([(paths[f], fqcn, ms) for f, fqcn, ms in classes])
    record = resolver.run_routes(
        [{"nodes": [{"source_file": p}]} for p in paths.values()],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource(providers)],
        repo_root=str(tmp_path),
    )
    return record, all_nodes, all_edges


SYMFONY_CONTROLLER = (
    "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route;\n"
    "class SymCtl {\n"
    "    #[Route('/sym')]\n"
    "    public function show() { $this->isGranted('ROLE_S'); $this->allows('acme'); }\n"
    "}\n"
)


def test_a_framework_the_manifest_does_not_require_publishes_nothing(tmp_path, monkeypatch):
    record, all_nodes, all_edges = _run(
        tmp_path,
        monkeypatch,
        {
            "composer.json": json.dumps({"require": {"laravel/framework": "^11"}}),
            "SymCtl.php": SYMFONY_CONTROLLER,
        },
        [("SymCtl.php", "App\\SymCtl", ["show"])],
    )
    # Laravel is selected; Symfony is not, so the Symfony attribute publishes nothing.
    assert record.providers == ["laravel"]
    assert not [e for e in all_edges if e.get("relation") == ROUTE_RELATION]
    assert not [n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND]


def test_status_names_providers_and_counts_attributes(tmp_path, monkeypatch):
    record, all_nodes, all_edges = _run(
        tmp_path,
        monkeypatch,
        {
            "composer.json": json.dumps({"require": {"symfony/framework-bundle": "*"}}),
            "SymCtl.php": SYMFONY_CONTROLLER.replace(
                "class SymCtl", "#[Route] #[Foo]\nclass SymCtl"
            ),
        },
        [("SymCtl.php", "App\\SymCtl", ["show"])],
    )
    assert record.state == "completed", record.reason
    assert record.providers == ["symfony"]
    assert (record.attributes_total, record.attributes_mapped) == (3, 2)
    route = next(n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND)
    assert route["provider"] == "symfony"
    handled = next(e for e in all_edges if e.get("relation") == ROUTE_RELATION)
    assert (handled["_via"], handled["provider"]) == ("attribute", "symfony")


# --- a second provider, written against the seam only --------------------------------------

ACME_GATE = "Acme\\Auth\\Gate"


class AcmeProvider:
    """Routes from `routes.php` calls `$r->get('/path', [Ctl::class, 'method'])`."""

    name = "acme"
    packages = ("acme/framework",)
    grants = {
        (THIS_RECEIVER, "allows"): GrantCall(conditional=True, label=(0, None)),
        (STATIC_RECEIVER + ACME_GATE, "authorize"): GrantCall(conditional=False, label=(0, None)),
    }

    def collect(self, ctx: ProviderContext) -> ProviderResult:
        out = ProviderResult()
        parsed = ctx.parse("routes.php")
        if parsed is None:
            return out
        handlers: dict[str, set] = {}
        for call in _calls(parsed.root, "get"):
            positional, _ = arguments(call)
            path = string_value(positional[0]) if positional else None
            target = positional[1] if len(positional) > 1 else None
            parts = [c for c in target.named_children] if target is not None else []
            values = [c.named_children[0] for c in parts if c.named_children]
            if path is None or len(values) != 2:
                out.count("acme-action-unreadable")
                continue
            fqcn = class_constant_name(values[0], parsed.names)
            method = string_value(values[1])
            files = ctx.class_files(fqcn or "")
            if fqcn is None or method is None or len(files) != 1:
                out.count("acme-handler-unproved")
                continue
            line = syntax_nodes.line_span(call)[0]
            route = RouteDeclaration(
                fqcn, method, "routes.php", line, path=path, method_path=path,
                provider=self.name, via="route_file", handler_file=files[0],
            )  # fmt: skip
            out.routes.append(route)
            handlers.setdefault(files[0], set()).add((fqcn, method))
            out.route_guards.append(
                RouteGuard(
                    route_key=route.node_key,
                    node_key=f"guard#{self.name}:{GUARD_KIND_MIDDLEWARE}:auth",
                    label="auth",
                    guard_attribute="middleware",
                    via="middleware",
                    source_file="routes.php",
                    line=line,
                    provider=self.name,
                    guard_kind=GUARD_KIND_MIDDLEWARE,
                )
            )
        for path, pairs in handlers.items():
            handler = ctx.parse(path)
            if handler is not None:
                out.checks.extend(
                    collect_access_checks(
                        handler.root, handler.names, path, pairs, self.grants, self.name
                    )
                )
        return out


def _calls(node, name: str) -> list:
    found = []
    if node.type == "member_call_expression":
        called = node.child_by_field_name("name")
        if called is not None and syntax_nodes.node_text(called) == name:
            found.append(node)
    for child in node.children:
        found.extend(_calls(child, name))
    return found


ACME_ROUTES = (
    "<?php use App\\AcmeCtl;\n"
    "$r->get('/acme/{id}', [AcmeCtl::class, 'show']);\n"
    "$r->get('/gone', [Missing::class, 'x']);\n"
)
ACME_CONTROLLER = (
    "<?php namespace App; use Acme\\Auth\\Gate;\n"
    "class AcmeCtl {\n"
    "    public function show() {\n"
    "        Gate::authorize('edit-post');\n"
    "        $this->allows('view');\n"
    "        $this->isGranted('ROLE_SYMFONY_ONLY');\n"
    "    }\n"
    "}\n"
)


def test_acme_is_a_provider():
    assert isinstance(AcmeProvider(), FrameworkProvider)


def test_second_provider_publishes_route_guard_and_checks(tmp_path, monkeypatch):
    record, all_nodes, all_edges = _run(
        tmp_path,
        monkeypatch,
        {"routes.php": ACME_ROUTES, "AcmeCtl.php": ACME_CONTROLLER},
        [("AcmeCtl.php", "App\\AcmeCtl", ["show"])],
        providers=(AcmeProvider(),),
    )
    assert record.state == "completed", record.reason
    route = next(n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND)
    assert (route["label"], route["provider"]) == ("/acme/{id}", "acme")
    handled = [e for e in all_edges if e.get("relation") == ROUTE_RELATION]
    assert [(e["target"], e["_via"]) for e in handled] == [("c_AcmeCtl_show", "route_file")]

    guards = {n["id"]: n for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    guarded = [e for e in all_edges if e.get("relation") == GUARD_RELATION]
    assert [(e["source"], e["_via"], e["guard_scope"], e["provider"]) for e in guarded] == [
        (route["id"], "middleware", "route", "acme")
    ]
    assert guards[guarded[0]["target"]]["label"] == "auth"

    checks = [e for e in all_edges if e.get("relation") == CHECK_RELATION]
    assert sorted((e["check_call"], e["conditional"], e["provider"]) for e in checks) == [
        ("Gate::authorize", False, "acme"),
        ("allows", True, "acme"),
    ]
    assert record.unresolved == {"tree_sitter_routes:acme-handler-unproved": 1}
    # A middleware guard proves no authorization: the handler counts as framework-checked.
    assert (record.handlers_total, record.handlers_check_framework) == (1, 1)


def test_two_providers_never_share_grant_calls(tmp_path, monkeypatch):
    record, all_nodes, all_edges = _run(
        tmp_path,
        monkeypatch,
        {
            "routes.php": ACME_ROUTES,
            "AcmeCtl.php": ACME_CONTROLLER,
            "SymCtl.php": SYMFONY_CONTROLLER,
        },
        [("AcmeCtl.php", "App\\AcmeCtl", ["show"]), ("SymCtl.php", "App\\SymCtl", ["show"])],
        providers=(symfony.PROVIDER, AcmeProvider()),
    )
    assert record.state == "completed", record.reason
    labels = {n["id"]: n["label"] for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    checks = {
        (e["source"], labels[e["target"]], e["provider"])
        for e in all_edges
        if e.get("relation") == CHECK_RELATION
    }
    assert checks == {
        ("c_SymCtl_show", "ROLE_S", "symfony"),
        ("c_AcmeCtl_show", "edit-post", "acme"),
        ("c_AcmeCtl_show", "view", "acme"),
    }


def test_guard_kind_scopes_new_provider_keys_only():
    acme = AccessGuard("A", None, "f", 1, "auth", True, provider="acme", guard_kind="middleware")
    sym = AccessGuard("A", None, "f", 1, "auth", True, provider="symfony")
    assert acme.node_key == "guard#acme:middleware:auth"
    assert sym.node_key == "guard#auth"
    source = AccessGuard("A", None, "f", 1, "$x", False, provider="acme", guard_kind="unknown")
    assert source.node_key == "A::guard#acme:unknown:$x"
