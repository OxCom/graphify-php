"""The Laravel provider: routes files, middleware guards, controller middleware, grant calls.

Every test passes `laravel.PROVIDER` explicitly, so a test does not depend on detection.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php import resolver  # noqa: E402
from graphify_php.frameworks import laravel, symfony  # noqa: E402
from graphify_php.frameworks.detect import select_providers  # noqa: E402
from graphify_php.frameworks.provider import (  # noqa: E402
    FrameworkProvider,
    ProviderContext,
    file_reader,
)
from graphify_php.graph_adapter import (  # noqa: E402
    CHECK_RELATION,
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
    ROUTE_RELATION,
    _route_nid,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax.routes import (  # noqa: E402
    TEMPLATE_MULTI_SEGMENT,
    TEMPLATE_REQUIREMENT_UNREAD,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frameworks" / "laravel"
NS = "App\\Http\\Controllers\\"

BOOT = (
    "<?php return Application::configure(basePath: dirname(__DIR__))->withRouting(\n"
    "    web: __DIR__.'/../routes/web.php',\n"
    "    api: __DIR__.'/../routes/api.php',\n"
    ")->create();\n"
)
HEAD = (
    "<?php\nuse Illuminate\\Support\\Facades\\Route;\n"
    "use App\\Http\\Controllers\\C;\nuse App\\Http\\Controllers\\D;\n"
)
CONTROLLERS = {
    "app/Http/Controllers/C.php": (
        "<?php namespace App\\Http\\Controllers;\n"
        "class C { public function a() {} public function b() {} public function __invoke() {} }\n"
    ),
    "app/Http/Controllers/D.php": (
        "<?php namespace App\\Http\\Controllers;\nclass D { public function a() {} }\n"
    ),
}


def collect(web: str = "", api: str = "", extra: dict | None = None, boot: str | None = BOOT):
    sources = dict(CONTROLLERS)
    if boot is not None:
        sources["bootstrap/app.php"] = boot
    # Both files exist, as in a fresh application, so `withRouting` never names a missing one.
    sources["routes/web.php"] = HEAD + web
    sources["routes/api.php"] = HEAD + api
    sources.update(extra or {})
    encoded = {k: v.encode("utf-8") for k, v in sources.items()}
    return laravel.PROVIDER.collect(ProviderContext.from_sources(encoded))


def table(result) -> list[tuple]:
    return [
        (r.methods, r.path, r.controller_class.removeprefix(NS), r.controller_method, r.name)
        for r in result.routes
    ]


def route_guards(result) -> dict[str, list[tuple[str, str]]]:
    """Route path -> its guards as `(kind, label)`, without the file-level `web` / `api` group."""
    paths = {r.node_key: r.path for r in result.routes}
    found: dict[str, list[tuple[str, str]]] = {}
    for guard in result.route_guards:
        if guard.label in ("web", "api") and guard.source_file.startswith("bootstrap/"):
            continue
        found.setdefault(paths[guard.route_key], []).append((guard.guard_kind, guard.label))
    return found


def test_is_a_provider():
    assert isinstance(laravel.PROVIDER, FrameworkProvider)
    assert (laravel.PROVIDER.name, laravel.PROVIDER.packages) == ("laravel", ("laravel/framework",))


# --- the fixture application ---------------------------------------------------------------


def fixture_result():
    root = str(FIXTURE)
    corpus = tuple(
        sorted(
            os.path.relpath(os.path.join(d, f), root)
            for d, _, files in os.walk(os.path.join(root, "app"))
            for f in files
        )
    )
    # The routes files and bootstrap/app.php are NOT in the corpus: they are read anyway.
    return laravel.PROVIDER.collect(ProviderContext(root, corpus, file_reader(root)))


def test_fixture_routes():
    get, post, delete = ("GET", "HEAD"), ("POST",), ("DELETE",)
    assert table(fixture_result()) == [
        # `fn () => view('welcome')`: a closure route, with no class and no method.
        (get, "/", "", "", None),
        (get, "/users/{user}", "UserController", "show", "users.show"),
        (post, "/users", "UserController", "store", None),
        (get, "/dashboard", "ShowDashboard", "__invoke", None),
        (("PUT",), "/users/{user}", "UserController", "update", None),
        (get, "/admin/users", "UserController", "index", "admin.users"),
        (get, "/admin/reports/{id}", "UserController", "report", "admin."),
        (delete, "/admin/users/{user}", "UserController", "destroy", "admin.users.destroy"),
        (get, "/posts/{post}", "PostController", "show", None),
        (post, "/posts", "PostController", "store", None),
        (("GET", "POST", "HEAD"), "/legacy/login", "UserController", "login", "legacy.login"),
        (get, "/photos", "PhotoController", "index", "photos.index"),
        (get, "/photos/{photo}", "PhotoController", "show", "photos.show"),
        # docs-check laravel B.7: a route-level `->prefix()` lands in front of the group prefix.
        (get, "/reports", "ReportController", "index", "reports.index"),
        (get, "/v2/reports/export", "ReportController", "export", "reports.export"),
        (get, "/reports/ping", "ReportController", "ping", "reports."),
        # docs-check laravel B.4: the fixture lock is 13.x, so `any` includes QUERY.
        (laravel.ALL_VERBS_13, "/hook", "ReportController", "hook", None),
        (get, "/api/user", "Api\\TokenController", "user", None),
        (get, "/api/tokens", "Api\\TokenController", "index", "tokens.index"),
        (post, "/api/tokens", "Api\\TokenController", "store", "tokens.store"),
        (get, "/api/tokens/{token}", "Api\\TokenController", "show", "tokens.show"),
        (delete, "/api/tokens/{token}", "Api\\TokenController", "destroy", "tokens.destroy"),
        (post, "/api/tokens/{token}/revoke", "Api\\TokenController", "revoke", None),
    ]


def test_fixture_reasons():
    assert fixture_result().reasons == {
        # docs-check laravel A.2: `health: '/up'` in bootstrap/app.php is counted, not published.
        laravel.REASON_HEALTH_ROUTE: 1,
        # docs-check laravel C.10: `#[WithoutMiddleware('auth')]` on `ping` voids its class guard.
        laravel.REASON_CONTROLLER_MIDDLEWARE_EXCLUDED: 1,
        laravel.REASON_CLOSURE_ACTION: 1,
        laravel.REASON_STRING_ACTION_SHORT: 1,
        laravel.REASON_PATH_NOT_LITERAL: 1,
        TEMPLATE_MULTI_SEGMENT: 1,
        laravel.REASON_RESOURCE_PARAMETER_UNPROVED: 1,
        laravel.REASON_IN_CONTROL_FLOW: 1,
        laravel.REASON_NO_CONTROLLER: 1,
        laravel.REASON_METHOD_UNKNOWN: 1,
        laravel.REASON_FILE_NOT_LOADED: 1,
        laravel.REASON_CONTROLLER_MIDDLEWARE_CLOSURE: 1,
    }


def test_fixture_route_guards():
    result = fixture_result()
    assert route_guards(result) == {
        "/users/{user}": [("authentication", "auth"), ("authorization", "update")],
        "/users": [("authentication", "auth"), ("middleware", "throttle:6,1")],
        "/dashboard": [("authentication", "verified")],
        "/admin/users": [("authentication", "auth")],
        "/admin/users/{user}": [("authentication", "auth")],
        "/posts": [("authorization", "create")],
        "/legacy/login": [("authentication", "guest")],
        "/api/user": [("authentication", "auth:sanctum")],
        "/api/tokens/{token}/revoke": [("middleware", "signed")],
    }
    groups = {(g.label, g.guard_kind) for g in result.route_guards if g.label in ("web", "api")}
    assert groups == {("web", "middleware"), ("api", "middleware")}
    assert all(g.via == "middleware" and g.provider == "laravel" for g in result.route_guards)
    can = next(g for g in result.route_guards if g.label == "create")
    assert (can.node_key, can.subject) == (
        "guard#laravel:authorization:create",
        "App\\Models\\Post",
    )


def test_fixture_controller_middleware_and_checks():
    result = fixture_result()
    post = [g for g in result.guards if g.holder_class == NS + "PostController"]
    assert sorted((g.holder_method, g.guard_kind, g.label) for g in post) == [
        ("show", "authentication", "auth"),
        ("show", "middleware", "log"),
        ("store", "authentication", "auth"),
        ("store", "authorization", "edit-posts"),
    ]
    assert all(g.via == "middleware" for g in post)
    # docs-check laravel C.10: 13.x attributes on ReportController. `ping` has none left: its
    # `#[WithoutMiddleware('auth')]` matched the class `auth`, so its class guards are void.
    report = [g for g in result.guards if g.holder_class == NS + "ReportController"]
    assert sorted((g.holder_method, g.guard_kind, g.label, g.guard_attribute) for g in report) == [
        ("export", "authentication", "auth", "Middleware"),
        ("export", "authorization", "export", "Authorize"),
        ("hook", "authentication", "auth", "Middleware"),
        ("index", "authentication", "auth", "Middleware"),
        ("index", "authorization", "view-reports", "Authorize"),
    ]
    assert {g.via for g in report} == {"attribute"}
    export = next(g for g in report if g.label == "export")
    assert (export.node_key, export.subject) == (
        "guard#laravel:authorization:export",
        "App\\Models\\Report",
    )
    assert [(c.holder_method, c.check_call, c.label, c.conditional) for c in result.checks] == [
        ("show", "authorize", "view", False),
        ("update", "Gate::authorize", "update", False),
        ("update", "Gate::allows", "admin", True),
    ]


# --- the pass, end to end ------------------------------------------------------------------


def _graph(classes):
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for path, fqcn, methods in classes:
        short = fqcn.rsplit("\\", 1)[-1]
        cid = f"c_{short}"
        all_nodes.append({"id": f"f_{short}", "label": os.path.basename(path), "source_file": path})
        all_nodes.append(
            {"id": cid, "label": short, "_callable": True, "_callable_class": True,
             "source_file": path, "_origin": "ast"}
        )  # fmt: skip
        all_edges.append({"source": f"f_{short}", "target": cid, "relation": "contains"})
        for method in methods:
            all_nodes.append(
                {"id": f"{cid}_{method}", "label": f".{method}()", "_callable": True,
                 "source_file": path, "_origin": "ast"}
            )  # fmt: skip
            all_edges.append({"source": cid, "target": f"{cid}_{method}", "relation": "method"})
    return all_nodes, all_edges


def test_fixture_publishes_through_the_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    app = tmp_path / "app"
    shutil.copytree(FIXTURE, app)
    controllers = app / "app" / "Http" / "Controllers"
    user = os.path.realpath(controllers / "UserController.php")
    post = os.path.realpath(controllers / "PostController.php")
    # The base class is in the corpus: from v13.5.0 a parent's attributes apply, so an unread
    # parent leaves the handlers' route guards unproved (Routing/Route.php:1244-1270).
    base = os.path.realpath(controllers / "Controller.php")
    all_nodes, all_edges = _graph(
        [
            (user, NS + "UserController",
             ["show", "store", "index", "update", "report", "login", "destroy"]),
            (post, NS + "PostController", ["show", "store"]),
        ]
    )  # fmt: skip
    record = resolver.run_routes(
        [{"nodes": [{"source_file": p}]} for p in (user, post, base)],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource((laravel.PROVIDER,))],
        repo_root=str(app),
    )
    assert record.state == "completed", record.reason
    assert record.providers == ["laravel"]
    routes = {n["id"]: n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND}
    handled = [e for e in all_edges if e.get("relation") == ROUTE_RELATION]
    # 9 of the 18 routes are served by the two classes in this graph.
    assert len(handled) == 9
    assert {(e["_via"], e["provider"]) for e in handled} == {("route_file", "laravel")}
    labels = {n["id"]: n["label"] for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    guarded = {
        (routes[e["source"]]["label"], labels[e["target"]], e["guard_scope"])
        for e in all_edges
        if e.get("relation") == GUARD_RELATION and e["source"] in routes
    }
    assert ("/admin/reports/{id}", "auth", "route") not in guarded
    assert ("/admin/reports/{id}", "web", "route") in guarded
    assert ("/posts", "create", "route") in guarded
    checks = sorted(
        (labels[e["target"]], e["conditional"])
        for e in all_edges
        if e.get("relation") == CHECK_RELATION
    )
    assert checks == [("admin", True), ("update", False), ("view", False)]
    assert record.unresolved["tree_sitter_routes:laravel-closure-action"] == 1


CLOSURE_WEB = (
    "Route::get('/admin', function () { return 1; })->middleware('auth')->can('admin');\n"
    "Route::get('/open', fn () => 1);\n"
    "Route::get('/c', [C::class, 'a']);\n"
)


def test_closure_routes_publish_without_a_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    files = dict(CONTROLLERS)
    files.update(
        {
            "composer.json": json.dumps({"require": {"laravel/framework": "^12"}}),
            "bootstrap/app.php": BOOT,
            "routes/web.php": HEAD + CLOSURE_WEB,
            "routes/api.php": HEAD,
        }
    )
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    c_path = os.path.realpath(tmp_path / "app/Http/Controllers/C.php")
    all_nodes, all_edges = _graph([(c_path, NS + "C", ["a"])])
    record = resolver.run_routes(
        [{"nodes": [{"source_file": c_path}]}],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource((laravel.PROVIDER,))],
        repo_root=str(tmp_path),
    )
    assert record.state == "completed", record.reason
    routes = {n["label"]: n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND}
    assert {label: r["handler_kind"] for label, r in routes.items()} == {
        "/admin": "closure",
        "/open": "closure",
        "/c": "method",
    }
    # Keyed by the declaring routes file and line, never by a class or a method.
    line = HEAD.count("\n") + 1
    assert routes["/admin"]["id"] == _route_nid(f"laravel:closure:routes/web.php:{line}#/admin")
    handled = {e["source"] for e in all_edges if e.get("relation") == ROUTE_RELATION}
    assert handled == {routes["/c"]["id"]}
    for node in routes.values():
        assert (node["handler_kind"] == "method") == (node["id"] in handled)
    labels = {n["id"]: n["label"] for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    admin = {
        (labels[e["target"]], e["guard_scope"])
        for e in all_edges
        if e.get("relation") == GUARD_RELATION and e["source"] == routes["/admin"]["id"]
    }
    assert {("auth", "route"), ("admin", "route")} <= admin
    assert routes["/admin"]["authorization_state"] == "covered"
    assert routes["/open"]["authorization_state"] == "not_proven"
    assert record.unresolved["tree_sitter_routes:laravel-closure-action"] == 2
    assert resolver.REASON_ROUTE_NO_CONTROLLER not in record.unresolved


# --- detection -----------------------------------------------------------------------------


def test_symfony_attributes_in_a_laravel_app_yield_nothing(tmp_path, monkeypatch):
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^12"}}), encoding="utf-8"
    )
    controller = (
        "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route;\n"
        "class SymCtl { #[Route('/sym')] public function show() {} }\n"
    )
    (tmp_path / "SymCtl.php").write_text(controller, encoding="utf-8")
    chosen, why = select_providers(str(tmp_path), (symfony.PROVIDER, laravel.PROVIDER))
    assert (chosen, why) == ((laravel.PROVIDER,), "")
    ctx = ProviderContext(str(tmp_path), ("SymCtl.php",), file_reader(str(tmp_path)))
    result = laravel.PROVIDER.collect(ctx)
    assert (result.routes, result.guards, result.route_guards, result.checks) == ([], [], [], [])
    assert result.reasons == {laravel.REASON_BOOTSTRAP_UNREAD: 1}

    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    path = os.path.realpath(tmp_path / "SymCtl.php")
    all_nodes, all_edges = _graph([(path, "App\\SymCtl", ["show"])])
    record = resolver.run_routes(
        [{"nodes": [{"source_file": path}]}],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource(chosen)],
        repo_root=str(tmp_path),
    )
    assert record.providers == ["laravel"]
    assert not [n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND]
    assert not [e for e in all_edges if e.get("relation") == ROUTE_RELATION]


# --- action forms --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected", "reason"),
    [
        ("[C::class, 'a']", ("C", "a"), None),
        ("'App\\Http\\Controllers\\C@a'", ("C", "a"), None),
        ("'\\App\\Http\\Controllers\\C@b'", ("C", "b"), None),
        ("'App\\\\Http\\\\Controllers\\\\C@a'", ("C", "a"), None),
        ("C::class", ("C", "__invoke"), None),
        # A `use` line does not apply to a string: Laravel looks up the global `C`.
        ("'C@a'", None, laravel.REASON_STRING_ACTION_SHORT),
        # Published with no class and no method, and still counted.
        ("function () { return 1; }", ("", ""), laravel.REASON_CLOSURE_ACTION),
        ("fn () => 1", ("", ""), laravel.REASON_CLOSURE_ACTION),
        ("'a'", None, laravel.REASON_ACTION_UNREADABLE),
        ("[C::class]", None, laravel.REASON_ACTION_UNREADABLE),
        ("[$c, 'a']", None, laravel.REASON_ACTION_UNREADABLE),
        ("$action", None, laravel.REASON_ACTION_UNREADABLE),
        ("[self::class, 'a']", None, laravel.REASON_ACTION_UNREADABLE),
    ],
)
def test_action_forms(action, expected, reason):
    result = collect(web=f"Route::get('/x', {action});\n")
    if expected is None:
        assert result.routes == []
        assert result.reasons == {reason: 1}
        return
    assert [(r.controller_class.removeprefix(NS), r.controller_method) for r in result.routes] == [
        expected
    ]
    if reason == laravel.REASON_CLOSURE_ACTION:
        assert result.routes[0].handler_kind == "closure"
        assert result.routes[0].handler_file == "routes/web.php"
        assert result.routes[0].node_key == "laravel:closure:routes/web.php:5#/x"
        assert result.reasons == {reason: 1}
        return
    assert result.routes[0].handler_kind == "method"
    assert result.routes[0].handler_file == f"app/Http/Controllers/{expected[0]}.php"
    assert result.reasons == {}


def test_controller_group_takes_bare_method_names():
    result = collect(
        web="Route::controller(C::class)->prefix('c')->group(function () {\n"
        "    Route::get('/one', 'a');\n"
        "    Route::get('/two', [D::class, 'a']);\n"
        "    Route::get('/three', 'App\\Http\\Controllers\\D@a');\n"
        "});\n"
    )
    assert [(r.path, r.controller_class.removeprefix(NS), r.controller_method)
            for r in result.routes] == [
        ("/c/one", "C", "a"), ("/c/two", "D", "a"), ("/c/three", "D", "a"),
    ]  # fmt: skip


def test_namespace_group_prepends_to_string_actions():
    # docs-check laravel B.5, B.6: `prependGroupNamespace` and `RouteGroup::formatNamespace`.
    # Replaces the blanket `laravel-string-action-namespace-group` refusal.
    result = collect(
        web="Route::namespace('App\\Http')->group(function () {\n"
        "    Route::get('/s', 'Controllers\\C@a');\n"
        "    Route::get('/full', 'App\\Http\\Controllers\\C@b');\n"
        "    Route::get('/root', '\\App\\Http\\Controllers\\D@a');\n"
        "    Route::get('/arr', [C::class, 'a']);\n"
        "    Route::namespace('Controllers')->group(function () {\n"
        "        Route::get('/nested', 'C@a');\n"
        "    });\n"
        "    Route::namespace('\\App\\Http\\Controllers')->group(function () {\n"
        "        Route::get('/reset', 'D@a');\n"
        "    });\n"
        "});\n"
        "Route::namespace('Admin')->group(function () {\n"
        "    Route::get('/inv', C::class);\n"
        "});\n"
    )
    assert [(r.path, r.controller_class, r.controller_method) for r in result.routes] == [
        ("/s", NS + "C", "a"),
        ("/full", NS + "C", "b"),
        ("/root", NS + "D", "a"),
        ("/arr", NS + "C", "a"),
        ("/nested", NS + "C", "a"),
        ("/reset", NS + "D", "a"),
        # A `Foo::class` action is a string to the router, so the namespace applies to it too.
        ("/inv", "Admin\\" + NS + "C", "__invoke"),
    ]
    assert result.reasons == {}


@pytest.mark.parametrize(
    ("web", "reason"),
    [
        # docs-check laravel B.5: a `use` import never applies to a string action.
        ("Route::get('/x', 'C@a');", laravel.REASON_STRING_ACTION_SHORT),
        # docs-check laravel B.5, B.6: a non-literal group namespace.
        ("Route::namespace($ns)->group(function () { Route::get('/x', 'C@a'); });",
         laravel.REASON_NAMESPACE_NOT_LITERAL),
        ("Route::group(['namespace' => $ns], function () {\n"
         "    Route::namespace('Inner')->group(function () { Route::get('/x', 'C@a'); });\n"
         "});", laravel.REASON_NAMESPACE_NOT_LITERAL),
        # A registrar `namespace` straight before a verb is not a group.
        ("Route::namespace('App')->get('/x', 'Http\\Controllers\\C@a');",
         laravel.REASON_STRING_ACTION_NAMESPACE),
    ],
)  # fmt: skip
def test_string_action_refusals(web, reason):
    result = collect(web=web + "\n")
    assert result.routes == []
    assert set(result.reasons) == {reason}


def test_non_literal_namespace_leaves_array_actions_alone():
    # docs-check laravel B.5: `Router::convertToControllerAction` only rewrites string actions.
    result = collect(
        web="Route::namespace($ns)->group(function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "});\n"
    )
    assert [r.path for r in result.routes] == ["/a"]
    assert result.reasons == {laravel.REASON_NAMESPACE_NOT_LITERAL: 1}


def test_inner_controller_group_overrides_the_outer_one():
    # docs-check laravel B.6: `RouteGroup::merge` drops the old `controller`; no refusal.
    result = collect(
        web="Route::controller(C::class)->group(function () {\n"
        "    Route::controller(D::class)->group(function () {\n"
        "        Route::get('/inner', 'a');\n"
        "    });\n"
        "    Route::get('/outer', 'b');\n"
        "});\n"
    )
    assert [(r.path, r.controller_class.removeprefix(NS), r.controller_method)
            for r in result.routes] == [("/inner", "D", "a"), ("/outer", "C", "b")]  # fmt: skip
    assert result.reasons == {}


@pytest.mark.parametrize(
    "action",
    [
        # docs-check laravel B.6: `prependGroupController` skips a string that `class_exists`.
        "'App\\Http\\Controllers\\D'",
        "D::class",
    ],
)
def test_controller_group_refuses_class_strings(action):
    result = collect(
        web=f"Route::controller(C::class)->group(function () {{ Route::get('/x', {action}); }});\n"
    )
    assert result.routes == []
    assert result.reasons == {laravel.REASON_CONTROLLER_GROUP_CLASS: 1}


def test_controller_and_namespace_groups_combine():
    # docs-check laravel B.6: the group controller is prepended first, then the namespace.
    result = collect(
        web="Route::namespace('Admin')->group(function () {\n"
        "    Route::controller('Http\\C')->group(function () { Route::get('/x', 'a'); });\n"
        "});\n"
    )
    assert [(r.controller_class, r.controller_method) for r in result.routes] == [
        ("Admin\\Http\\C", "a")
    ]


@pytest.mark.parametrize(
    ("call", "methods"),
    [
        ("get", ("GET", "HEAD")),
        ("post", ("POST",)),
        ("put", ("PUT",)),
        ("patch", ("PATCH",)),
        ("delete", ("DELETE",)),
        ("options", ("OPTIONS",)),
        ("any", laravel.ALL_VERBS),
    ],
)
def test_verbs(call, methods):
    result = collect(web=f"Route::{call}('/v', [C::class, 'a']);\n")
    assert [r.methods for r in result.routes] == [methods]


def test_match_reads_literal_verbs_only():
    result = collect(
        web="Route::match(['put', 'patch'], '/m', [C::class, 'a']);\n"
        "Route::match($verbs, '/n', [C::class, 'a']);\n"
    )
    assert [(r.methods, r.path) for r in result.routes] == [(("PUT", "PATCH"), "/m")]
    assert result.reasons == {laravel.REASON_VERBS_NOT_LITERAL: 1}


def test_match_lists_each_verb_once():
    result = collect(web="Route::match(['get', 'GET', 'post', 'get'], '/m', [C::class, 'a']);\n")
    assert [r.methods for r in result.routes] == [("GET", "POST", "HEAD")]


def lock(version: str) -> dict:
    """A `composer.lock` naming `laravel/framework` at `version`, for `collect(extra=...)`."""
    packages = [{"name": "symfony/routing", "version": "v7.3.0"},
                {"name": "laravel/framework", "version": version}]  # fmt: skip
    return {"composer.lock": json.dumps({"packages": packages, "packages-dev": []})}


@pytest.mark.parametrize(
    ("extra", "methods", "reasons"),
    [
        # `any()` is `self::$verbs` (Routing/Router.php:244); QUERY is in it
        # (Router.php:136) from v13.35.0 (CHANGELOG.md:243), not before.
        (lock("v13.35.0"), laravel.ALL_VERBS_13, {}),
        (lock("v13.34.2"), laravel.ALL_VERBS, {}),
        (lock("v13.1.0"), laravel.ALL_VERBS, {}),
        (lock("v12.40.2"), laravel.ALL_VERBS, {}),
        # A branch alias has no minor: QUERY unproved, the version counted once.
        (lock("13.x-dev"), laravel.ALL_VERBS, {laravel.REASON_VERSION_UNREAD: 1}),
        # Unknown version: the 12.x list, counted once for both routes.
        ({}, laravel.ALL_VERBS, {laravel.REASON_VERSION_UNREAD: 1}),
        (lock("dev-master"), laravel.ALL_VERBS, {laravel.REASON_VERSION_UNREAD: 1}),
        ({"composer.lock": "{not json"}, laravel.ALL_VERBS, {laravel.REASON_VERSION_UNREAD: 1}),
    ],
)
def test_any_follows_the_installed_version(extra, methods, reasons):
    result = collect(
        web="Route::any('/v', [C::class, 'a']);\nRoute::any('/w', [C::class, 'b']);\n",
        extra=extra,
    )
    assert [r.methods for r in result.routes] == [methods, methods]
    assert result.reasons == reasons
    assert "QUERY" not in laravel.ALL_VERBS


def test_version_is_not_counted_where_it_does_not_matter():
    # docs-check laravel B.4: only `any`, `query` and controller attributes read the version.
    assert collect(web="Route::get('/v', [C::class, 'a']);\n").reasons == {}


@pytest.mark.parametrize(
    ("extra", "methods", "reasons"),
    [
        # `Router::query()` (Routing/Router.php:230) exists from v13.35.0 (CHANGELOG.md:243).
        (lock("v13.35.0"), [("QUERY",)], {}),
        (lock("v13.34.0"), [], {laravel.REASON_METHOD_UNKNOWN: 1}),
        (lock("v13.0.0"), [], {laravel.REASON_METHOD_UNKNOWN: 1}),
        (lock("v12.0.0"), [], {laravel.REASON_METHOD_UNKNOWN: 1}),
        ({}, [], {laravel.REASON_METHOD_UNKNOWN: 1, laravel.REASON_VERSION_UNREAD: 1}),
    ],
)
def test_query_verb(extra, methods, reasons):
    result = collect(web="Route::query('/q', [C::class, 'a']);\n", extra=extra)
    assert [r.methods for r in result.routes] == methods
    assert result.reasons == reasons


@pytest.mark.parametrize(
    ("lock_text", "major"),
    [
        (json.dumps({"packages": [{"name": "laravel/framework", "version": "v11.9.0"}]}), 11),
        (json.dumps({"packages": [{"name": "laravel/framework", "version": "13.0"}]}), 13),
        (json.dumps({"packages-dev": [{"name": "laravel/framework", "version": "v13.0.0"}]}),
         None),
        (json.dumps({"packages": [{"name": "laravel/lumen-framework", "version": "v10.0.0"}]}),
         None),
        (json.dumps({"packages": [{"name": "laravel/framework"}]}), None),
        (json.dumps([]), None),
    ],
)  # fmt: skip
def test_installed_major(lock_text, major):
    # docs-check laravel B.4: `packages` only, version only.
    assert laravel.installed_major(lock_text.encode()) == major
    assert laravel.installed_major(None) is None


def test_redirect_and_view_routes_are_counted_not_published():
    # docs-check laravel B.4: they route to the framework's RedirectController / ViewController.
    result = collect(
        web="Route::redirect('/a', '/b');\nRoute::permanentRedirect('/c', '/d');\n"
        "Route::view('/e', 'welcome');\n"
    )
    assert result.routes == []
    assert result.reasons == {laravel.REASON_NO_CONTROLLER: 3}


def test_route_level_prefix_is_applied_in_written_order():
    # docs-check laravel B.7: `Route::prefix`, `rtrim(p, '/') . '/' . ltrim(uri, '/')`, on the
    # URI that already holds the group prefix.
    result = collect(
        web="Route::get('/b', [C::class, 'a'])->prefix('v1')->prefix('v2/');\n"
        "Route::prefix('g')->group(function () {\n"
        "    Route::get('b', [C::class, 'b'])->prefix('/v1/');\n"
        "    Route::get('/', [D::class, 'a'])->prefix('v1');\n"
        "});\n"
    )
    assert [(r.path, r.method_path) for r in result.routes] == [
        ("/v2/v1/b", "/b"),
        ("/v1/g/b", "b"),
        ("/v1/g", "/"),
    ]
    assert result.reasons == {}


def test_non_literal_route_level_prefix_refuses_the_route():
    result = collect(web="Route::get('/b', [C::class, 'a'])->prefix($p);\n")
    assert result.routes == []
    assert result.reasons == {laravel.REASON_PREFIX_NOT_LITERAL: 1}


def test_route_level_name_appends():
    # docs-check laravel B.7: `Route::name()` appends to the existing `as`, the group's included.
    result = collect(
        web="Route::name('admin.')->group(function () {\n"
        "    Route::get('/a', [C::class, 'a'])->name('users.')->name('index');\n"
        "});\n"
        "Route::get('/b', [C::class, 'b'])->name('one')->name('.two');\n"
    )
    assert [r.name for r in result.routes] == ["admin.users.index", "one.two"]


# --- groups --------------------------------------------------------------------------------


def test_nested_groups_concatenate_prefix_and_name():
    result = collect(
        web="Route::prefix('/admin/')->name('admin.')->group(function () {\n"
        "    Route::group(['prefix' => 'users/', 'as' => 'users.'], function () {\n"
        "        Route::get('{id}', [C::class, 'a'])->name('show');\n"
        "        Route::get('/', [C::class, 'b'])->name('index');\n"
        "    });\n"
        "    Route::get('', [D::class, 'a']);\n"
        "});\n"
    )
    assert [(r.path, r.name, r.method_path) for r in result.routes] == [
        ("/admin/users/{id}", "admin.users.show", "{id}"),
        ("/admin/users", "admin.users.index", "/"),
        ("/admin", "admin.", ""),
    ]


def test_registrar_before_a_verb():
    result = collect(web="Route::prefix('p')->middleware('auth')->get('/x', [C::class, 'a']);\n")
    assert [r.path for r in result.routes] == ["/p/x"]
    assert route_guards(result) == {"/p/x": [("authentication", "auth")]}


def test_non_literal_prefix_refuses_every_nested_route():
    result = collect(
        web="Route::prefix($p)->group(function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "    Route::get('/b', [C::class, 'b']);\n"
        "});\n"
    )
    assert result.routes == []
    assert result.reasons == {laravel.REASON_PREFIX_NOT_LITERAL: 3}


def test_group_middleware_is_inherited_and_without_middleware_removes_it():
    result = collect(
        web="Route::middleware(['auth', 'throttle:api'])->group(function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "    Route::get('/b', [C::class, 'b'])->withoutMiddleware('throttle:api');\n"
        "    Route::withoutMiddleware(['auth'])->group(function () {\n"
        "        Route::get('/c', [D::class, 'a']);\n"
        "    });\n"
        "});\n"
    )
    assert route_guards(result) == {
        "/a": [("authentication", "auth"), ("middleware", "throttle:api")],
        "/b": [("authentication", "auth")],
        "/c": [("middleware", "throttle:api")],
    }
    assert result.reasons == {}


def test_unmatched_without_middleware_drops_every_route_guard():
    # `Authenticate::class` and the `auth` alias may resolve to one class: nothing is proved.
    result = collect(
        web="Route::get('/a', [C::class, 'a'])->middleware('auth')->withoutMiddleware(X::class);\n"
    )
    assert [r.path for r in result.routes] == ["/a"]
    assert [g for g in result.route_guards if g.route_key == result.routes[0].node_key] == []
    assert result.reasons == {laravel.REASON_WITHOUT_UNMATCHED: 1}


def test_unknown_group_attribute_skips_the_group():
    result = collect(
        web="Route::group(['prefix' => 'x', 'weird' => 1], function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "});\n"
        "Route::can('x')->group(function () { Route::get('/b', [C::class, 'a']); });\n"
    )
    assert result.routes == []
    assert result.reasons == {
        laravel.REASON_GROUP_ATTRIBUTE_UNKNOWN: 1,
        laravel.REASON_METHOD_UNKNOWN: 1,
    }


def test_array_group_keys_after_aliasing():
    # docs-check laravel B.6: `as`, `scope_bindings`, `excluded_middleware` are the array keys.
    result = collect(
        web="Route::middleware(['auth', 'log'])->group(function () {\n"
        "    Route::group(['as' => 'g.', 'scope_bindings' => true,\n"
        "                  'excluded_middleware' => ['log']], function () {\n"
        "        Route::get('/a', [C::class, 'a'])->name('a');\n"
        "    });\n"
        "});\n"
    )
    assert [(r.path, r.name) for r in result.routes] == [("/a", "g.a")]
    assert route_guards(result) == {"/a": [("authentication", "auth")]}
    assert result.reasons == {}


def test_raw_without_middleware_array_key_is_unknown():
    # docs-check laravel B.6: Laravel reads only `excluded_middleware`; the fluent name is not
    # an array key, so the group is refused rather than read as a removal.
    result = collect(
        web="Route::group(['withoutMiddleware' => ['auth']], function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "});\n"
    )
    assert result.routes == []
    assert result.reasons == {laravel.REASON_GROUP_ATTRIBUTE_UNKNOWN: 1}


def test_group_callback_file_and_require_are_followed():
    result = collect(
        web="Route::prefix('admin')->group(base_path('routes/admin.php'));\n"
        "Route::prefix('more')->group(function () { require __DIR__.'/more.php'; });\n",
        extra={
            "routes/admin.php": HEAD + "Route::get('/a', [C::class, 'a']);\n",
            "routes/more.php": HEAD + "Route::get('/m', [C::class, 'b']);\n",
            "routes/never.php": HEAD + "Route::get('/n', [C::class, 'b']);\n",
        },
    )
    assert [(r.path, r.source_file) for r in result.routes] == [
        ("/admin/a", "routes/admin.php"),
        ("/more/m", "routes/more.php"),
    ]
    assert result.reasons == {laravel.REASON_FILE_NOT_LOADED: 1}


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("if ($x) { Route::get('/a', [C::class, 'a']); }", laravel.REASON_IN_CONTROL_FLOW),
        (
            "foreach ($xs as $x) { Route::get($x, [C::class, 'a']); }",
            laravel.REASON_IN_CONTROL_FLOW,
        ),
        ("Route::get('/a/' . $x, [C::class, 'a']);", laravel.REASON_PATH_NOT_LITERAL),
        ("Route::get(\"/a/$x\", [C::class, 'a']);", laravel.REASON_PATH_NOT_LITERAL),
        ("Route::myMacro('/a');", laravel.REASON_METHOD_UNKNOWN),
        ("Route::get('/a', [C::class, 'a'])->myMacro();", laravel.REASON_MODIFIER_UNKNOWN),
        ("Route::redirect('/a', '/b');", laravel.REASON_NO_CONTROLLER),
        ("Route::view('/a', 'welcome');", laravel.REASON_NO_CONTROLLER),
        ("Route::fallback([C::class, 'a']);", laravel.REASON_FALLBACK),
        ("Route::pattern('id', '.*');", laravel.REASON_GLOBAL_PATTERN),
        (
            "Route::group([], function ($router) { $router->get('/a', [C::class, 'a']); });",
            laravel.REASON_RECEIVER_VARIABLE,
        ),
        ("Route::group([], $callback);", laravel.REASON_GROUP_CALLBACK_UNREAD),
        ("require $file;", laravel.REASON_REQUIRE_NOT_LITERAL),
    ],
)
def test_unprovable_forms_are_counted(statement, reason):
    result = collect(web=statement + "\n")
    assert result.routes == []
    assert result.reasons == {reason: 1}


def test_macro_definition_is_not_counted():
    assert collect(web="Route::macro('x', function () {});\n").reasons == {}


# --- guard kinds ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("middleware", "kind", "label"),
    [
        ("'auth'", "authentication", "auth"),
        ("'auth:api'", "authentication", "auth:api"),
        ("'auth.basic'", "authentication", "auth.basic"),
        ("'verified'", "authentication", "verified"),
        ("'guest'", "authentication", "guest"),
        # docs-check laravel C.12: `auth.session` and `password.confirm` are sign-in checks.
        ("'auth.session'", "authentication", "auth.session"),
        ("'password.confirm'", "authentication", "password.confirm"),
        ("'cache.headers:public'", "middleware", "cache.headers:public"),
        ("'precognitive'", "middleware", "precognitive"),
        ("'subscribed'", "middleware", "subscribed"),
        ("\\Illuminate\\Auth\\Middleware\\Authenticate::class", "authentication",
         "Illuminate\\Auth\\Middleware\\Authenticate"),
        ("\\Illuminate\\Auth\\Middleware\\RequirePassword::class", "authentication",
         "Illuminate\\Auth\\Middleware\\RequirePassword"),
        ("'can:edit,post'", "authorization", "edit"),
        ("'throttle:60,1'", "middleware", "throttle:60,1"),
        ("'signed'", "middleware", "signed"),
        ("'csrf'", "middleware", "csrf"),
        ("'tenant'", "middleware", "tenant"),
        ("\\App\\Http\\Middleware\\Custom::class", "middleware", "App\\Http\\Middleware\\Custom"),
    ],
)  # fmt: skip
def test_guard_kind(middleware, kind, label):
    result = collect(web=f"Route::get('/g', [C::class, 'a'])->middleware({middleware});\n")
    assert route_guards(result) == {"/g": [(kind, label)]}
    guard = next(g for g in result.route_guards if g.label == label)
    assert guard.node_key == f"guard#laravel:{kind}:{label}"


def test_can_modifier_is_authorization_with_its_models():
    result = collect(
        web="Route::put('/p/{post}', [C::class, 'a'])->can('update', 'post');\n"
        "Route::get('/q', [C::class, 'b'])->can('viewAny', \\App\\Models\\Post::class);\n"
        "Route::get('/r', [D::class, 'a'])->can($ability);\n"
    )
    assert sorted((g.label, g.subject, g.guard_kind) for g in result.route_guards
                  if g.label not in ("web",)) == [
        ("update", "post", "authorization"),
        ("viewAny", "App\\Models\\Post", "authorization"),
    ]  # fmt: skip
    assert result.reasons == {laravel.REASON_CAN_NOT_LITERAL: 1}


def test_non_literal_middleware_entry_is_counted_and_the_rest_kept():
    result = collect(web="Route::get('/m', [C::class, 'a'])->middleware(['auth', $mw]);\n")
    assert route_guards(result) == {"/m": [("authentication", "auth")]}
    assert result.reasons == {laravel.REASON_MIDDLEWARE_NOT_LITERAL: 1}


def test_routes_sharing_a_node_keep_only_their_common_guards():
    result = collect(
        web="Route::get('/x', [C::class, 'a'])->middleware('auth');\n"
        "Route::post('/x', [C::class, 'a']);\n"
    )
    assert [r.methods for r in result.routes] == [("GET", "HEAD"), ("POST",)]
    assert [g.label for g in result.route_guards] == ["web"]
    assert result.reasons == {laravel.REASON_ROUTE_KEY_GUARDS_DIFFER: 1}


# --- resources -----------------------------------------------------------------------------


def test_resource_table():
    result = collect(web="Route::resource('photos', C::class);\n")
    assert [(r.methods, r.path, r.controller_method, r.name) for r in result.routes] == [
        (("GET", "HEAD"), "/photos", "index", "photos.index"),
        (("GET", "HEAD"), "/photos/create", "create", "photos.create"),
        (("POST",), "/photos", "store", "photos.store"),
        (("GET", "HEAD"), "/photos/{photo}", "show", "photos.show"),
        (("GET", "HEAD"), "/photos/{photo}/edit", "edit", "photos.edit"),
        (("PUT", "PATCH"), "/photos/{photo}", "update", "photos.update"),
        (("DELETE",), "/photos/{photo}", "destroy", "photos.destroy"),
    ]


def test_api_resource_with_options_inside_a_group():
    result = collect(
        web="Route::prefix('v1')->name('v1.')->middleware('auth')->group(function () {\n"
        "    Route::apiResource('user-posts', C::class)->except(['destroy'])\n"
        "        ->names(['index' => 'posts.list'])->middleware('throttle:9');\n"
        "    Route::resource('tags', D::class)->only('show')->parameters(['tags' => 'slug']);\n"
        "});\n"
    )
    assert [(r.path, r.controller_method, r.name) for r in result.routes] == [
        ("/v1/user-posts", "index", "v1.posts.list"),
        ("/v1/user-posts", "store", "v1.user-posts.store"),
        ("/v1/user-posts/{user_post}", "show", "v1.user-posts.show"),
        ("/v1/user-posts/{user_post}", "update", "v1.user-posts.update"),
        ("/v1/tags/{slug}", "show", "v1.tags.show"),
    ]
    guards = route_guards(result)
    assert guards["/v1/tags/{slug}"] == [("authentication", "auth")]
    # Two routes on that path, `index` and `store`, each with both guards.
    assert guards["/v1/user-posts"] == 2 * [
        ("authentication", "auth"),
        ("middleware", "throttle:9"),
    ]


def test_resources_and_api_resources():
    result = collect(
        web="Route::resources(['photos' => C::class]);\n"
        "Route::apiResources(['posts' => D::class, $name => C::class]);\n"
    )
    assert len([r for r in result.routes if r.controller_class == NS + "C"]) == 7
    assert [r.controller_method for r in result.routes if r.controller_class == NS + "D"] == list(
        laravel.API_RESOURCE_ACTIONS
    )
    assert result.reasons == {laravel.REASON_RESOURCE_NAME_NOT_LITERAL: 1}


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("Route::resource('people', C::class);", laravel.REASON_RESOURCE_PARAMETER_UNPROVED),
        ("Route::resource('categories', C::class);", laravel.REASON_RESOURCE_PARAMETER_UNPROVED),
        ("Route::resource('news', C::class);", laravel.REASON_RESOURCE_PARAMETER_UNPROVED),
        # docs-check laravel B.8: nested `a.b` is read now; `a/b` and a repeated wildcard are not.
        ("Route::resource('photos.photos', C::class);", laravel.REASON_RESOURCE_NESTED),
        ("Route::resource('people.comments', C::class)->only(['index']);",
         laravel.REASON_RESOURCE_PARAMETER_UNPROVED),
        ("Route::singleton('photos.thumbnail', C::class);", laravel.REASON_RESOURCE_NESTED),
        ("Route::singleton('profile', C::class)->shallow();",
         laravel.REASON_RESOURCE_OPTION_UNKNOWN),
        ("Route::singleton('profile', C::class)->only($x);",
         laravel.REASON_RESOURCE_OPTION_NOT_LITERAL),
        ("Route::resource('photos', C::class)->only($x);",
         laravel.REASON_RESOURCE_OPTION_NOT_LITERAL),
        ("Route::resource('photos', C::class)->except(['a', $b]);",
         laravel.REASON_RESOURCE_OPTION_NOT_LITERAL),
        # docs-check laravel B.8: `->shallow()` is read now; a non-literal flag is not.
        ("Route::resource('photos', C::class)->shallow($s);",
         laravel.REASON_RESOURCE_OPTION_NOT_LITERAL),
        ("Route::resource('photos', C::class)->scoped();", laravel.REASON_RESOURCE_OPTION_UNKNOWN),
        ("Route::resource('photos', C::class, ['only' => ['index']]);",
         laravel.REASON_RESOURCE_OPTION_UNKNOWN),
        ("Route::resource($name, C::class);", laravel.REASON_RESOURCE_NAME_NOT_LITERAL),
    ],
)  # fmt: skip
def test_resource_refusals(statement, reason):
    result = collect(web=statement + "\n")
    assert result.routes == []
    assert result.reasons == {reason: 1}


def test_unproved_parameter_still_allows_index_and_store():
    result = collect(web="Route::resource('people', C::class)->only(['index', 'store']);\n")
    assert [r.path for r in result.routes] == ["/people", "/people"]


def test_nested_and_shallow_resources():
    # docs-check laravel B.8: `photos/{photo}/comments/{comment}`, names `photos.comments.*`;
    # shallow member routes are `/comments/{comment}`, named `comments.*`.
    result = collect(
        web="Route::resource('photos.comments', C::class)->only(['index', 'show']);\n"
        "Route::resource('photos.comments', D::class)->only(['store', 'update'])->shallow();\n"
    )
    assert [(r.methods, r.path, r.controller_method, r.name) for r in result.routes] == [
        (("GET", "HEAD"), "/photos/{photo}/comments", "index", "photos.comments.index"),
        (("GET", "HEAD"), "/photos/{photo}/comments/{comment}", "show", "photos.comments.show"),
        (("POST",), "/photos/{photo}/comments", "store", "photos.comments.store"),
        (("PUT", "PATCH"), "/comments/{comment}", "update", "comments.update"),
    ]
    assert result.reasons == {}


def test_resource_parameter_override_takes_dash_to_underscore():
    # docs-check laravel B.8: `getResourceWildcard` replaces `-` with `_` after the override.
    result = collect(
        web="Route::resource('tags', C::class)->only('show')->parameters(['tags' => 'tag-id']);\n"
        "Route::resource('blog-posts.user-notes', D::class)->only('show');\n"
    )
    assert [r.path for r in result.routes] == [
        "/tags/{tag_id}",
        "/blog-posts/{blog_post}/user-notes/{user_note}",
    ]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # docs-check laravel B.8: singleton defaults, `creatable()`, `destroyable()`, apiSingleton.
        ("Route::singleton('profile', C::class);",
         [("show", ("GET", "HEAD"), "/profile"), ("edit", ("GET", "HEAD"), "/profile/edit"),
          ("update", ("PUT", "PATCH"), "/profile")]),
        ("Route::singleton('profile', C::class)->creatable();",
         [("show", ("GET", "HEAD"), "/profile"), ("edit", ("GET", "HEAD"), "/profile/edit"),
          ("update", ("PUT", "PATCH"), "/profile"), ("create", ("GET", "HEAD"), "/profile/create"),
          ("store", ("POST",), "/profile"), ("destroy", ("DELETE",), "/profile")]),
        ("Route::singleton('profile', C::class)->destroyable()->except(['edit']);",
         [("show", ("GET", "HEAD"), "/profile"), ("update", ("PUT", "PATCH"), "/profile"),
          ("destroy", ("DELETE",), "/profile")]),
        ("Route::apiSingleton('profile', C::class);",
         [("show", ("GET", "HEAD"), "/profile"), ("update", ("PUT", "PATCH"), "/profile")]),
        ("Route::apiSingleton('profile', C::class)->creatable();",
         [("show", ("GET", "HEAD"), "/profile"), ("update", ("PUT", "PATCH"), "/profile"),
          ("store", ("POST",), "/profile"), ("destroy", ("DELETE",), "/profile")]),
    ],
)  # fmt: skip
def test_singletons(statement, expected):
    result = collect(web=statement + "\n")
    assert [(r.controller_method, r.methods, r.path) for r in result.routes] == expected
    assert {r.name for r in result.routes} == {f"profile.{m}" for m, _, _ in expected}
    assert result.reasons == {}


# --- bootstrap/app.php ---------------------------------------------------------------------


def test_api_file_takes_the_default_api_prefix_and_group():
    result = collect(api="Route::get('/user', [C::class, 'a']);\n")
    assert [r.path for r in result.routes] == ["/api/user"]
    assert [(g.label, g.source_file) for g in result.route_guards] == [("api", "bootstrap/app.php")]


def test_api_prefix_is_read_from_with_routing():
    boot = BOOT.replace("api.php',\n", "api.php',\n    apiPrefix: '/v2/',\n")
    result = collect(api="Route::get('/user', [C::class, 'a']);\n", boot=boot)
    assert [r.path for r in result.routes] == ["/v2/user"]


def test_non_literal_api_prefix_refuses_the_api_file():
    boot = BOOT.replace("api.php',\n", "api.php',\n    apiPrefix: $prefix,\n")
    result = collect(
        web="Route::get('/w', [C::class, 'a']);\n",
        api="Route::get('/user', [C::class, 'a']);\n",
        boot=boot,
    )
    assert [r.path for r in result.routes] == ["/w"]
    assert result.reasons == {
        laravel.REASON_API_PREFIX_NOT_LITERAL: 1,
        laravel.REASON_FILE_NOT_LOADED: 1,
    }


def test_web_may_list_several_files():
    boot = BOOT.replace(
        "web: __DIR__.'/../routes/web.php'",
        "web: [__DIR__.'/../routes/web.php', __DIR__.'/../routes/extra.php']",
    )
    result = collect(
        web="Route::get('/w', [C::class, 'a']);\n",
        extra={"routes/extra.php": HEAD + "Route::get('/e', [C::class, 'b']);\n"},
        boot=boot,
    )
    assert [r.path for r in result.routes] == ["/w", "/e"]


@pytest.mark.parametrize(
    ("boot", "reason"),
    [
        (None, laravel.REASON_BOOTSTRAP_UNREAD),
        ("<?php return Application::configure()->create();", laravel.REASON_BOOTSTRAP_UNREAD),
        (
            "<?php return Application::configure()->withRouting(using: function () {})->create();",
            laravel.REASON_ROUTING_USING,
        ),
    ],
)
def test_without_a_readable_with_routing_no_route_is_read(boot, reason):
    result = collect(web="Route::get('/w', [C::class, 'a']);\n", boot=boot)
    assert result.routes == []
    assert result.reasons == {reason: 1, laravel.REASON_FILE_NOT_LOADED: 1}


def test_then_callback_is_counted_and_the_files_still_read():
    boot = BOOT.replace("api.php',\n", "api.php',\n    then: function () {},\n")
    result = collect(web="Route::get('/w', [C::class, 'a']);\n", boot=boot)
    assert [r.path for r in result.routes] == ["/w"]
    assert result.reasons == {laravel.REASON_ROUTING_THEN: 1}


def test_health_route_is_counted_not_published():
    # docs-check laravel A.2: `health:` is a bare GET route with no group and no controller.
    boot = BOOT.replace("api.php',\n", "api.php',\n    health: '/up',\n")
    result = collect(web="Route::get('/w', [C::class, 'a']);\n", boot=boot)
    assert [r.path for r in result.routes] == ["/w"]
    assert result.reasons == {laravel.REASON_HEALTH_ROUTE: 1}


def test_health_is_not_counted_under_using():
    # docs-check laravel A.2: with `using:` the framework registers no HTTP routes at all.
    boot = (
        "<?php return Application::configure()->withRouting(\n"
        "    using: function () {}, health: '/up',\n)->create();"
    )
    result = collect(web="Route::get('/w', [C::class, 'a']);\n", boot=boot)
    assert result.reasons == {laravel.REASON_ROUTING_USING: 1, laravel.REASON_FILE_NOT_LOADED: 1}


def test_api_may_list_several_files():
    # docs-check laravel A.2: `web` and `api` accept a string or an array of paths.
    boot = BOOT.replace(
        "api: __DIR__.'/../routes/api.php'",
        "api: [__DIR__.'/../routes/api.php', __DIR__.'/../routes/api2.php']",
    )
    result = collect(
        api="Route::get('/a', [C::class, 'a']);\n",
        extra={"routes/api2.php": HEAD + "Route::get('/b', [C::class, 'b']);\n"},
        boot=boot,
    )
    assert [r.path for r in result.routes] == ["/api/a", "/api/b"]


BARE_ROUTES = "<?php\nRoute::get('/x', [\\App\\Http\\Controllers\\C::class, 'a']);\n"


@pytest.mark.parametrize(
    ("config", "routes", "reasons"),
    [
        # docs-check laravel A.1: no config, or the 11+ skeleton without `aliases`: the default.
        (None, 1, {}),
        ("<?php return ['name' => 'x'];", 1, {}),
        # A non-literal `aliases` keeps `Facade::defaultAliases()`, and is not counted.
        ("<?php return ['aliases' => Facade::defaultAliases()->merge(['X' => Y::class])"
         "->toArray()];", 1, {}),
        ("<?php return ['aliases' => ['Route' => Illuminate\\Support\\Facades\\Route::class]];",
         1, {}),
        # A literal `aliases` without `Route` replaces the default list.
        ("<?php return ['aliases' => ['Gate' => Illuminate\\Support\\Facades\\Gate::class]];", 0,
         {laravel.REASON_ROUTE_ALIAS_REMOVED: 1}),
        ("<?php return ['aliases' => ['Route' => App\\MyRoute::class]];", 0,
         {laravel.REASON_ROUTE_ALIAS_REMOVED: 1}),
    ],
)  # fmt: skip
def test_bare_route_alias(config, routes, reasons):
    extra = {"routes/web.php": BARE_ROUTES}
    if config is not None:
        extra["config/app.php"] = config
    result = collect(extra=extra)
    assert len(result.routes) == routes
    assert result.reasons == reasons


def test_imported_route_ignores_the_alias_config():
    # docs-check laravel A.1: an imported facade does not depend on `app.aliases`.
    result = collect(
        web="Route::get('/x', [C::class, 'a']);\n",
        extra={"config/app.php": "<?php return ['aliases' => []];"},
    )
    assert [r.path for r in result.routes] == ["/x"]
    assert result.reasons == {}


# --- controller middleware -----------------------------------------------------------------

HAS_MIDDLEWARE_HEAD = (
    "<?php namespace App\\Http\\Controllers;\n"
    "use Illuminate\\Routing\\Controllers\\HasMiddleware;\n"
    "use Illuminate\\Routing\\Controllers\\Middleware;\n"
)


def controller_result(body: str, routes: str = "", implements: str = " implements HasMiddleware"):
    controller = (
        HAS_MIDDLEWARE_HEAD + f"class C{implements} {{\n{body}\n"
        "    public function a() {}\n    public function b() {}\n}\n"
    )
    return collect(
        web=routes or "Route::get('/a', [C::class, 'a']);\nRoute::get('/b', [C::class, 'b']);\n",
        extra={"app/Http/Controllers/C.php": controller},
    )


def holder_guards(result) -> list[tuple]:
    return sorted((g.holder_method, g.guard_kind, g.label) for g in result.guards)


def test_has_middleware_with_only_and_except():
    result = controller_result(
        "public static function middleware(): array { return [\n"
        "    'auth',\n"
        "    new Middleware(['log', 'throttle:5'], only: ['a']),\n"
        "    new Middleware('can:manage', except: ['a']),\n"
        "    new Middleware(middleware: 'verified', only: ['b', 'unrouted']),\n"
        "]; }"
    )
    assert holder_guards(result) == [
        ("a", "authentication", "auth"),
        ("a", "middleware", "log"),
        ("a", "middleware", "throttle:5"),
        ("b", "authentication", "auth"),
        ("b", "authentication", "verified"),
        ("b", "authorization", "manage"),
    ]
    assert {(g.via, g.guard_attribute, g.provider) for g in result.guards} == {
        ("middleware", "middleware", "laravel")
    }
    assert result.reasons == {}


@pytest.mark.parametrize(
    ("body", "implements", "reason"),
    [
        (
            "public static function middleware(): array { return [fn ($r, $n) => $n($r)]; }",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_CLOSURE,
        ),
        (
            "public static function middleware(): array { return [new Middleware(fn () => 1)]; }",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_CLOSURE,
        ),
        (
            "public static function middleware(): array {\n"
            "    return [new Middleware('a', only: $x)];\n"
            "}",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL,
        ),
        (
            "public static function middleware(): array { return [new Other('auth')]; }",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL,
        ),
        (
            "public static function middleware(): array { $m = ['auth']; return $m; }",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL,
        ),
        (
            "public static function middleware(): array { return self::LIST; }",
            " implements HasMiddleware",
            laravel.REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL,
        ),
        (
            "public static function middleware(): array { return ['auth']; }",
            "",
            laravel.REASON_CONTROLLER_MIDDLEWARE_UNPROVED,
        ),
        ("", " implements HasMiddleware", laravel.REASON_CONTROLLER_MIDDLEWARE_UNPROVED),
        (
            "public function __construct() { $this->middleware('auth')->only('a'); }",
            "",
            laravel.REASON_CONTROLLER_CONSTRUCTOR_MIDDLEWARE,
        ),
    ],
)
def test_controller_middleware_refusals(body, implements, reason):
    result = controller_result(body, implements=implements)
    assert result.guards == []
    assert result.reasons == {reason: 1}


def test_route_without_middleware_voids_controller_guards():
    result = controller_result(
        "public static function middleware(): array { return ['auth', 'log']; }",
        routes="Route::get('/a', [C::class, 'a'])->withoutMiddleware('auth');\n"
        "Route::get('/b', [C::class, 'b']);\n",
    )
    assert holder_guards(result) == [
        ("b", "authentication", "auth"),
        ("b", "middleware", "log"),
    ]
    assert result.reasons == {laravel.REASON_CONTROLLER_MIDDLEWARE_EXCLUDED: 2}


# --- grant calls ---------------------------------------------------------------------------

GRANT_CONTROLLER = (
    "<?php namespace App\\Http\\Controllers;\n"
    "use Illuminate\\Support\\Facades\\Gate;\n"
    "class C {\n"
    "    public function a($post) {\n"
    "        $this->authorize('update', $post);\n"
    "        Gate::authorize(ability: 'publish', arguments: [$post]);\n"
    "        Gate::allows('view'); Gate::denies('delete'); Gate::check('edit');\n"
    "        $this->authorize($ability);\n"
    "        \\Illuminate\\Support\\Facades\\Gate::allows('fq');\n"
    "        Other::allows('nope');\n"
    "        $later = function () { Gate::authorize('nested'); };\n"
    "    }\n"
    "    public function b() { Gate::authorize('unrouted'); }\n"
    "}\n"
)


def test_grant_calls_in_handler_bodies():
    result = collect(
        web="Route::get('/a', [C::class, 'a']);\n",
        extra={"app/Http/Controllers/C.php": GRANT_CONTROLLER},
    )
    assert [(c.check_call, c.label, c.literal, c.conditional, c.provider)
            for c in result.checks] == [
        ("authorize", "update", True, False, "laravel"),
        ("Gate::authorize", "publish", True, False, "laravel"),
        ("Gate::allows", "view", True, True, "laravel"),
        ("Gate::denies", "delete", True, True, "laravel"),
        ("Gate::check", "edit", True, True, "laravel"),
        ("authorize", "$ability", False, False, "laravel"),
        ("Gate::allows", "fq", True, True, "laravel"),
    ]  # fmt: skip
    assert result.checks[1].subject == "[$post]"
    assert result.checks[0].node_key == "guard#laravel:authorization:update"


def test_gate_any_and_none_are_conditional_and_allow_if_is_not_a_check():
    # docs-check laravel C.11: `any` / `none` / `check` take `$abilities` and return a bool;
    # `allowIf` / `denyIf` take no ability string.
    controller = (
        "<?php namespace App\\Http\\Controllers;\n"
        "use Illuminate\\Support\\Facades\\Gate;\n"
        "class C { public function a() {\n"
        "    Gate::any(['x', 'y']); Gate::none(abilities: 'z'); Gate::check('w');\n"
        "    Gate::allowIf(true); Gate::denyIf(false);\n"
        "} }\n"
    )
    result = collect(
        web="Route::get('/a', [C::class, 'a']);\n",
        extra={"app/Http/Controllers/C.php": controller},
    )
    assert [(c.check_call, c.label, c.literal, c.conditional) for c in result.checks] == [
        ("Gate::any", "['x', 'y']", False, True),
        ("Gate::none", "z", True, True),
        ("Gate::check", "w", True, True),
    ]


ATTRIBUTE_HEAD = (
    "<?php namespace App\\Http\\Controllers;\n"
    "use Illuminate\\Routing\\Attributes\\Controllers\\Authorize;\n"
    "use Illuminate\\Routing\\Attributes\\Controllers\\Middleware;\n"
    "use Illuminate\\Routing\\Attributes\\Controllers\\WithoutMiddleware;\n"
)


def attribute_result(body: str, version: str | None = "v13.42.0", routes: str = ""):
    controller = ATTRIBUTE_HEAD + body
    extra = {"app/Http/Controllers/C.php": controller}
    if version is not None:
        extra.update(lock(version))
    return collect(
        web=routes or "Route::get('/a', [C::class, 'a']);\nRoute::get('/b', [C::class, 'b']);\n",
        extra=extra,
    )


def test_controller_attributes_on_class_and_method():
    # docs-check laravel C.10: class and method targets, repeatable, `only` / `except` arrays.
    result = attribute_result(
        "#[Middleware('auth')]\n"
        "#[Middleware(['throttle:5', 'can:manage'], except: ['a'])]\n"
        "#[Authorize('view', only: ['a'])]\n"
        "class C {\n"
        "    #[Authorize(ability: 'edit', models: 'post')] #[Middleware('verified')]\n"
        "    public function a() {}\n"
        "    public function b() {}\n"
        "}\n"
    )
    assert sorted((g.holder_method, g.guard_kind, g.label, g.subject, g.guard_attribute)
                  for g in result.guards) == [
        ("a", "authentication", "auth", "", "Middleware"),
        ("a", "authentication", "verified", "", "Middleware"),
        ("a", "authorization", "edit", "post", "Authorize"),
        ("a", "authorization", "view", "", "Authorize"),
        ("b", "authentication", "auth", "", "Middleware"),
        ("b", "authorization", "manage", "", "Middleware"),
        ("b", "middleware", "throttle:5", "", "Middleware"),
    ]  # fmt: skip
    assert {(g.via, g.provider) for g in result.guards} == {("attribute", "laravel")}
    assert result.reasons == {}


def test_authorize_attribute_shares_the_can_node():
    # docs-check laravel C.10, B.7: `#[Authorize('x')]` and `->can('x')` are both `can:x`.
    result = attribute_result(
        "#[Authorize('update')] class C { public function a() {} public function b() {} }\n",
        routes="Route::get('/a', [C::class, 'a'])->can('update');\n",
    )
    route = next(g for g in result.route_guards if g.label == "update")
    holder = next(g for g in result.guards if g.label == "update")
    assert route.node_key == holder.node_key == "guard#laravel:authorization:update"


def test_without_middleware_attribute_removes_route_middleware():
    # docs-check laravel C.10: `#[WithoutMiddleware]` removes a middleware by its text.
    result = attribute_result(
        "class C { #[WithoutMiddleware('throttle:9')] public function a() {}\n"
        "          public function b() {} }\n",
        routes="Route::middleware(['auth', 'throttle:9'])->group(function () {\n"
        "    Route::get('/a', [C::class, 'a']);\n"
        "    Route::get('/b', [C::class, 'b']);\n"
        "});\n",
    )
    assert route_guards(result) == {
        "/a": [("authentication", "auth")],
        "/b": [("authentication", "auth"), ("middleware", "throttle:9")],
    }
    assert result.reasons == {}


@pytest.mark.parametrize(
    ("body", "reason", "count"),
    [
        # docs-check laravel C.10: `only` / `except` must be literal arrays of method names.
        ("#[Middleware('auth', only: self::ONLY)] class C { public function a() {}\n"
         "public function b() {} }\n", laravel.REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL, 1),
        ("#[Middleware('auth', only: 'a')] class C { public function a() {}\n"
         "public function b() {} }\n", laravel.REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL, 1),
        ("#[Authorize('x', null, ['a', $b])] class C { public function a() {}\n"
         "public function b() {} }\n", laravel.REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL, 1),
        ("#[Middleware(self::MW)] class C { public function a() {} public function b() {} }\n",
         laravel.REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL, 1),
        ("#[Authorize(Ability::Edit)] class C { public function a() {}\n"
         "public function b() {} }\n", laravel.REASON_CAN_NOT_LITERAL, 1),
    ],
)  # fmt: skip
def test_controller_attribute_refusals(body, reason, count):
    result = attribute_result(body)
    assert result.guards == []
    assert result.reasons == {reason: count}


def test_unread_without_middleware_attribute_proves_no_route_guard():
    # docs-check laravel C.10: an unread removal leaves every guard of that handler unproved.
    result = attribute_result(
        "class C { #[WithoutMiddleware($m)] public function a() {} public function b() {} }\n",
        routes="Route::get('/a', [C::class, 'a'])->middleware('auth');\n",
    )
    assert route_guards(result) == {}
    assert result.reasons == {laravel.REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL: 1}


@pytest.mark.parametrize(
    ("version", "reasons"),
    [
        # docs-check laravel C.10: the attributes exist from 13.x only.
        ("v12.9.0", {laravel.REASON_CONTROLLER_ATTRIBUTE_VERSION: 2}),
        (None, {laravel.REASON_VERSION_UNREAD: 1}),
    ],
)
def test_controller_attributes_need_13(version, reasons):
    result = attribute_result(
        "#[Middleware('auth')] #[Authorize('x')]\n"
        "class C { public function a() {} public function b() {} }\n",
        version=version,
    )
    assert result.guards == []
    assert result.reasons == reasons


def test_controller_attribute_short_name_without_import_is_ignored():
    # Strict FQCN: an unimported `#[Authorize]` resolves into the file's own namespace.
    result = collect(
        web="Route::get('/a', [C::class, 'a']);\n",
        extra={
            **lock("v13.0.0"),
            "app/Http/Controllers/C.php": (
                "<?php namespace App\\Http\\Controllers;\n"
                "#[Authorize('x')] class C { public function a() {} }\n"
            ),
        },
    )
    assert result.guards == []
    assert result.reasons == {}


# --- route template ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "wheres", "expected"),
    [
        ("/posts/{post}", {}, ("/posts/{post}", "")),
        ("/posts/{post?}", {}, ("/posts/{post?}", "")),
        ("posts/{post:slug}", {}, ("/posts/{post}", "")),
        ("/posts/{post:slug?}", {}, ("/posts/{post?}", "")),
        ("/f/{path}", {"path": ".*"}, ("", TEMPLATE_MULTI_SEGMENT)),
        ("/f/{path}", {"path": "[^x]+"}, ("", TEMPLATE_MULTI_SEGMENT)),
        ("/f/{path}", {"path": "\\S+"}, ("", TEMPLATE_MULTI_SEGMENT)),
        ("/f/{path}", {"path": "[a-z/]+"}, ("", TEMPLATE_MULTI_SEGMENT)),
        ("/f/{id}", {"id": "[0-9]+"}, ("/f/{id}", "")),
        ("/f/{id}", {"id": "[^/]+"}, ("/f/{id}", "")),
        ("/f/{id}", {"id": "\\d+\\.\\d+"}, ("/f/{id}", "")),
        ("/f/{id}", {"id": None}, ("/f/{id}", TEMPLATE_REQUIREMENT_UNREAD)),
        ("/f/{other}", {"id": ".*"}, ("/f/{other}", "")),
    ],
)
def test_route_template(path, wheres, expected):
    assert laravel.route_template(path, wheres) == expected


def test_where_constraints_are_counted_on_the_result():
    result = collect(
        web="Route::get('/a/{x}', [C::class, 'a'])->where('x', '.+');\n"
        "Route::get('/b/{id}', [C::class, 'b'])->whereNumber('id');\n"
        "Route::get('/c/{id}', [D::class, 'a'])->where('id', $pattern);\n"
        "Route::where(['y' => '.*'])->group(function () {\n"
        "    Route::get('/d/{y}', [C::class, 'a']);\n"
        "});\n"
    )
    assert [(r.path, r.template, r.template_reason) for r in result.routes] == [
        ("/a/{x}", "", TEMPLATE_MULTI_SEGMENT),
        ("/b/{id}", "/b/{id}", ""),
        ("/c/{id}", "/c/{id}", TEMPLATE_REQUIREMENT_UNREAD),
        ("/d/{y}", "", TEMPLATE_MULTI_SEGMENT),
    ]
    assert result.reasons == {
        TEMPLATE_MULTI_SEGMENT: 2,
        TEMPLATE_REQUIREMENT_UNREAD: 1,
    }


# --- framework source corrections (plan P2, Laravel 13.x `src/Illuminate/`) ---------------


def test_l1_registrar_chain_before_a_verb_is_the_route_action():
    # Routing/RouteRegistrar.php:304 `$action = array_merge($this->attributes, $action);` makes
    # the registrar prefix an action prefix; Router.php:589 builds `g/b`, then Route.php:189 and
    # :829 prepend `p`. RouteGroup.php:41 merges group middleware first; RouteGroup.php:160
    # appends the action `as`. RouteRegistrar.php:160 keeps one value per key: last one wins.
    result = collect(
        web="Route::prefix('g')->middleware('web2')->name('g.')->group(function () {\n"
        "    Route::prefix('p')->middleware('auth')->name('r.')->get('b', [C::class, 'a'])"
        "->name('x');\n"
        "    Route::prefix('p')->get('c', [C::class, 'b'])->prefix('v');\n"
        "});\n"
        "Route::prefix('p')->get('b', [D::class, 'a']);\n"
        "Route::prefix('one')->prefix('two')->get('c', [C::class, 'a']);\n"
    )
    assert [(r.path, r.name) for r in result.routes] == [
        ("/p/g/b", "g.r.x"),
        ("/v/p/g/c", "g."),
        ("/p/b", None),
        ("/two/c", None),
    ]
    assert route_guards(result)["/p/g/b"] == [("middleware", "web2"), ("authentication", "auth")]
    assert result.reasons == {}


def test_l2_registrar_controller_before_a_verb_binds_nothing():
    # Routing/Router.php:635 `prependGroupController` reads the group stack only, and :642
    # `$action['controller'] = $action['uses'];` overwrites the registrar `controller`.
    result = collect(
        web="Route::controller(C::class)->get('/x', 'a');\n"
        "Route::controller(D::class)->group(function () {\n"
        "    Route::controller(C::class)->get('/y', 'a');\n"
        "});\n"
        "Route::controller(C::class)->get('/z', [D::class, 'a']);\n"
    )
    assert [(r.path, r.controller_class.removeprefix(NS), r.controller_method, r.handler_kind)
            for r in result.routes] == [
        ("/x", "", "", "unresolved"),
        ("/y", "D", "a", "method"),
        ("/z", "D", "a", "method"),
    ]  # fmt: skip
    assert result.routes[0].handler_file == "routes/web.php"
    assert result.routes[0].node_key == "laravel:unresolved:routes/web.php:5#/x"
    assert result.reasons == {laravel.REASON_REGISTRAR_CONTROLLER: 1}
    # `uses` `a` has no `@`, so `RouteAction::parse` calls `makeInvokable`
    # (Routing/RouteAction.php:46-47), which throws "Invalid route action" (:92): the framework
    # rejects `/x` at registration. The reason says so, not "unguarded".
    assert (
        laravel.REASON_REGISTRAR_CONTROLLER == "laravel-registrar-controller-rejected-by-framework"
    )


def test_l3_group_controller_and_namespace_rules():
    # Routing/Router.php:676 `if (class_exists($class))` keeps a corpus class name unprefixed;
    # :680 a string with `@`. Router.php:657 skips the namespace for a leading `\`, and
    # RouteRegistrar.php:290-291 gives a registrar array action that `\`, so it gets neither.
    result = collect(
        web="Route::controller(C::class)->group(function () {\n"
        "    Route::get('/m', 'a');\n"
        "    Route::get('/legacy', 'Legacy');\n"
        "});\n"
        "Route::namespace('Admin')->group(function () {\n"
        "    Route::prefix('p')->get('/arr', [C::class, 'a']);\n"
        "    Route::get('/str', 'Http\\C@a');\n"
        "});\n",
        extra={"app/Legacy.php": "<?php\nclass Legacy { public function __invoke() {} }\n"},
    )
    assert [(r.path, r.controller_class, r.controller_method) for r in result.routes] == [
        ("/m", NS + "C", "a"),
        ("/p/arr", NS + "C", "a"),
        ("/str", "Admin\\Http\\C", "a"),
    ]
    assert result.reasons == {laravel.REASON_CONTROLLER_GROUP_CLASS: 1}


def test_l4_as_concatenates_with_no_separator():
    # Routing/RouteGroup.php:160 `$new['as'] = $old['as'].($new['as'] ?? '');`
    result = collect(
        web="Route::name('admin')->group(function () {\n"
        "    Route::get('/a', [C::class, 'a'])->name('index');\n"
        "    Route::resource('photos', C::class)->only(['index']);\n"
        "});\n"
    )
    assert [r.name for r in result.routes] == ["adminindex", "adminphotos.index"]


@pytest.mark.parametrize(
    ("lock_text", "version"),
    [
        ('{"packages": [{"name": "laravel/framework", "version": "v13.35.1"}]}', (13, 35)),
        ('{"packages": [{"name": "laravel/framework", "version": "13.0"}]}', (13, 0)),
        ('{"packages": [{"name": "laravel/framework", "version": "13.x-dev"}]}', (13, None)),
        ('{"packages": [{"name": "laravel/framework", "version": "dev-master"}]}', None),
    ],
)
def test_l5_installed_version_has_minor_precision(lock_text, version):
    assert laravel.installed_version(lock_text.encode()) == version


L5_BODY = (
    "#[Middleware('verified')]\n"
    "class C { #[WithoutMiddleware('throttle:9')] public function a() {} public function b() {} }\n"
)
L5_ROUTES = "Route::middleware(['auth', 'throttle:9'])->get('/a', [C::class, 'a']);\n"


@pytest.mark.parametrize(
    ("version", "guards", "route", "reasons"),
    [
        # CHANGELOG.md:1577-1578: `#[Middleware]` / `#[Authorize]` from v13.0.0.
        ("v12.9.0", [], ["auth", "throttle:9"], {laravel.REASON_CONTROLLER_ATTRIBUTE_VERSION: 2}),
        # CHANGELOG.md:789: `#[WithoutMiddleware]` from v13.20.0; before it, it is ignored.
        ("v13.0.0", ["verified"], ["auth", "throttle:9"],
         {laravel.REASON_WITHOUT_ATTRIBUTE_VERSION: 1}),
        ("v13.19.3", ["verified"], ["auth", "throttle:9"],
         {laravel.REASON_WITHOUT_ATTRIBUTE_VERSION: 1}),
        ("v13.20.0", ["verified"], ["auth"], {}),
        # No minor: the removal may apply, so nothing on that handler is proved.
        ("13.x-dev", [], [],
         {laravel.REASON_VERSION_UNREAD: 1, laravel.REASON_CONTROLLER_MIDDLEWARE_EXCLUDED: 1}),
    ],
)  # fmt: skip
def test_l5_controller_attribute_version_gates(version, guards, route, reasons):
    result = attribute_result(L5_BODY, version=version, routes=L5_ROUTES)
    assert [g.label for g in result.guards] == guards
    assert [label for _, label in route_guards(result).get("/a", [])] == route
    assert result.reasons == reasons


PARENT_HEAD = ATTRIBUTE_HEAD.replace("namespace App\\Http\\Controllers;", "namespace App\\Base;")


@pytest.mark.parametrize(
    ("version", "parent", "route", "reasons"),
    [
        # CHANGELOG.md:1347: parent class attributes from v13.5.0 (Route.php:1201, :1266).
        ("v13.4.0", "\\App\\Base\\P", ["auth"], {}),
        ("v13.5.0", "\\App\\Base\\P", ["auth"],
         {laravel.REASON_CONTROLLER_ATTRIBUTE_INHERITED: 1}),
        # From v13.20.0 the inherited `#[WithoutMiddleware]` may remove the route's `auth`.
        ("v13.20.0", "\\App\\Base\\P", [], {laravel.REASON_CONTROLLER_ATTRIBUTE_INHERITED: 1}),
        # A parent outside the corpus may carry one: nothing on the handler is proved.
        ("v13.42.0", "\\Vendor\\Base", [], {laravel.REASON_CONTROLLER_PARENT_UNREAD: 1}),
        # Routing/Controller.php:7 declares no attribute.
        ("v13.42.0", "\\Illuminate\\Routing\\Controller", ["auth"], {}),
    ],
)  # fmt: skip
def test_l5_parent_class_attributes(version, parent, route, reasons):
    result = collect(
        web="Route::get('/a', [C::class, 'a'])->middleware('auth');\n",
        extra={
            **lock(version),
            "app/Base/P.php": PARENT_HEAD + "#[WithoutMiddleware('auth')] class P {}\n",
            "app/Http/Controllers/C.php": (
                ATTRIBUTE_HEAD + f"class C extends {parent} {{ public function a() {{}} }}\n"
            ),
        },
    )
    assert [label for _, label in route_guards(result).get("/a", [])] == route
    assert result.guards == []
    assert result.reasons == reasons


def test_l6_without_middleware_attribute_removes_route_and_group_middleware():
    # Routing/Route.php:1305-1308 merges the attribute exclusions into `excludedMiddleware()`,
    # and Router.php:847 applies them to the whole `gatherMiddleware()` list.
    result = attribute_result(
        "class C { #[WithoutMiddleware(['auth', 'throttle:9'])] public function a() {}\n"
        "          public function b() {} }\n",
        routes="Route::middleware('auth')->group(function () {\n"
        "    Route::get('/a', [C::class, 'a'])->middleware('throttle:9');\n"
        "    Route::get('/b', [C::class, 'b'])->middleware('throttle:9');\n"
        "});\n",
    )
    assert route_guards(result) == {
        "/b": [("authentication", "auth"), ("middleware", "throttle:9")],
    }
    assert result.reasons == {}


@pytest.mark.parametrize(
    ("then", "paths", "reasons"),
    [
        # Foundation/Configuration/ApplicationBuilder.php:162 `... || is_callable($then)`
        # replaces a given `using` with the default callback.
        ("function () {}", ["/w"], {laravel.REASON_ROUTING_THEN: 1}),
        ("$then", [], {laravel.REASON_ROUTING_USING: 1, laravel.REASON_FILE_NOT_LOADED: 1}),
    ],
)
def test_l7_callable_then_overrides_using(then, paths, reasons):
    boot = BOOT.replace("api.php',\n", f"api.php',\n    using: function () {{}}, then: {then},\n")
    result = collect(web="Route::get('/w', [C::class, 'a']);\n", boot=boot)
    assert [r.path for r in result.routes] == paths
    assert result.reasons == reasons


def test_l7_resource_verbs_refuse_create_and_edit():
    # Routing/Router.php:1424 calls ResourceRegistrar::verbs, which at :744 rewrites the
    # `create` / `edit` segments for every later resource.
    result = collect(
        web="Route::resource('photos', C::class);\n"
        "Route::singleton('profile', D::class)->creatable();\n",
        extra={
            "app/Providers/AppServiceProvider.php": (
                "<?php\nclass AppServiceProvider { public function boot() {\n"
                "    Route::resourceVerbs(['create' => 'crear', 'edit' => 'editar']);\n} }\n"
            )
        },
    )
    assert sorted(r.controller_method for r in result.routes) == [
        "destroy", "destroy", "index", "show", "show", "store", "store", "update", "update",
    ]  # fmt: skip
    assert result.reasons == {laravel.REASON_RESOURCE_VERBS: 4}


def test_l7_resource_name_with_a_slash_is_a_prefix_group():
    # Routing/ResourceRegistrar.php:88 sends a `/` name to `prefixedResource`; :258 takes all
    # but the last segment as the group prefix, and the last segment names the routes.
    result = collect(
        web="Route::prefix('g')->group(function () {\n"
        "    Route::resource('admin/photos', C::class)->only(['index', 'show']);\n"
        "});\n"
        "Route::singleton('admin/profile', D::class)->only(['show']);\n"
    )
    assert [(r.path, r.name) for r in result.routes] == [
        ("/g/admin/photos", "photos.index"),
        ("/g/admin/photos/{photo}", "photos.show"),
        ("/admin/profile", "profile.show"),
    ]
    assert result.reasons == {}


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        # Routing/ResourceRegistrar.php:473 builds the singleton `show` URI without
        # `getShallowName`, unlike edit/update/destroy (:492, :511, :530): a nested singleton,
        # where that matters, is not read.
        ("Route::singleton('photos.thumbnail', C::class);", laravel.REASON_RESOURCE_NESTED),
        # PendingSingletonResourceRegistration declares no `shallow()` method.
        ("Route::singleton('profile', C::class)->shallow();",
         laravel.REASON_RESOURCE_OPTION_UNKNOWN),
    ],
)  # fmt: skip
def test_l7_singleton_shallow_is_never_read(statement, reason):
    result = collect(web=statement + "\n")
    assert result.routes == []
    assert result.reasons == {reason: 1}


@pytest.mark.parametrize(
    ("statement", "paths", "reason"),
    [
        # Routing/RouteRegistrar.php:175 passes the attributes to `Router::resource` as options;
        # `ResourceRegistrar` never reads `prefix` and joins `as` with a `.`.
        ("Route::prefix('admin')->resource('photos', C::class);", [],
         laravel.REASON_RESOURCE_REGISTRAR_ATTRIBUTE),
        ("Route::name('admin')->singleton('profile', C::class);", [],
         laravel.REASON_RESOURCE_REGISTRAR_ATTRIBUTE),
        # `middleware` is an option `getResourceAction` reads (ResourceRegistrar.php:643-645).
        ("Route::middleware('auth')->resource('photos', C::class)->only(['index']);",
         ["/photos"], None),
        # RouteRegistrar has no `resources` method: `__call` throws (:322-345).
        ("Route::middleware('auth')->resources(['photos' => C::class]);", [],
         laravel.REASON_METHOD_UNKNOWN),
    ],
)  # fmt: skip
def test_registrar_attributes_before_a_resource(statement, paths, reason):
    result = collect(web=statement + "\n")
    assert [r.path for r in result.routes] == paths
    assert result.reasons == ({reason: 1} if reason else {})
