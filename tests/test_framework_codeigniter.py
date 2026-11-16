"""The CodeIgniter 4 provider: routes files, route filters, `Config\\Filters`, attributes.

Every test passes `codeigniter.PROVIDER` explicitly, so a test does not depend on detection.
Each rule cites the docs-check item it rests on (`docs-check ci4 <item>`).
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
from graphify_php.frameworks import REGISTRY, codeigniter, symfony  # noqa: E402
from graphify_php.frameworks.detect import select_providers  # noqa: E402
from graphify_php.frameworks.provider import (  # noqa: E402
    AUTHORIZATION_ACCESS_CONTROL_BLOCKED,
    AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    FrameworkProvider,
    ProviderContext,
    file_reader,
)
from graphify_php.graph_adapter import (  # noqa: E402
    GUARD_NODE_KIND,
    GUARD_RELATION,
    ROUTE_NODE_KIND,
    ROUTE_RELATION,
)
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax.routes import TEMPLATE_MULTI_SEGMENT, TEMPLATE_UNPARSED  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frameworks" / "codeigniter"
NS = "App\\Controllers\\"
CI = codeigniter

# Reasons every in-memory app without those config files counts; dropped by `own()`.
BASELINE = {
    CI.REASON_ROUTE_DISCOVERY,
    CI.REASON_FILTERS_CONFIG_MISSING,
    CI.REASON_ATTRIBUTES_SETTING_UNREAD,
}
HEAD = "<?php\nuse App\\Controllers\\Api\\Tokens;\n"
CONTROLLERS = {
    "app/Controllers/C.php": (
        "<?php namespace App\\Controllers;\n"
        "class C { public function a() {} public function b() {} }\n"
    ),
    "app/Controllers/Admin/Users.php": (
        "<?php namespace App\\Controllers\\Admin;\nclass Users { public function index() {} }\n"
    ),
    "app/Controllers/Api/Tokens.php": (
        "<?php namespace App\\Controllers\\Api;\nclass Tokens { public function create() {} }\n"
    ),
}


def _lock(version: str | None) -> str:
    packages = [{"name": "codeigniter4/framework", "version": version}] if version else []
    return json.dumps({"packages": packages})


def collect(
    routes: str,
    *,
    version: str | None = "v4.5.4",
    routing: str | None = None,
    filters: str | None = None,
    shield: bool = True,
    extra: dict | None = None,
):
    require = {"codeigniter4/framework": "^4.5"}
    if shield:
        require["codeigniter4/shield"] = "^1.1"
    sources = dict(CONTROLLERS)
    sources["composer.json"] = json.dumps({"require": require})
    sources["composer.lock"] = _lock(version)
    sources["app/Config/Routes.php"] = HEAD + routes
    if routing is not None:
        sources["app/Config/Routing.php"] = routing
    if filters is not None:
        sources["app/Config/Filters.php"] = filters
    sources.update(extra or {})
    encoded = {k: v.encode("utf-8") for k, v in sources.items()}
    return CI.PROVIDER.collect(ProviderContext.from_sources(encoded))


def own(result) -> dict[str, int]:
    return {k: v for k, v in result.reasons.items() if k not in BASELINE}


def table(result) -> list[tuple]:
    return [
        (r.methods, r.path, r.controller_class.removeprefix(NS), r.controller_method)
        for r in result.routes
    ]


def guards_of(result, kind_filter=None) -> dict[str, list[tuple[str, str, str]]]:
    """Route path -> `(via, kind, label)` of its route guards, without required and csrf."""
    paths = {r.node_key: r.path for r in result.routes}
    found: dict[str, list[tuple[str, str, str]]] = {}
    for guard in result.route_guards:
        if guard.label in ("forcehttps", "pagecache", "csrf"):
            continue
        found.setdefault(paths[guard.route_key], []).append(
            (guard.via, guard.guard_kind, guard.label)
        )
    return found


def routing(**props: str) -> str:
    body = "".join(f"    public ${k} = {v};\n" for k, v in props.items())
    return f"<?php namespace Config;\nclass Routing extends BaseRouting {{\n{body}}}\n"


def filters_config(**props: str) -> str:
    body = "".join(f"    public array ${k} = {v};\n" for k, v in props.items())
    return f"<?php namespace Config;\nclass Filters extends BaseFilters {{\n{body}}}\n"


def test_is_a_registered_provider():
    assert isinstance(CI.PROVIDER, FrameworkProvider)
    assert CI.PROVIDER.name == "codeigniter"
    assert CI.PROVIDER.packages == ("codeigniter4/framework", "codeigniter4/codeigniter4")
    assert CI.PROVIDER in REGISTRY


# --- the fixture application ---------------------------------------------------------------


def fixture_result():
    root = str(FIXTURE)
    corpus = tuple(
        sorted(
            os.path.relpath(os.path.join(d, f), root)
            for d, _, files in os.walk(os.path.join(root, "app", "Controllers"))
            for f in files
        )
    )
    # `app/Config/*.php` are NOT in the corpus: they are read anyway.
    return CI.PROVIDER.collect(ProviderContext(root, corpus, file_reader(root)))


def test_fixture_routes():
    get, post = ("GET",), ("POST",)
    assert table(fixture_result()) == [
        (get, "/", "Home", "index"),
        (get, "/users/(:num)", "Users", "show"),
        (get, "/files/(:any)", "Files", "serve"),
        # docs-check ci4 B.5: `Admin\Users::index` has no leading `\`, so it is prefixed.
        (get, "/admin/users", "Admin\\Users", "index"),
        (get, "/legacy", "Legacy\\Old", "run"),
        # docs-check ci4 B.5: an array callable is fully qualified through the imports.
        (post, "/tokens", "Api\\Tokens", "create"),
        (get, "/ping", "", ""),
        (get, "/users/(:segment)", "Users", "other"),
        (get, "/account", "Account", "index"),
        (CI.HTTP_METHODS, "/contact", "Contact", "form"),
        (("GET", "POST"), "/search", "Search", "run"),
        (get, "/reports/daily", "Reports", "daily"),
        (get, "/reports/x/y", "Admin\\Users", "index"),
        (get, "/photos", "Photos", "index"),
        (get, "/photos/new", "Photos", "new"),
        (get, "/photos/(.*)/edit", "Photos", "edit"),
        (get, "/photos/(.*)", "Photos", "show"),
        (post, "/photos", "Photos", "create"),
        (("PUT",), "/photos/(.*)", "Photos", "update"),
        (("PATCH",), "/photos/(.*)", "Photos", "update"),
        (("DELETE",), "/photos/(.*)", "Photos", "delete"),
        (get, "/albums", "Albums", "index"),
        (get, "/albums/show/(:num)", "Albums", "show"),
        (get, "/albums/(:num)", "Albums", "show"),
        (get, "/panel", "Panel", "index"),
    ]


def test_fixture_reasons():
    result = fixture_result()
    assert result.reasons == {
        TEMPLATE_MULTI_SEGMENT: 6,
        CI.REASON_CLOSURE_ACTION: 1,
        CI.REASON_CLI_ROUTE: 1,
        CI.REASON_VIEW_ROUTE: 1,
        CI.REASON_REDIRECT_ROUTE: 1,
        CI.REASON_ENVIRONMENT_ROUTE: 1,
        CI.REASON_HOSTNAME_ROUTE: 1,
        # `#[Restrict]` on Panel: a route condition, not a guard.
        CI.REASON_ATTRIBUTE_RESTRICT: 1,
        # docs-check ci4 C.13: `service('auth')->routes($routes)`.
        CI.REASON_ROUTES_PASSED: 1,
        # docs-check ci4 A.3: `Routing::$autoRoute = true`.
        CI.REASON_AUTO_ROUTE: 1,
        # docs-check ci4 B.10: `AdminRoutes.php` repeats GET `users/(:num)`; the first wins.
        CI.REASON_DUPLICATE: 1,
        # `admin-only` before `users/1`: partial on `users/(:num)` and `users/(:segment)`.
        CI.REASON_FILTER_PATTERN_UNDECIDED: 2,
        # `$methods['POST']` on `contact` (any verb) and `search` (GET and POST).
        CI.REASON_FILTER_METHODS_PARTIAL: 2,
    }


def test_fixture_templates_and_handlers():
    by_path = {(r.path, r.methods): r for r in fixture_result().routes}
    users = by_path[("/users/(:num)", ("GET",))]
    assert (users.template, users.handler_file) == ("/users/{1}", "app/Controllers/Users.php")
    files = by_path[("/files/(:any)", ("GET",))]
    assert (files.template, files.template_reason) == ("", TEMPLATE_MULTI_SEGMENT)
    assert by_path[("/albums/show/(:num)", ("GET",))].template == "/albums/show/{1}"
    # A class no corpus file declares keeps its name, with no handler file.
    assert by_path[("/legacy", ("GET",))].handler_file == ""


def test_fixture_closure_route():
    ping = next(r for r in fixture_result().routes if r.path == "/ping")
    assert (ping.handler_kind, ping.handler_file) == ("closure", "app/Config/Routes.php")
    assert ping.node_key == "codeigniter:closure:app/Config/Routes.php:15#/ping"


def test_fixture_guards():
    result = fixture_result()
    found = guards_of(result)
    # A route `filter` option: Shield `session` is authentication. Filters.php `account*`
    # matches the literal `account`, so its config rule applies too.
    assert found["/account"] == [
        ("filter", "authentication", "session"),
        ("filter_config", "authentication", "session"),
    ]
    # docs-check ci4 B.8: 4.5.4 merges the group filter into the route's own.
    assert found["/reports/daily"] == [
        ("filter", "authorization", "group:admin"),
        ("filter", "authorization", "permission:reports.read"),
        ("filter_config", "authentication", "session"),
    ]
    assert found["/reports/x/y"] == [
        ("filter", "authorization", "group:admin"),
        ("filter_config", "authentication", "session"),
    ]
    # `$methods['POST']`: a POST-only route gets `honeypot`, a GET+POST one does not.
    assert found["/tokens"] == [("filter_config", "middleware", "honeypot")]
    assert "/search" not in found
    # Config rule nodes are keyed by rule index; route filters by kind and label.
    keys = {g.label: g.node_key for g in result.route_guards}
    assert keys["honeypot"] == "codeigniter:config#app/Config/Filters.php:3"
    assert keys["group:admin"] == "guard#codeigniter:authorization:group:admin"


def test_fixture_globals_except():
    # docs-check ci4 C.11: `csrf` with `except => ['api/*', 'tokens']`.
    result = fixture_result()
    paths = {r.node_key: r.path for r in result.routes}
    with_csrf = {paths[g.route_key] for g in result.route_guards if g.label == "csrf"}
    assert "/tokens" not in with_csrf
    assert {"/", "/users/(:num)", "/files/(:any)", "/photos/(.*)"} <= with_csrf
    assert len(with_csrf) == len(set(paths.values())) - 1


def test_fixture_undecided_pattern():
    # `admin-only` (a custom class, `unknown`) before `users/1`: partial on `users/(:num)`.
    result = fixture_result()
    assert result.route_undecided == {
        f"{NS}Users::show#/users/(:num)": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
        f"{NS}Users::other#/users/(:segment)": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    }
    assert result.undecided == 2
    assert not [g for g in result.route_guards if g.label == "admin-only"]


def test_fixture_controller_attributes():
    guards = fixture_result().guards
    assert [(g.holder_class.removeprefix(NS), g.holder_method, g.label, g.guard_kind,
             g.guard_attribute) for g in guards] == [
        ("Account", "index", "group:admin,superadmin", "authorization", "Filter"),
    ]  # fmt: skip
    # A `#[Filter]` shares the node of the same route filter.
    assert guards[0].node_key == "guard#codeigniter:authorization:group:admin,superadmin"


# --- namespaces and handlers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("routes", "routing_file", "expected", "reason"),
    [
        ("$routes->get('x', 'C::a');", None, "App\\Controllers\\C", None),
        # docs-check ci4 B.5: a `\` past the start does not make it qualified.
        ("$routes->get('x', 'Admin\\Users::index');", None, "App\\Controllers\\Admin\\Users", None),
        ("$routes->get('x', '\\App\\Controllers\\C::a');", None, "App\\Controllers\\C", None),
        ("$routes->get('x', 'C::a', ['namespace' => 'Other']);", None, "Other\\C", None),
        (
            "$routes->setDefaultNamespace('Mod\\Controllers');\n$routes->get('x', 'C::a');",
            None,
            "Mod\\Controllers\\C",
            None,
        ),
        (
            "$routes->get('x', 'C::a');",
            "public string $defaultNamespace = 'Site';",
            "Site\\C",
            None,
        ),
        (
            "$routes->setDefaultNamespace($ns);\n$routes->get('x', 'C::a');",
            None,
            None,
            CI.REASON_NAMESPACE_NOT_LITERAL,
        ),
        (
            "$routes->get('x', 'C::a', ['namespace' => $ns]);",
            None,
            None,
            CI.REASON_NAMESPACE_NOT_LITERAL,
        ),
        # docs-check ci4 B.5: an array callable ignores the namespace option.
        (
            "$routes->get('x', [Tokens::class, 'create'], ['namespace' => 'Other']);",
            None,
            "App\\Controllers\\Api\\Tokens",
            None,
        ),
        (
            "$routes->get('x/(:num)', [[Tokens::class, 'create'], '$1']);",
            None,
            "App\\Controllers\\Api\\Tokens",
            None,
        ),
        ("$routes->get('x', 'C');", None, None, CI.REASON_HANDLER_NO_METHOD),
        ("$routes->get('x', 'C::$1');", None, None, CI.REASON_HANDLER_DYNAMIC),
        ("$routes->get('x', $to);", None, None, CI.REASON_HANDLER_NOT_LITERAL),
        ("$routes->get('x', [$c, 'a']);", None, None, CI.REASON_HANDLER_NOT_LITERAL),
    ],
)
def test_handler_forms(routes, routing_file, expected, reason):
    props = (
        f"<?php namespace Config;\nclass Routing {{ {routing_file} }}\n" if routing_file else None
    )
    result = collect(routes + "\n", routing=props)
    if expected is None:
        assert result.routes == []
        assert own(result) == {reason: 1}
        return
    assert [r.controller_class for r in result.routes] == [expected]
    assert own(result) == {}


# --- declaration forms ---------------------------------------------------------------------


def test_verbs_match_add_map():
    result = collect(
        "$routes->head('h', 'C::a');\n$routes->options('o', 'C::a');\n"
        "$routes->match(['PUT', 'patch'], 'm', 'C::a');\n"
        "$routes->map(['one' => 'C::a', 'two' => 'C::b']);\n"
    )
    assert [(r.methods, r.path) for r in result.routes] == [
        (("HEAD",), "/h"),
        (("OPTIONS",), "/o"),
        (("PUT", "PATCH"), "/m"),
        (CI.HTTP_METHODS, "/one"),
        (CI.HTTP_METHODS, "/two"),
    ]
    assert own(result) == {}


@pytest.mark.parametrize(
    ("routes", "reason"),
    [
        ("$routes->cli('x', 'C::a');", CI.REASON_CLI_ROUTE),
        ("$routes->view('x', 'page');", CI.REASON_VIEW_ROUTE),
        ("$routes->addRedirect('x', 'y');", CI.REASON_REDIRECT_ROUTE),
        ("$routes->get($path, 'C::a');", CI.REASON_PATH_NOT_LITERAL),
        ("$routes->match($verbs, 'x', 'C::a');", CI.REASON_VERBS_NOT_LITERAL),
        ("$routes->get('x', 'C::a', $options);", CI.REASON_OPTIONS_NOT_LITERAL),
        ("$routes->get('x', 'C::a', ['redirect' => 302]);", CI.REASON_OPTION_UNKNOWN),
        ("$routes->macro('x');", CI.REASON_METHOD_UNKNOWN),
        ("if ($x) { $routes->get('x', 'C::a'); }", CI.REASON_IN_CONTROL_FLOW),
        ("register($routes);", CI.REASON_ROUTES_PASSED),
        ("require $file;", CI.REASON_REQUIRE_NOT_LITERAL),
        ("$routes->group($name, static function ($routes) {});", CI.REASON_GROUP_NAME_NOT_LITERAL),
        ("$routes->group('g', $callback);", CI.REASON_GROUP_CALLBACK_UNREAD),
    ],
)
def test_refused_forms(routes, reason):
    result = collect(routes + "\n")
    assert result.routes == []
    assert own(result) == {reason: 1}


def test_closure_routes():
    result = collect(
        "$routes->get('a', function () { return 1; });\n$routes->post('b', static fn () => 2);\n"
    )
    assert [(r.handler_kind, r.handler_file, r.controller_class) for r in result.routes] == [
        ("closure", "app/Config/Routes.php", ""),
        ("closure", "app/Config/Routes.php", ""),
    ]
    assert own(result) == {CI.REASON_CLOSURE_ACTION: 2}


def test_route_files_follow_routing_config():
    # docs-check ci4 A.1: `$routeFiles`, read in order; a file not listed is not read.
    extra = {
        "app/Config/Extra.php": "<?php\n$routes->get('extra', 'C::a');\n",
        "app/Config/Unlisted.php": "<?php\n$routes->get('unlisted', 'C::a');\n",
    }
    listed = routing(routeFiles="[APPPATH . 'Config/Extra.php', APPPATH . 'Config/Routes.php']")
    result = collect("$routes->get('main', 'C::b');\n", routing=listed, extra=extra)
    assert [r.path for r in result.routes] == ["/extra", "/main"]
    unread = routing(routeFiles="[APPPATH . 'Config/Routes.php', $more]")
    result = collect("$routes->get('main', 'C::b');\n", routing=unread, extra=extra)
    assert result.routes == []
    assert own(result) == {CI.REASON_ROUTE_FILES_NOT_LITERAL: 1}


def test_require_reads_the_file_in_scope():
    extra = {"app/Config/More.php": "<?php\n$routes->get('more', 'C::a');\n"}
    result = collect(
        "$routes->group('g', static function ($routes) {\n"
        "    require APPPATH . 'Config/More.php';\n"
        "});\n",
        extra=extra,
    )
    assert [r.path for r in result.routes] == ["/g/more"]


# --- placeholders --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "template", "reason"),
    [
        ("p/(:num)/(:alpha)", "/p/{1}/{2}", ""),
        ("p/(:segment)/(:alphanum)/(:hash)", "/p/{1}/{2}/{3}", ""),
        ("p/(:any)", "", TEMPLATE_MULTI_SEGMENT),
        ("p/([0-9]+)", "/p/{1}", ""),
        ("p/(.+)", "", TEMPLATE_MULTI_SEGMENT),
        ("feed.xml", "", TEMPLATE_MULTI_SEGMENT),
        ("p/[a-z]+", "", TEMPLATE_UNPARSED),
        ("{locale}/p", "/{locale}/p", ""),
    ],
)
def test_placeholder_templates(path, template, reason):
    result = collect(f"$routes->get('{path}', 'C::a');\n")
    route = result.routes[0]
    assert (route.path, route.template, route.template_reason) == ("/" + path, template, reason)


def test_custom_placeholder_must_come_first():
    # docs-check ci4 B.6: `addPlaceholder()` "must be called before you add the route".
    result = collect(
        "$routes->get('early/(:uuid)', 'C::a');\n"
        "$routes->addPlaceholder('uuid', '[0-9a-f-]+');\n"
        "$routes->get('late/(:uuid)', 'C::b');\n"
    )
    assert [(r.path, r.template) for r in result.routes] == [
        ("/early/(:uuid)", ""),
        ("/late/(:uuid)", "/late/{1}"),
    ]
    assert own(result) == {CI.REASON_PLACEHOLDER_UNKNOWN: 1, TEMPLATE_UNPARSED: 1}
    unread = collect("$routes->addPlaceholder($n, $p);\n$routes->get('x/(:num)', 'C::a');\n")
    assert unread.routes[0].template == ""
    assert own(unread) == {CI.REASON_PLACEHOLDER_NOT_LITERAL: 1, TEMPLATE_UNPARSED: 1}


# --- groups and versions -------------------------------------------------------------------

GROUPS = (
    "$routes->group('admin', ['filter' => 'group:admin'], static function ($routes) {\n"
    "    $routes->group('users', ['filter' => 'session'], static function ($routes) {\n"
    "        $routes->get('list', 'C::a', ['filter' => 'permission:users.read']);\n"
    "        $routes->get('/', 'C::b');\n"
    "    });\n"
    "});\n"
)


def test_group_prefix():
    # docs-check ci4 B.8: `trim($oldGroup . '/' . $name, '/')`; `/` inside is the group itself.
    result = collect(GROUPS)
    assert [r.path for r in result.routes] == ["/admin/users/list", "/admin/users"]
    empty = collect(
        "$routes->group('', static function ($routes) { $routes->get('x', 'C::a'); });\n"
    )
    assert [r.path for r in empty.routes] == ["/x"]


def test_group_filters_merge_from_4_5_4():
    found = guards_of(collect(GROUPS, version="v4.5.4"))
    assert found["/admin/users/list"] == [
        ("filter", "authorization", "group:admin"),
        ("filter", "authentication", "session"),
        ("filter", "authorization", "permission:users.read"),
    ]
    assert found["/admin/users"] == [
        ("filter", "authorization", "group:admin"),
        ("filter", "authentication", "session"),
    ]


@pytest.mark.parametrize(
    ("version", "reason"),
    [("v4.5.3", CI.REASON_GROUP_FILTER_VERSION), (None, CI.REASON_VERSION_UNREAD)],
)
def test_group_filters_are_not_claimed_before_4_5_4(version, reason):
    result = collect(GROUPS, version=version)
    found = guards_of(result)
    # Only the route's own filter is proved; group `group:admin` may apply but is unclaimed.
    assert found == {"/admin/users/list": [("filter", "authorization", "permission:users.read")]}
    assert reason in own(result)
    assert result.route_undecided == {
        f"{NS}C::b#/admin/users": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
        f"{NS}C::a#/admin/users/list": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    }


NESTED_OPTIONS = (
    "$routes->group('g', ['namespace' => 'Outer'], static function ($routes) {\n"
    "    $routes->group('h', ['as' => 'inner'], static function ($routes) {\n"
    "        $routes->get('x', 'C::a');\n"
    "    });\n"
    "});\n"
)


@pytest.mark.parametrize(
    ("version", "expected", "reasons"),
    [
        # docs-check ci4 B.8: from 4.5.0 the outer options merge into the inner group.
        ("v4.5.0", "Outer\\C", {}),
        # Before 4.5.0 the inner options replace the outer ones: the default namespace.
        ("v4.4.8", "App\\Controllers\\C", {}),
        (None, None, {CI.REASON_VERSION_UNREAD: 1, CI.REASON_NAMESPACE_NOT_LITERAL: 1}),
    ],
)
def test_group_option_inheritance_by_version(version, expected, reasons):
    result = collect(NESTED_OPTIONS, version=version)
    assert [r.controller_class for r in result.routes] == ([expected] if expected else [])
    assert own(result) == reasons


def test_multiple_route_filters_before_4_5():
    routes = "$routes->get('x', 'C::a', ['filter' => ['session', 'group:admin']]);\n"
    assert guards_of(collect(routes, version="v4.5.0")) == {
        "/x": [("filter", "authentication", "session"), ("filter", "authorization", "group:admin")]
    }
    old = collect(routes, version="v4.4.0")
    assert guards_of(old) == {}
    assert own(old) == {CI.REASON_FILTER_MULTIPLE_VERSION: 1}
    assert old.route_undecided == {f"{NS}C::a#/x": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED}


def test_route_filter_forms():
    result = collect(
        "$routes->get('a', 'C::a', ['filter' => 'admin-auth:dual,noreturn']);\n"
        "$routes->get('b', 'C::a',"
        " ['filter' => \\CodeIgniter\\Shield\\Filters\\GroupFilter::class]);\n"
        "$routes->get('c', 'C::a', ['filter' => $f]);\n"
    )
    assert guards_of(result) == {
        # Args stay in the label; an alias nothing maps is `unknown`.
        "/a": [("filter", "unknown", "admin-auth:dual,noreturn")],
        "/b": [("filter", "authorization", "CodeIgniter\\Shield\\Filters\\GroupFilter")],
    }
    assert own(result) == {CI.REASON_FILTER_NOT_LITERAL: 1}
    assert result.route_undecided == {f"{NS}C::a#/c": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED}


# --- resource and presenter ----------------------------------------------------------------


def test_resource_table_websafe_and_options():
    # docs-check ci4 B.9: the resource table, `websafe`, `controller`, `placeholder`, `except`.
    result = collect(
        "$routes->resource('admin/photos', ['websafe' => 1, 'placeholder' => '(:num)',"
        " 'except' => 'new,edit']);\n"
        "$routes->resource('tags', ['controller' => 'Labels', 'only' => ['index']]);\n"
    )
    assert [(r.methods, r.path, r.controller_class.removeprefix(NS), r.controller_method,
             r.template) for r in result.routes] == [
        (("GET",), "/admin/photos", "Admin\\Photos", "index", "/admin/photos"),
        (("GET",), "/admin/photos/(:num)", "Admin\\Photos", "show", "/admin/photos/{1}"),
        (("POST",), "/admin/photos", "Admin\\Photos", "create", "/admin/photos"),
        (("PUT",), "/admin/photos/(:num)", "Admin\\Photos", "update", "/admin/photos/{1}"),
        (("PATCH",), "/admin/photos/(:num)", "Admin\\Photos", "update", "/admin/photos/{1}"),
        (("DELETE",), "/admin/photos/(:num)", "Admin\\Photos", "delete", "/admin/photos/{1}"),
        (("POST",), "/admin/photos/(:num)/delete", "Admin\\Photos", "delete",
         "/admin/photos/{1}/delete"),
        # websafe POST `photos/$id` for update: POST `photos/(:num)` was free.
        (("POST",), "/admin/photos/(:num)", "Admin\\Photos", "update", "/admin/photos/{1}"),
        (("GET",), "/tags", "Labels", "index", "/tags"),
    ]  # fmt: skip
    assert own(result) == {}


def test_resource_default_id_may_match_a_slash():
    # docs-check ci4 B.9: the source's `$defaultPlaceholder = 'any'` (`.*`), not `(:segment)`.
    result = collect("$routes->resource('photos', ['only' => 'show']);\n")
    route = result.routes[0]
    assert (route.path, route.template, route.template_reason) == (
        "/photos/(.*)", "", TEMPLATE_MULTI_SEGMENT,
    )  # fmt: skip
    constrained = collect(
        "$routes->setDefaultConstraint('num');\n$routes->resource('photos', ['only' => 'show']);\n"
    )
    assert constrained.routes[0].template == "/photos/{1}"


def test_presenter_table():
    result = collect("$routes->presenter('photos', ['placeholder' => '(:num)']);\n")
    assert [(r.methods[0], r.path.removeprefix("/photos"), r.controller_method)
            for r in result.routes] == [
        ("GET", "", "index"),
        ("GET", "/show/(:num)", "show"),
        ("GET", "/new", "new"),
        ("POST", "/create", "create"),
        ("GET", "/edit/(:num)", "edit"),
        ("POST", "/update/(:num)", "update"),
        ("GET", "/remove/(:num)", "remove"),
        ("POST", "/delete/(:num)", "delete"),
        ("GET", "/(:num)", "show"),
        ("POST", "", "create"),
    ]  # fmt: skip
    refused = collect("$routes->presenter('photos', ['websafe' => true]);\n")
    assert refused.routes == []
    assert own(refused) == {CI.REASON_OPTION_UNKNOWN: 1}


def test_resource_id_presenter_paths_and_websafe_isset():
    # `protected $defaultPlaceholder = 'any';` (system/Router/RouteCollection.php:67) and
    # `$id = $options['placeholder'] ?? $this->placeholders[$this->defaultPlaceholder] ...`
    # (:759), wrapped as `'(' . trim($id, '()') . ')'` (:762): the default id is `(.*)`.
    show = collect("$routes->resource('photos', ['only' => 'show']);\n")
    assert [r.path for r in show.routes] == ["/photos/(.*)"]
    # presenter() registers `show` on `name/show/ID` (:874) and `name/ID` (:895), `create` on
    # `name/create` (:880) and `name` (:898).
    presenter = collect("$routes->presenter('p', ['only' => 'show,create']);\n")
    assert [(r.methods[0], r.path, r.controller_method) for r in presenter.routes] == [
        ("GET", "/p/show/(.*)", "show"),
        ("POST", "/p/create", "create"),
        ("GET", "/p/(.*)", "show"),
        ("POST", "/p", "create"),
    ]
    # `isset($options['websafe'])` (:800): `false` turns it on, `null` does not.
    for value, extra in (("false", 1), ("null", 0)):
        websafe = collect(f"$routes->resource('r', ['only' => 'delete', 'websafe' => {value}]);\n")
        assert len(websafe.routes) == 1 + extra


# --- duplicates and conditional routes -----------------------------------------------------


def test_duplicates_first_wins():
    result = collect(
        "$routes->get('x', 'C::a');\n$routes->get('x', 'C::b');\n"
        "$routes->match(['get', 'post'], 'x', 'C::b');\n"
        # Same regex once placeholders are replaced: `(:hash)` and `(:segment)` are `[^/]+`.
        "$routes->get('y/(:segment)', 'C::a');\n$routes->get('y/(:hash)', 'C::b');\n"
    )
    assert [(r.methods, r.path, r.controller_method) for r in result.routes] == [
        (("GET",), "/x", "a"),
        # Only POST is still free for the `match()` route.
        (("POST",), "/x", "b"),
        (("GET",), "/y/(:segment)", "a"),
    ]
    assert own(result) == {CI.REASON_DUPLICATE: 3}


def test_verb_route_beats_any_route_and_inherits_its_filter():
    # `$this->routes[$verb] + ($this->routes['*'] ?? [])` (system/Router/RouteCollection.php:504):
    # a PHP union, so the verb table's entry for a key wins whatever the order, and the `*`
    # route serves the remaining verbs. `loadRoutesOptions()` (:1568-1575) fills the verb
    # route's missing option keys from the `*` route's: GET has no `filter`, so it runs
    # `session`; POST has its own `filter` key and keeps it.
    result = collect(
        "$routes->add('x', 'C::a', ['filter' => 'session']);\n$routes->get('x', 'C::b');\n"
        "$routes->post('x', '\\App\\Controllers\\Admin\\Users::index', ['filter' => 'honeypot']);\n"
    )
    rest = tuple(m for m in CI.HTTP_METHODS if m not in ("GET", "POST"))
    assert [(r.methods, r.controller_method) for r in result.routes] == [
        (rest, "a"),
        (("GET",), "b"),
        (("POST",), "index"),
    ]
    assert {(g.route_key, g.label) for g in result.route_guards} == {
        (f"{NS}C::a#/x", "session"),
        (f"{NS}C::b#/x", "session"),
        (f"{NS}Admin\\Users::index#/x", "honeypot"),
    }
    assert own(result) == {}
    assert result.route_undecided == {}


def test_view_before_a_get_route_refuses_its_get():
    # `view()` is `$this->create(Method::GET, $from, $to, $routeOptions)`
    # (system/Router/RouteCollection.php:1060), and `create()` returns when the key already
    # exists in that verb table (:1413-1414). A later GET is never registered; POST is.
    result = collect(
        "$routes->view('x', 'page');\n$routes->get('x', 'C::a');\n"
        "$routes->match(['get', 'post'], 'x', 'C::b');\n"
    )
    assert [(r.methods, r.path, r.controller_method) for r in result.routes] == [
        (("POST",), "/x", "b"),
    ]
    assert own(result) == {CI.REASON_VIEW_ROUTE: 1, CI.REASON_VIEW_COLLISION: 2}


def test_get_route_before_a_view_keeps_its_get():
    # Same first-wins return (system/Router/RouteCollection.php:1413-1414): the view is dropped.
    result = collect("$routes->get('x', 'C::a');\n$routes->view('x', 'page');\n")
    assert [(r.methods, r.path, r.controller_method) for r in result.routes] == [
        (("GET",), "/x", "a"),
    ]
    assert own(result) == {CI.REASON_VIEW_ROUTE: 1}


@pytest.mark.parametrize(
    "routes",
    [
        "$routes->add('x', 'C::a');\n$routes->view('x', 'page');\n",
        "$routes->view('x', 'page');\n$routes->add('x', 'C::a');\n",
    ],
)
def test_view_takes_get_from_an_any_route_in_either_order(routes):
    # `$this->routes[$verb] + ($this->routes['*'] ?? [])` (system/Router/RouteCollection.php:504):
    # the view's GET table entry wins whatever the order; the `*` route keeps the other verbs.
    result = collect(routes)
    rest = tuple(m for m in CI.HTTP_METHODS if m != "GET")
    assert [(r.methods, r.controller_method) for r in result.routes] == [(rest, "a")]
    assert own(result) == {CI.REASON_VIEW_ROUTE: 1, CI.REASON_VIEW_COLLISION: 1}


@pytest.mark.parametrize("options", ["['hostname' => 'a.test']", "$options"])
def test_view_that_may_be_hostname_bound_refuses_a_get_route(options):
    # A `hostname` view sets `$overwrite = true` (system/Router/RouteCollection.php:1354), so it
    # replaces an earlier GET route for that host; unread options may hold `hostname`.
    result = collect(f"$routes->get('x', 'C::a');\n$routes->view('x', 'page', {options});\n")
    assert result.routes == []
    assert own(result) == {CI.REASON_VIEW_ROUTE: 1, CI.REASON_VIEW_COLLISION: 1}


@pytest.mark.parametrize(
    "routes",
    [
        "$routes->get('x', 'C::a');\n$routes->addRedirect('x', 'y');\n"
        "$routes->post('x', 'C::b');\n",
        "$routes->addRedirect('x', 'y');\n$routes->get('x', 'C::a');\n"
        "$routes->post('x', 'C::b');\n",
        "$routes->addRedirect('x', 'y');\n$routes->add('x', 'C::a');\n"
        "$routes->post('x', 'C::b');\n",
    ],
)
def test_redirect_refuses_every_route_on_its_key(routes):
    # `addRedirect()` is `$this->create('*', $from, $redirectTo, ['redirect' => $status])`
    # (system/Router/RouteCollection.php:610), which sets `$this->routes['*'][$routeKey]
    # ['redirect']` (:1428). `checkRoutes()` asks `isRedirect($routeKey)` (Router.php:461) for
    # the matched route of any verb, and `isRedirect()` reads only that `*` entry (:617).
    result = collect(routes)
    assert result.routes == []
    assert own(result) == {CI.REASON_REDIRECT_ROUTE: 1, CI.REASON_REDIRECT_COLLISION: 2}


def test_any_route_before_a_redirect_cancels_it():
    # The `*` table already holds the key, so `create()` returns (RouteCollection.php:1413-1414)
    # before it sets the `redirect` entry (:1428): nothing on the key redirects.
    result = collect(
        "$routes->add('x', 'C::a');\n$routes->addRedirect('x', 'y');\n$routes->get('x', 'C::b');\n"
    )
    rest = tuple(m for m in CI.HTTP_METHODS if m != "GET")
    assert [(r.methods, r.controller_method) for r in result.routes] == [
        (rest, "a"),
        (("GET",), "b"),
    ]
    assert own(result) == {CI.REASON_REDIRECT_ROUTE: 1}


def test_any_route_options_fill_only_missing_keys():
    # :1568-1575 `array_diff_key($val, $options[$key])`: a group `filter` is a key of the verb
    # route's options from 4.5.0 (group(), RouteCollection.php:686-689), so nothing is inherited.
    routes = (
        "$routes->add('x', 'C::a', ['filter' => 'session']);\n"
        "$routes->group('', ['filter' => 'honeypot'], static function ($routes) {\n"
        "    $routes->get('x', 'C::b');\n});\n"
    )
    new = collect(routes)
    assert {g.label for g in new.route_guards if g.route_key == f"{NS}C::b#/x"} == {"honeypot"}
    assert own(new) == {}
    # Before 4.5.0 the inner-replaces rule leaves the key's presence unread.
    old = collect(routes, version="v4.4.0")
    assert old.reasons[CI.REASON_ANY_OPTIONS_UNDECIDED] == 1
    assert old.route_undecided[f"{NS}C::b#/x"] == AUTHORIZATION_ACCESS_CONTROL_UNDECIDED
    # A hostname `*` route is in the `*` table only on a matching host: no claim either way.
    host = collect(
        "$routes->add('x', 'C::a', ['hostname' => 'h.test', 'filter' => 'session']);\n"
        "$routes->get('x', 'C::b');\n"
    )
    assert [(len(r.methods), r.controller_method) for r in host.routes] == [(8, "a"), (1, "b")]
    assert own(host) == {CI.REASON_HOSTNAME_ROUTE: 1, CI.REASON_ANY_OPTIONS_UNDECIDED: 1}
    assert host.route_undecided[f"{NS}C::b#/x"] == AUTHORIZATION_ACCESS_CONTROL_UNDECIDED


def test_environment_and_hostname_routes():
    # docs-check ci4 B.10: `environment()` runs only in that environment; hostname is runtime.
    result = collect(
        "$routes->environment('development', static function ($routes) {\n"
        "    $routes->get('debug', 'C::a');\n    $routes->get('shared', 'C::a');\n});\n"
        "$routes->get('shared', 'C::b');\n"
        "$routes->get('host', 'C::a', ['subdomain' => 'admin']);\n"
    )
    assert [r.path for r in result.routes] == ["/host"]
    assert own(result) == {
        CI.REASON_ENVIRONMENT_ROUTE: 2,
        CI.REASON_SUBDOMAIN_ROUTE: 1,
        CI.REASON_DUPLICATE_CONDITIONAL: 1,
    }


def test_codeigniter_c7_conditions_specs_and_match_verbs():
    # `subdomain` is an `elseif` after `hostname` (system/Router/RouteCollection.php:1357):
    # with both set only the hostname condition is counted. The route is published, no
    # precedence claimed: a plain GET on its key is refused.
    both = collect(
        "$routes->get('h', 'C::a', ['hostname' => 'a.test', 'subdomain' => 'x']);\n"
        "$routes->get('h', 'C::b');\n"
    )
    assert [r.controller_method for r in both.routes] == ["a"]
    assert own(both) == {CI.REASON_HOSTNAME_ROUTE: 1, CI.REASON_DUPLICATE_CONDITIONAL: 1}
    # `[$alias, $arguments] = explode(':', $filter)` (system/Filters/Filters.php:615) drops a
    # second `:`: the spec is unread, no guard, and the route is undecided.
    spec = collect("$routes->get('s', 'C::a', ['filter' => 'group:admin:x']);\n")
    assert spec.route_guards == []
    assert own(spec) == {CI.REASON_FILTER_SPEC_UNREAD: 1}
    assert spec.route_undecided == {f"{NS}C::a#/s": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED}
    config = filters_config(filters="['permission:a:b' => ['before' => ['*']]]")
    rule = collect("$routes->get('s', 'C::a');\n", filters=config)
    assert guards_of(rule) == {}
    assert own(rule) == {CI.REASON_FILTER_SPEC_UNREAD: 1}
    assert rule.route_undecided == {f"{NS}C::a#/s": AUTHORIZATION_ACCESS_CONTROL_UNDECIDED}
    # `$this->{$verb}(...)` (RouteCollection.php:935): there is no `trace()` or `connect()`.
    for verb in ("TRACE", "connect"):
        refused = collect(f"$routes->match(['GET', '{verb}'], 'm', 'C::a');\n")
        assert refused.routes == []
        assert own(refused) == {CI.REASON_MATCH_VERB_NO_METHOD: 1}


def test_auto_route():
    on = collect("$routes->setAutoRoute(true);\n")
    assert own(on) == {CI.REASON_AUTO_ROUTE: 1}
    off = collect("$routes->setAutoRoute(false);\n", routing=routing(autoRoute="true"))
    assert own(off) == {}


# --- Config\Filters ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "verdict"),
    [
        ("admin/*", "admin/(:num)", "all"),
        ("admin*", "admin", "all"),
        ("*", "(:any)", "all"),
        ("admin/*", "admin", "none"),
        ("api/*", "users/(:num)", "none"),
        ("users/1", "users/(:num)", "undecided"),
        ("users/*/edit", "users/(:num)/edit", "undecided"),
        ("users/*/edit", "users/(:num)/show", "none"),
        ("Admin/Users", "admin/users", "all"),
        ("admin/(:num)", "admin/(:num)", "undecided"),
        # Filters.php:909-913: only `*` is rewritten; every other regex mark stays live.
        ("admin/user.", "admin/users", "undecided"),
        ("admin/(users|tags)", "admin/users", "undecided"),
        ("admin/u#", "admin/users", "undecided"),
        # `\A...\z` anchors both ends: a prefix alone does not match.
        ("admin", "admin/users", "none"),
        ("/Admin/ ", "admin", "all"),
    ],
)
def test_filter_pattern_matching(pattern, path, verdict):
    # docs-check ci4 C.11: relative to baseURL, `*` is `.*`, lower-cased.
    assert CI.covers(path.lower(), [pattern]) == verdict


def test_empty_filters_path_matches_all_and_empty_except_matches_none():
    # `if ($paths === '' || $paths === []) { return true; }` (system/Filters/Filters.php:862)
    # versus `if ($paths === []) { return false; }` for `except` (:885).
    for empty in ("[]", "''"):
        config = filters_config(
            filters=f"['session' => ['before' => {empty}]]",
            globals="['before' => ['honeypot' => ['except' => []]]]",
        )
        assert guards_of(collect("$routes->get('a/b', 'C::a');\n", filters=config)) == {
            "/a/b": [
                ("filter_config", "middleware", "honeypot"),
                ("filter_config", "authentication", "session"),
            ]
        }
    # `['']` is one pattern, `#\A\z#u`: it matches only the root.
    config = filters_config(filters="['session' => ['before' => ['']]]")
    assert guards_of(collect("$routes->get('a', 'C::a');\n", filters=config)) == {}


def test_filters_shield_kinds_and_remapped_alias():
    config = filters_config(
        aliases="['permission' => \\App\\Filters\\Mine::class]",
        filters="['session' => ['before' => ['*']], 'group:admin' => ['before' => ['*']],"
        " 'permission' => ['before' => ['*']], 'jwt' => ['before' => ['*']]]",
    )
    found = guards_of(collect("$routes->get('x', 'C::a');\n", filters=config))
    assert found == {
        "/x": [
            ("filter_config", "authentication", "session"),
            ("filter_config", "authorization", "group:admin"),
            # An application alias pointing elsewhere is `unknown`: it may do anything.
            ("filter_config", "unknown", "permission"),
            ("filter_config", "authentication", "jwt"),
        ]
    }
    no_shield = guards_of(collect("$routes->get('x', 'C::a');\n", filters=config, shield=False))
    assert {kind for _, kind, _ in no_shield["/x"]} == {"unknown"}


def test_filters_methods_and_required_by_version():
    config = filters_config(
        required="['before' => ['forcehttps', 'cors']]",
        methods="['post' => ['honeypot']]",
    )
    routes = "$routes->post('x', 'C::a');\n"
    new = collect(routes, filters=config, version="v4.5.0")
    # The deprecated lowercase fallback (system/Filters/Filters.php:733-742) still applies a
    # lowercase key when no exact `POST` key exists.
    assert [(g.label, g.guard_kind) for g in new.route_guards] == [
        ("forcehttps", "middleware"),
        ("cors", "middleware"),
        ("honeypot", "middleware"),
    ]
    assert own(new) == {}
    old = collect(routes, filters=config, version="v4.4.0")
    assert [g.label for g in old.route_guards] == ["honeypot"]
    assert own(old) == {CI.REASON_FILTER_REQUIRED_VERSION: 1}


def test_filter_order_sections_and_methods_keys():
    # `initialize()` (system/Filters/Filters.php:476-478) prepends `$filters`, `$methods`,
    # `$globals` to the route filters enabled first (system/CodeIgniter.php:478): a route
    # `filter` adds to the config rules and never replaces them.
    config = filters_config(
        globals="['before' => ['session']]",
        methods="['GET' => ['honeypot'], 'get' => ['cors'], 'Get' => ['invalidchars']]",
        filters="['secureheaders' => ['before' => ['x']]]",
    )
    result = collect("$routes->get('x', 'C::a', ['filter' => 'group:admin']);\n", filters=config)
    # `array_key_exists($method, ...)` (:727) takes the exact key; the lowercase fallback
    # (:733) runs only when it is missing; `Get` never matches.
    assert sorted(guards_of(result)["/x"]) == [
        ("filter", "authorization", "group:admin"),
        ("filter_config", "authentication", "session"),
        ("filter_config", "middleware", "honeypot"),
        ("filter_config", "middleware", "secureheaders"),
    ]
    assert own(result) == {}


METHODS_ONLY_ROUTES = (
    "$routes->post('x', 'C::a');\n"
    "$routes->post('y', 'C::b', ['filter' => 'permission:p']);\n"
    "$routes->post('z', 'C::b');\n"
)
METHODS_ONLY_FILTERS = filters_config(
    methods="['POST' => ['group:admin']]",
    filters="['group:z' => ['before' => ['z']]]",
)


def test_methods_filter_alone_leaves_the_route_undecided():
    # `processMethods()` reads `$method = $this->request->getMethod();`
    # (system/Filters/Filters.php:723) and looks it up as is, while `setHTTPVerb()` routes on
    # `strtoupper($verb)` (system/Router/RouteCollection.php:554, called at Router.php:166):
    # a `post` request reaches the POST route and skips `$methods['POST']`.
    result = collect(METHODS_ONLY_ROUTES, filters=METHODS_ONLY_FILTERS)
    keys = {r.path: r.node_key for r in result.routes}
    # The guard stays published with its kind; it does not decide `/x`.
    assert guards_of(result)["/x"] == [("filter_config", "authorization", "group:admin")]
    assert [(g.label, g.decides) for g in result.route_guards if g.route_key == keys["/x"]] == [
        ("group:admin", False)
    ]
    # A route filter or a `$filters` rule decides `/y` and `/z` as usual.
    assert result.route_undecided == {keys["/x"]: AUTHORIZATION_ACCESS_CONTROL_UNDECIDED}
    assert own(result) == {CI.REASON_METHODS_CASE_BYPASS: 1}


def test_methods_filter_alone_is_not_covered_through_the_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    sources = dict(CONTROLLERS)
    sources["composer.json"] = json.dumps(
        {"require": {"codeigniter4/framework": "^4.5", "codeigniter4/shield": "^1.1"}}
    )
    sources["composer.lock"] = _lock("v4.5.4")
    sources["app/Config/Routes.php"] = HEAD + METHODS_ONLY_ROUTES
    sources["app/Config/Filters.php"] = METHODS_ONLY_FILTERS
    for rel, text in sources.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    controller = os.path.realpath(tmp_path / "app" / "Controllers" / "C.php")
    all_nodes, all_edges = _graph([(controller, NS + "C", ["a", "b"])])
    record = resolver.run_routes(
        [{"nodes": [{"source_file": controller}]}],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource((CI.PROVIDER,))],
        repo_root=str(tmp_path),
    )
    assert record.state == "completed", record.reason
    routes = {n["label"]: n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND}
    assert (routes["/x"]["authorization_state"], routes["/x"]["authorization_reason"]) == (
        "undecided",
        AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    )
    assert routes["/y"]["authorization_state"] == "covered"
    assert routes["/z"]["authorization_state"] == "covered"
    guards = {n["id"]: n for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    edges = [
        guards[e["target"]]
        for e in all_edges
        if e.get("relation") == GUARD_RELATION and e["source"] == routes["/x"]["id"]
    ]
    assert [(g["label"], g["guard_kind"]) for g in edges] == [("group:admin", "authorization")]
    assert record.authorization_reason == {AUTHORIZATION_ACCESS_CONTROL_UNDECIDED: 1}
    assert record.unresolved["tree_sitter_routes:" + CI.REASON_METHODS_CASE_BYPASS] == 1


def test_filters_after_only_and_unreadable_config():
    config = filters_config(filters="['session' => ['after' => ['*']]]")
    assert collect("$routes->get('x', 'C::a');\n", filters=config).route_guards == []
    blocked = collect(
        "$routes->get('x', 'C::a');\n", filters=filters_config(globals="self::GLOBALS")
    )
    assert own(blocked) == {CI.REASON_FILTERS_CONFIG_UNREAD: 1}
    assert blocked.route_undecided == {f"{NS}C::a#/x": AUTHORIZATION_ACCESS_CONTROL_BLOCKED}


# --- controller attributes -----------------------------------------------------------------

ATTRIBUTED = {
    "app/Controllers/Attr.php": (
        "<?php namespace App\\Controllers;\n"
        "use CodeIgniter\\Router\\Attributes\\Cache;\n"
        "use CodeIgniter\\Router\\Attributes\\Filter;\n"
        "use CodeIgniter\\Router\\Attributes\\Restrict;\n"
        "#[Filter(by: 'session')]\n"
        "#[Restrict(environment: 'production')]\n"
        "class Attr {\n"
        "    #[Filter(by: 'permission', having: ['users.manage'])]\n"
        "    #[Cache(for: 60)]\n"
        "    public function a() {}\n"
        "    public function b() {}\n"
        "}\n"
    )
}
ATTR_ROUTES = "$routes->get('a', 'Attr::a');\n$routes->get('b', 'Attr::b');\n"
ON = [(None, "session", "authentication"), ("a", "permission:users.manage", "authorization")]
BLOCKED = {
    f"{NS}Attr::a#/a": AUTHORIZATION_ACCESS_CONTROL_BLOCKED,
    f"{NS}Attr::b#/b": AUTHORIZATION_ACCESS_CONTROL_BLOCKED,
}


@pytest.mark.parametrize(
    ("version", "setting", "guards", "reasons", "undecided"),
    [
        # `public bool $useControllerAttributes = true;` (system/Config/Routing.php:106): the
        # default from 4.7.0 (docs/changelogs/v4.7.0.md:286), so an absent property reads them.
        ("v4.7.0", None, ON, {CI.REASON_ATTRIBUTE_RESTRICT: 1}, {}),
        ("v4.7.6", "true", ON, {CI.REASON_ATTRIBUTE_RESTRICT: 1}, {}),
        # `useControllerAttributes === false` returns early (system/Router/Router.php:781).
        ("v4.7.0", "false", [], {CI.REASON_ATTRIBUTES_DISABLED: 3}, {}),
        # Not literal, or no version read: no claim, and the `#[Filter]` holders' routes are
        # undecided (`codeigniter-controller-attributes-setting-unread`, in BASELINE).
        ("v4.7.0", "$flag", [], {CI.REASON_ATTRIBUTE_RESTRICT: 1}, BLOCKED),
        (None, None, [], {CI.REASON_ATTRIBUTE_RESTRICT: 1, CI.REASON_VERSION_UNREAD: 1},
         BLOCKED),
        # Before 4.7.0 there are no controller attributes: any found is ignored.
        ("v4.6.4", "true", [], {CI.REASON_ATTRIBUTES_VERSION: 3}, {}),
    ],
)  # fmt: skip
def test_controller_attributes_by_version_and_setting(version, setting, guards, reasons, undecided):
    props = routing(useControllerAttributes=setting) if setting else routing()
    result = collect(ATTR_ROUTES, version=version, routing=props, extra=ATTRIBUTED)
    assert [(g.holder_method, g.label, g.guard_kind) for g in result.guards] == guards
    assert own(result) == reasons
    unread = result.reasons.get(CI.REASON_ATTRIBUTES_SETTING_UNREAD, 0)
    assert unread == (1 if undecided else 0)
    assert result.route_undecided == undecided


def test_controller_attribute_filter_is_a_route_filter_spec():
    # `getFilters()` returns `[$by . ':' . implode(',', $having)]` (Filter.php:65-72), appended
    # to the route's filters (system/Router/Router.php:253-266): the guard node is the route
    # filter's. `#[Restrict]` is a 404 condition, `#[Cache]` is ignored, closures are skipped
    # (Router.php:786).
    result = collect(
        ATTR_ROUTES + "$routes->get('z', 'C::a', ['filter' => 'permission:users.manage']);\n"
        "$routes->get('c', static fn () => 1);\n",
        version="v4.7.0",
        routing=routing(),
        extra=ATTRIBUTED,
    )
    method = next(g for g in result.guards if g.holder_method == "a")
    route = next(g for g in result.route_guards if g.label == "permission:users.manage")
    assert method.node_key == route.node_key
    assert own(result) == {CI.REASON_CLOSURE_ACTION: 1, CI.REASON_ATTRIBUTE_RESTRICT: 1}
    # `array $having` (Filter.php:44-47) given a string is a TypeError; the Router logs it and
    # drops the attribute (Router.php:799-806). A second `:` is unread (Filters.php:615).
    invalid = {
        "app/Controllers/Attr.php": (
            "<?php namespace App\\Controllers;\nuse CodeIgniter\\Router\\Attributes\\Filter;\n"
            "class Attr {\n    #[Filter(by: 'permission', having: 'users.manage')]\n"
            "    public function a() {}\n"
            "    #[Filter(by: 'group:admin', having: ['x'])]\n    public function b() {}\n}\n"
        )
    }
    bad = collect(ATTR_ROUTES, version="v4.7.0", routing=routing(), extra=invalid)
    assert bad.guards == []
    assert own(bad) == {CI.REASON_ATTRIBUTE_INVALID: 1, CI.REASON_FILTER_SPEC_UNREAD: 1}


# --- detection and the pass ----------------------------------------------------------------


def test_symfony_attributes_in_a_codeigniter_app_yield_nothing(tmp_path, monkeypatch):
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"codeigniter4/framework": "^4.5"}}), encoding="utf-8"
    )
    controller = (
        "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route;\n"
        "class SymCtl { #[Route('/sym')] public function show() {} }\n"
    )
    (tmp_path / "SymCtl.php").write_text(controller, encoding="utf-8")
    chosen, why = select_providers(str(tmp_path), REGISTRY)
    assert (chosen, why) == ((CI.PROVIDER,), "")
    assert symfony.PROVIDER not in chosen

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
    assert record.providers == ["codeigniter"]
    assert not [n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND]
    assert not [e for e in all_edges if e.get("relation") == ROUTE_RELATION]


def _graph(classes):
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for path, fqcn, methods in classes:
        short = fqcn.replace("\\", "_")
        cid = f"c_{short}"
        all_nodes.append({"id": f"f_{short}", "label": os.path.basename(path), "source_file": path})
        all_nodes.append(
            {"id": cid, "label": fqcn.rsplit("\\", 1)[-1], "_callable": True,
             "_callable_class": True, "source_file": path, "_origin": "ast"}
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
    controllers = app / "app" / "Controllers"
    users = os.path.realpath(controllers / "Users.php")
    reports = os.path.realpath(controllers / "Reports.php")
    account = os.path.realpath(controllers / "Account.php")
    all_nodes, all_edges = _graph(
        [
            (users, NS + "Users", ["show", "other", "late"]),
            (reports, NS + "Reports", ["daily"]),
            (account, NS + "Account", ["index"]),
        ]
    )
    record = resolver.run_routes(
        [{"nodes": [{"source_file": p}]} for p in (users, reports, account)],
        all_nodes,
        all_edges,
        sources=[TreeSitterRouteSource((CI.PROVIDER,))],
        repo_root=str(app),
    )
    assert record.state == "completed", record.reason
    assert record.providers == ["codeigniter"]
    routes = {n["id"]: n for n in all_nodes if n.get("_kind") == ROUTE_NODE_KIND}
    handled = {e["source"] for e in all_edges if e.get("relation") == ROUTE_RELATION}
    by_label = {n["label"]: n for n in routes.values()}
    assert {by_label[p]["id"] for p in ("/users/(:num)", "/users/(:segment)", "/reports/daily",
                                        "/account")} == handled  # fmt: skip
    # The handler_kind invariant: `method` exactly when a `handled_by` edge leaves the node.
    for node in routes.values():
        assert (node["handler_kind"] == "method") == (node["id"] in handled)
    assert by_label["/ping"]["handler_kind"] == "closure"
    # Home is not in this graph: a node, no edge.
    assert by_label["/"]["handler_kind"] == "unresolved"
    assert by_label["/reports/daily"]["authorization_state"] == "covered"
    assert by_label["/users/(:num)"]["authorization_state"] == "undecided"
    assert by_label["/account"]["authorization_state"] == "covered"
    assert by_label["/files/(:any)"]["route_template"] == ""
    labels = {n["id"]: n["label"] for n in all_nodes if n.get("_kind") == GUARD_NODE_KIND}
    daily = {
        labels[e["target"]]
        for e in all_edges
        if e.get("relation") == GUARD_RELATION and e["source"] == by_label["/reports/daily"]["id"]
    }
    assert {"group:admin", "permission:reports.read", "session", "csrf"} <= daily
    assert record.unresolved["tree_sitter_routes:codeigniter-closure-action"] == 1
