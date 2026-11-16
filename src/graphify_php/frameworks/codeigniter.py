"""CodeIgniter 4: routes files, route filters, `Config\\Filters` rules and controller attributes.

What is read, and from where. Each rule cites the docs-check item it rests on
(`docs-check ci4 <item>`); the docs check summarised the user guide and `RouteCollection.php`,
so an item is a lead to re-check at source, not proof.

- `app/Config/Routing.php` `$routeFiles` when it is a literal list of `APPPATH . 'Config/...'`
  strings, else `app/Config/Routes.php`, read in order. `$defaultNamespace`, `$autoRoute` and
  `$useControllerAttributes` from the same class.
- `$routes->get|post|put|patch|delete|options|head|match|add|map`, `group`, `environment`,
  `resource`, `presenter`, on the collection variable or a group callback's parameter.
- Route `filter` options are route-scoped guards. `app/Config/Filters.php` `$required`,
  `$globals`, `$methods` and `$filters` before-filters are route-scoped guards keyed by their
  config rule, only when they provably apply to every URL of the route.
- `#[Filter]` on a controller from 4.7.0, unless `Routing.php` turns controller attributes off
  literally. `#[Restrict]` is a route condition, counted and never a guard.

A form this provider cannot prove is counted under a fixed `codeigniter-*` reason and produces
no edge: a wrong edge is worse than a missing one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace

from ..syntax import nodes
from ..syntax.attributes import fold
from ..syntax.routes import (
    GUARD_KIND_AUTHENTICATION,
    GUARD_KIND_AUTHORIZATION,
    GUARD_KIND_MIDDLEWARE,
    GUARD_KIND_UNKNOWN,
    HANDLER_KIND_CLOSURE,
    HANDLER_KIND_METHOD,
    TEMPLATE_MULTI_SEGMENT,
    TEMPLATE_UNPARSED,
    AccessGuard,
    RouteDeclaration,
    arguments,
    array_items,
    brace_template,
    class_constant_name,
    is_array,
    literal,
    pick,
    requirement_may_match_slash,
    strings,
)
from .provider import (
    AUTHORIZATION_ACCESS_CONTROL_BLOCKED,
    AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    ProviderContext,
    ProviderResult,
    RouteGuard,
    config_rule_key,
)

NAME = "codeigniter"
# docs-check ci4 A.2: the release package and the development repository.
PACKAGES = ("codeigniter4/framework", "codeigniter4/codeigniter4")
FRAMEWORK_PACKAGE = "codeigniter4/framework"
SHIELD_PACKAGE = "codeigniter4/shield"
COMPOSER_JSON = "composer.json"
COMPOSER_LOCK = "composer.lock"
APP_DIR = "app"
ROUTING = "app/Config/Routing.php"
DEFAULT_ROUTES = "app/Config/Routes.php"
FILTERS = "app/Config/Filters.php"
MODULES = "app/Config/Modules.php"
# docs-check ci4 A.3: `Config\Routing::$defaultNamespace`.
DEFAULT_NAMESPACE = "App\\Controllers"

VIA_ROUTE_FILE = "route_file"
VIA_FILTER = "filter"
VIA_FILTER_CONFIG = "filter_config"
VIA_ATTRIBUTE = "attribute"
FILTER = "filter"

# docs-check ci4 B.4: `Router::HTTP_METHODS`, what `add()` and `map()` register under `*`.
HTTP_METHODS = ("OPTIONS", "GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "TRACE", "CONNECT")
VERB_CALLS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "options": "OPTIONS",
    "head": "HEAD",
}
# docs-check ci4 B.6: `RouteCollection::$placeholders`, in replacement order.
PLACEHOLDERS = {
    "any": ".*",
    "segment": "[^/]+",
    "alphanum": "[a-zA-Z0-9]+",
    "num": "[0-9]+",
    "alpha": "[a-zA-Z]+",
    "hash": "[^/]+",
}
# docs-check ci4 B.9: the SOURCE has `$defaultPlaceholder = 'any'` (`.*`), while the guide says a
# resource id is `(:segment)`. The source is followed: a resource id may match `/`.
DEFAULT_PLACEHOLDER = "any"

# docs-check ci4 B.8: before 4.5.0 an inner group's options replace the outer ones; before
# 4.5.4 group filters are not merged into the inner routes' filters.
GROUP_OPTIONS_MERGE = (4, 5, 0)
GROUP_FILTERS_MERGE = (4, 5, 4)
# docs-check ci4 B.7: multiple route filters are always on from 4.5.0.
MULTIPLE_FILTERS = (4, 5, 0)
# docs-check ci4 C.11: `$required` from 4.5.0; `$methods` keys lowercase before 4.5.0.
REQUIRED_FILTERS = (4, 5, 0)
METHODS_UPPERCASE = (4, 5, 0)
# Controller attributes arrive in 4.7.0 (docs/changelogs/v4.7.0.md:286) and are on by default:
# `public bool $useControllerAttributes = true;` (system/Config/Routing.php:106).
CONTROLLER_ATTRIBUTES = (4, 7, 0)

# docs-check ci4 C.12: the built-in filters; none is access control.
BUILTIN_FILTERS = {
    "csrf": "CodeIgniter\\Filters\\CSRF",
    "toolbar": "CodeIgniter\\Filters\\DebugToolbar",
    "honeypot": "CodeIgniter\\Filters\\Honeypot",
    "invalidchars": "CodeIgniter\\Filters\\InvalidChars",
    "secureheaders": "CodeIgniter\\Filters\\SecureHeaders",
    "cors": "CodeIgniter\\Filters\\Cors",
    "forcehttps": "CodeIgniter\\Filters\\ForceHTTPS",
    "pagecache": "CodeIgniter\\Filters\\PageCache",
    "performance": "CodeIgniter\\Filters\\PerformanceMetrics",
}
# docs-check ci4 C.13: Shield's Registrar aliases. The authentication / authorization split is
# the docs check's reading, not Shield's wording.
SHIELD_FILTERS = {
    "session": ("CodeIgniter\\Shield\\Filters\\SessionAuth", GUARD_KIND_AUTHENTICATION),
    "tokens": ("CodeIgniter\\Shield\\Filters\\TokenAuth", GUARD_KIND_AUTHENTICATION),
    "hmac": ("CodeIgniter\\Shield\\Filters\\HmacAuth", GUARD_KIND_AUTHENTICATION),
    "jwt": ("CodeIgniter\\Shield\\Filters\\JWTAuth", GUARD_KIND_AUTHENTICATION),
    "chain": ("CodeIgniter\\Shield\\Filters\\ChainAuth", GUARD_KIND_AUTHENTICATION),
    "group": ("CodeIgniter\\Shield\\Filters\\GroupFilter", GUARD_KIND_AUTHORIZATION),
    "permission": ("CodeIgniter\\Shield\\Filters\\PermissionFilter", GUARD_KIND_AUTHORIZATION),
    "auth-rates": ("CodeIgniter\\Shield\\Filters\\AuthRates", GUARD_KIND_MIDDLEWARE),
    "force-reset": (
        "CodeIgniter\\Shield\\Filters\\ForcePasswordResetFilter",
        GUARD_KIND_MIDDLEWARE,
    ),
}
# A filter whose decision may be authorization: an undecided match makes the route undecided.
_RISKY = frozenset({GUARD_KIND_AUTHORIZATION, GUARD_KIND_UNKNOWN})

# docs-check ci4 C.13 extra: controller attributes, toggled by `Routing::$useControllerAttributes`.
FILTER_ATTRIBUTE = "CodeIgniter\\Router\\Attributes\\Filter"
RESTRICT_ATTRIBUTE = "CodeIgniter\\Router\\Attributes\\Restrict"

# Fixed refusal reasons. A reason never carries a variable.
REASON_VERSION_UNREAD = "codeigniter-version-unread"
REASON_ROUTE_FILES_NOT_LITERAL = "codeigniter-route-files-not-literal"
REASON_ROUTES_FILE_UNREADABLE = "codeigniter-routes-file-unreadable"
REASON_ROUTING_NOT_LITERAL = "codeigniter-routing-config-not-literal"
REASON_ROUTE_DISCOVERY = "codeigniter-route-discovery-unread"
REASON_ROUTES_PASSED = "codeigniter-routes-passed-to-call"
REASON_REQUIRE_NOT_LITERAL = "codeigniter-require-not-literal"
REASON_IN_CONTROL_FLOW = "codeigniter-route-in-control-flow"
REASON_METHOD_UNKNOWN = "codeigniter-route-method-unknown"
REASON_CLI_ROUTE = "codeigniter-cli-route"
REASON_VIEW_ROUTE = "codeigniter-view-route"
REASON_REDIRECT_ROUTE = "codeigniter-redirect-route"
REASON_PATH_NOT_LITERAL = "codeigniter-path-not-literal"
REASON_VERBS_NOT_LITERAL = "codeigniter-verbs-not-literal"
REASON_OPTIONS_NOT_LITERAL = "codeigniter-route-options-not-literal"
REASON_OPTION_UNKNOWN = "codeigniter-route-option-unknown"
REASON_HANDLER_NOT_LITERAL = "codeigniter-handler-not-literal"
REASON_HANDLER_NO_METHOD = "codeigniter-handler-no-method"
REASON_HANDLER_DYNAMIC = "codeigniter-handler-dynamic"
REASON_NAMESPACE_NOT_LITERAL = "codeigniter-namespace-not-literal"
# Counted for every closure action, which is published as a route with no handler node.
REASON_CLOSURE_ACTION = "codeigniter-closure-action"
REASON_HANDLER_AMBIGUOUS = "codeigniter-handler-class-ambiguous"
REASON_PLACEHOLDER_NOT_LITERAL = "codeigniter-placeholder-not-literal"
REASON_PLACEHOLDER_UNKNOWN = "codeigniter-placeholder-unknown"
REASON_GROUP_NAME_NOT_LITERAL = "codeigniter-group-name-not-literal"
REASON_GROUP_CALLBACK_UNREAD = "codeigniter-group-callback-unread"
REASON_GROUP_FILTER_VERSION = "codeigniter-group-filter-version"
REASON_ENVIRONMENT_ROUTE = "codeigniter-environment-route"
REASON_HOSTNAME_ROUTE = "codeigniter-hostname-route"
REASON_SUBDOMAIN_ROUTE = "codeigniter-subdomain-route"
REASON_MATCH_VERB_NO_METHOD = "codeigniter-match-verb-no-method"
REASON_DUPLICATE = "codeigniter-route-duplicate"
REASON_DUPLICATE_CONDITIONAL = "codeigniter-route-duplicate-conditional"
REASON_ANY_OPTIONS_UNDECIDED = "codeigniter-route-any-options-undecided"
REASON_AUTO_ROUTE = "codeigniter-auto-route-unprovable"
REASON_RESOURCE_NAME_NOT_LITERAL = "codeigniter-resource-name-not-literal"
REASON_RESOURCE_OPTION_NOT_LITERAL = "codeigniter-resource-option-not-literal"
REASON_FILTER_NOT_LITERAL = "codeigniter-filter-not-literal"
REASON_FILTER_MULTIPLE_VERSION = "codeigniter-filter-multiple-version"
REASON_FILTERS_CONFIG_MISSING = "codeigniter-filters-config-missing"
REASON_FILTERS_CONFIG_UNREAD = "codeigniter-filters-config-unread"
REASON_FILTER_PATTERN_UNDECIDED = "codeigniter-filter-pattern-undecided"
REASON_FILTER_EXCEPT_UNDECIDED = "codeigniter-filter-except-undecided"
REASON_FILTER_METHODS_PARTIAL = "codeigniter-filter-methods-partial"
REASON_FILTER_METHODS_CASE = "codeigniter-filter-methods-case"
REASON_FILTER_REQUIRED_VERSION = "codeigniter-filter-required-version"
REASON_FILTER_SPEC_UNREAD = "codeigniter-filter-spec-unread"
REASON_ATTRIBUTES_DISABLED = "codeigniter-controller-attributes-disabled"
REASON_ATTRIBUTES_SETTING_UNREAD = "codeigniter-controller-attributes-setting-unread"
REASON_ATTRIBUTES_VERSION = "codeigniter-controller-attributes-version"
REASON_ATTRIBUTE_NOT_LITERAL = "codeigniter-controller-attribute-not-literal"
REASON_ATTRIBUTE_INVALID = "codeigniter-controller-attribute-invalid"
REASON_ATTRIBUTE_RESTRICT = "codeigniter-controller-attribute-restrict"
# A route a `view()` (GET table) or `addRedirect()` (`routes['*'][key]['redirect']`) on its key
# shadows. See `_deduplicate` and `_redirected`.
REASON_VIEW_COLLISION = "codeigniter-route-key-view-collision"
REASON_REDIRECT_COLLISION = "codeigniter-route-key-redirect-collision"
# Its only authorization guard is a `$methods` rule, which a lowercase request method skips.
REASON_METHODS_CASE_BYPASS = "codeigniter-methods-filter-case-bypass"

# `_Route.special`: a declaration kept only to refuse the routes it shadows, never published.
SPECIAL_VIEW = "view"
SPECIAL_REDIRECT = "redirect"


class _Unread:
    """A value that is written but not a literal."""

    def __repr__(self) -> str:
        return "UNREAD"


UNREAD = _Unread()
_ABSENT = object()
_CLOSURE = ("", "")
_ENV = "environment"
_HOST = "hostname"

_ROUTE_OPTIONS = frozenset({"namespace", "filter", "hostname", "subdomain", "as", "offset",
                            "priority"})  # fmt: skip
_PRESENTER_OPTIONS = (_ROUTE_OPTIONS - {"as"}) | {"controller", "placeholder", "only", "except"}
_RESOURCE_OPTIONS = _PRESENTER_OPTIONS | {"websafe"}
_CONFIG_CALLS = frozenset(
    {"setDefaultController", "setDefaultMethod", "setTranslateURIDashes", "set404Override",
     "setPrioritize", "useSupportedLocalesOnly"}
)  # fmt: skip
_CONTROL_FLOW = frozenset(
    {"if_statement", "foreach_statement", "for_statement", "while_statement", "do_statement",
     "switch_statement", "try_statement"}
)  # fmt: skip
_REQUIRES = ("require_expression", "require_once_expression", "include_expression",
             "include_once_expression")  # fmt: skip
_STRING_HANDLER = re.compile(r"(\\?[A-Za-z_]\w*(?:\\[A-Za-z_]\w*)*)::([A-Za-z_]\w*)(?:/.*)?", re.S)
_PLACEHOLDER = re.compile(r"\(:(\w+)\)")
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_LIST_TEXT = re.compile(r"\[\s*(?:'[^'\\]*'\s*,\s*)*(?:'[^'\\]*'\s*,?\s*)?\]")
# Regex marks in a route path or a filter pattern: what follows the first one is not literal.
_ROUTE_META = frozenset("([{.*+?|^$\\")
_ROUTE_META_END = frozenset("([{.*+?|^$\\)]}")
# `#` is the pattern delimiter (system/Filters/Filters.php:913): a `#` in a pattern is undecided.
_PATTERN_META = frozenset(".[](){}+?|^$\\#")

# docs-check ci4 B.9: `resource()` registration order: (method, verb, suffix, params).
RESOURCE_TABLE = (
    ("index", "GET", "", ""),
    ("new", "GET", "/new", ""),
    ("edit", "GET", "/{id}/edit", "/$1"),
    ("show", "GET", "/{id}", "/$1"),
    ("create", "POST", "", ""),
    ("update", "PUT", "/{id}", "/$1"),
    ("update", "PATCH", "/{id}", "/$1"),
    ("delete", "DELETE", "/{id}", "/$1"),
)
RESOURCE_WEBSAFE = (("delete", "POST", "/{id}/delete", "/$1"), ("update", "POST", "/{id}", "/$1"))
RESOURCE_METHODS = ("index", "show", "create", "update", "delete", "new", "edit")
# docs-check ci4 B.9: `presenter()`; the bare `photos/$id` and POST `photos` come last.
PRESENTER_TABLE = (
    ("index", "GET", "", ""),
    ("show", "GET", "/show/{id}", "/$1"),
    ("new", "GET", "/new", ""),
    ("create", "POST", "/create", ""),
    ("edit", "GET", "/edit/{id}", "/$1"),
    ("update", "POST", "/update/{id}", "/$1"),
    ("remove", "GET", "/remove/{id}", "/$1"),
    ("delete", "POST", "/delete/{id}", "/$1"),
    ("show", "GET", "/{id}", "/$1"),
    ("create", "POST", "", ""),
)
PRESENTER_METHODS = ("index", "show", "new", "create", "edit", "update", "remove", "delete")


@dataclass(frozen=True)
class _Scope:
    """What a group puts in force. `merged` and `replaced` are the group options under the
    4.5.0+ and the older inheritance rule; `group_filters` the merged group filters."""

    prefix: str | None = None
    merged: tuple[tuple[str, object], ...] = ()
    replaced: tuple[tuple[str, object], ...] = ()
    group_filters: tuple[str, ...] = ()
    group_filters_unread: bool = False
    group_filter_key: bool = False
    env: bool = False


@dataclass
class _Route:
    """`filter_key`: whether the route's stored options hold a `filter` key (None: unread)."""

    declaration: RouteDeclaration
    any_verb: bool
    key: str
    uri: str
    conditional: str
    filters: tuple[str, ...]
    unclaimed: bool
    filter_key: bool | None = False
    special: str = ""


@dataclass(frozen=True)
class _Rule:
    """One before-filter of `app/Config/Filters.php`."""

    index: int
    section: str
    label: str
    kind: str
    line: int
    patterns: object = None
    verb: str = ""
    verb_proved: bool = True
    unread: bool = False


@dataclass
class _Run:
    ctx: ProviderContext
    out: ProviderResult
    corpus: dict[str, str]
    version: tuple[int, int, int] | None = None
    version_counted: bool = False
    shield: bool = False
    default_namespace: object = DEFAULT_NAMESPACE
    auto_route: bool | None = False
    # `_ABSENT` (the framework default applies), True, False, or UNREAD.
    attributes_setting: object = _ABSENT
    attributes_mode: str = ""
    placeholders: dict[str, str] | None = field(default_factory=lambda: dict(PLACEHOLDERS))
    default_placeholder: str | None = DEFAULT_PLACEHOLDER
    routes: list[_Route] = field(default_factory=list)
    loading: list[str] = field(default_factory=list)
    aliases: dict[str, object] = field(default_factory=dict)
    rules: list[_Rule] = field(default_factory=list)
    filters_blocked: bool = False


class CodeIgniterProvider:
    name = NAME
    packages = PACKAGES

    def collect(self, ctx: ProviderContext) -> ProviderResult:
        out = ProviderResult()
        run = _Run(ctx, out, {_rel(ctx, p): p for p in ctx.files})
        run.version = installed_version(ctx.read(COMPOSER_LOCK))
        run.shield = SHIELD_PACKAGE in _requires(ctx.read(COMPOSER_JSON))
        files = _read_routing(run)
        _count_discovery(run)
        _read_filters(run)
        for rel in files:
            _load(run, rel, frozenset({"$routes"}), _Scope())
        if run.auto_route is not False:
            # docs-check ci4 A.3: auto routes exist with no declaration; a static reader
            # cannot list them.
            out.count(REASON_AUTO_ROUTE)
        _publish(run)
        return out


PROVIDER = CodeIgniterProvider()


# --- files and versions --------------------------------------------------------------------


def _rel(ctx: ProviderContext, path: str) -> str:
    if os.path.isabs(path):
        path = os.path.relpath(path, os.path.realpath(ctx.repo_root))
    return os.path.normpath(path).replace(os.sep, "/")


def _spelling(run: _Run, rel: str) -> str:
    return run.corpus.get(rel, rel)


def _requires(manifest: bytes | None) -> set[str]:
    """Lower-cased direct `require` and `require-dev` of the root `composer.json`."""
    try:
        data = json.loads((manifest or b"").decode("utf-8", errors="replace"))
    except ValueError:
        return set()
    found: set[str] = set()
    for section in ("require", "require-dev"):
        value = data.get(section) if isinstance(data, dict) else None
        if isinstance(value, dict):
            found.update(k.lower() for k in value if isinstance(k, str))
    return found


def installed_version(lock: bytes | None) -> tuple[int, int, int] | None:
    """The `codeigniter4/framework` version in `composer.lock` `packages`, else None."""
    try:
        data = json.loads((lock or b"").decode("utf-8", errors="replace"))
    except ValueError:
        return None
    packages = data.get("packages") if isinstance(data, dict) else None
    for package in packages if isinstance(packages, list) else ():
        if isinstance(package, dict) and package.get("name") == FRAMEWORK_PACKAGE:
            version = package.get("version")
            match = _VERSION.match(version) if isinstance(version, str) else None
            if match is None:
                return None
            major, minor, patch = (int(g) for g in match.groups())
            return major, minor, patch
    return None


def _at_least(run: _Run, version: tuple[int, int, int]) -> bool | None:
    """Whether the installed version is `version` or newer; None (counted once) when unread."""
    if run.version is None:
        if not run.version_counted:
            run.version_counted = True
            run.out.count(REASON_VERSION_UNREAD)
        return None
    return run.version >= version


def _class_properties(root) -> tuple[dict[str, object] | None, bool]:
    """`(property name -> default value node, has methods)` of the file's first class."""
    stack = [root]
    declaration = None
    while stack:
        current = stack.pop()
        if current.type == "class_declaration":
            declaration = current
            break
        stack.extend(reversed(current.children))
    body = nodes.child(declaration, "declaration_list") if declaration is not None else None
    if body is None:
        return None, False
    found: dict[str, object] = {}
    for declared in nodes.children(body, "property_declaration"):
        for element in nodes.children(declared, "property_element"):
            named = [c for c in element.children if c.is_named]
            if named and named[0].type == "variable_name":
                found[nodes.node_text(named[0]).lstrip("$")] = named[1] if len(named) > 1 else None
    return found, bool(nodes.children(body, "method_declaration"))


def _bool(node) -> bool | None:
    if node is None or node.type != "boolean":
        return None
    return {"true": True, "false": False}.get(nodes.node_text(node).strip().lower())


def _app_path(node, current_rel: str) -> str | None:
    """`APPPATH . 'x'` or `__DIR__ . '/x'` as a repo-relative path, else None."""
    if node is None or node.type != "binary_expression":
        return None
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    operators = [c for c in node.children if not c.is_named]
    tail = literal(right)
    if len(operators) != 1 or nodes.node_text(operators[0]) != "." or tail is None:
        return None
    base = nodes.node_text(left).strip() if left is not None else ""
    if base == "APPPATH":
        joined = f"{APP_DIR}/{tail}"
    elif base == "__DIR__":
        joined = os.path.dirname(current_rel) + "/" + tail
    else:
        return None
    normal = os.path.normpath(joined.lstrip("/")).replace(os.sep, "/")
    return None if normal.startswith("..") or os.path.isabs(normal) else normal


def _read_routing(run: _Run) -> list[str]:
    """The route files to read, in order, and the `Config\\Routing` values in force.

    docs-check ci4 A.1: `$routeFiles` since 4.4.0, default `[APPPATH . 'Config/Routes.php']`.
    """
    parsed = run.ctx.parse(_spelling(run, ROUTING))
    props = _class_properties(parsed.root)[0] if parsed is not None else None
    if props is None:
        # No readable `Config\Routing`: the attribute default is not provably in force.
        run.attributes_setting = UNREAD
    props = props or {}
    files = [DEFAULT_ROUTES]
    if "routeFiles" in props:
        items = array_items(props["routeFiles"])
        listed = [_app_path(v, ROUTING) if k is None else None for k, v in items or []]
        if items is None or None in listed:
            run.out.count(REASON_ROUTE_FILES_NOT_LITERAL)
            files = []
        else:
            files = [p for p in listed if p is not None]
    if "defaultNamespace" in props:
        value = literal(props["defaultNamespace"])
        if value is None:
            run.out.count(REASON_ROUTING_NOT_LITERAL)
        run.default_namespace = UNREAD if value is None else value
    if "autoRoute" in props:
        run.auto_route = _bool(props["autoRoute"])
    if "useControllerAttributes" in props:
        setting = _bool(props["useControllerAttributes"])
        run.attributes_setting = UNREAD if setting is None else setting
    return files


def _count_discovery(run: _Run) -> None:
    """docs-check ci4 A.1: `discoverRoutes()` loads every namespace's `Config/Routes.php`.

    Counted once unless `app/Config/Modules.php` literally turns discovery or its `routes`
    alias off.
    """
    parsed = run.ctx.parse(_spelling(run, MODULES))
    props = (_class_properties(parsed.root)[0] if parsed is not None else None) or {}
    if _bool(props.get("enabled")) is False:
        return
    aliases = strings(props.get("aliases")) if props.get("aliases") is not None else None
    if aliases is not None and "routes" not in aliases:
        return
    run.out.count(REASON_ROUTE_DISCOVERY)


# --- routes files --------------------------------------------------------------------------


def _load(run: _Run, rel: str, receivers: frozenset[str], scope: _Scope) -> None:
    if rel in run.loading:
        return
    parsed = run.ctx.parse(_spelling(run, rel))
    if parsed is None:
        run.out.count(REASON_ROUTES_FILE_UNREADABLE)
        return
    run.loading.append(rel)
    try:
        _statements(run, parsed.root.children, receivers, rel, parsed.names, scope)
    finally:
        run.loading.pop()


def _statements(run: _Run, statements, receivers, rel: str, names, scope: _Scope) -> None:
    for statement in statements:
        if statement.type == "expression_statement":
            expression = next((c for c in statement.children if c.is_named), None)
            if expression is not None:
                _expression(run, expression, receivers, rel, names, scope)
        elif statement.type in _CONTROL_FLOW:
            if _mentions(statement, receivers) or _has_require(statement):
                run.out.count(REASON_IN_CONTROL_FLOW)
        elif statement.type == "compound_statement":
            _statements(run, statement.children, receivers, rel, names, scope)


def _mentions(node, receivers) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "variable_name" and nodes.node_text(current) in receivers:
            return True
        stack.extend(current.children)
    return False


def _has_require(node) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _REQUIRES:
            return True
        stack.extend(current.children)
    return False


def _passes(node, receivers) -> bool:
    """Whether the collection is handed to another call, which may add routes unseen."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "argument":
            value = next((c for c in current.children if c.is_named), None)
            if (
                value is not None
                and value.type == "variable_name"
                and nodes.node_text(value) in receivers
            ):
                return True
        stack.extend(current.children)
    return False


def _expression(run: _Run, expression, receivers, rel: str, names, scope: _Scope) -> None:
    if expression.type in _REQUIRES:
        operand = next((c for c in expression.children if c.is_named), None)
        target = _app_path(operand, rel)
        if target is None:
            run.out.count(REASON_REQUIRE_NOT_LITERAL)
            return
        _load(run, target, receivers, scope)
        return
    calls = []
    node = expression
    while node is not None and node.type == "member_call_expression":
        calls.append((nodes.node_text(node.child_by_field_name("name")), node))
        node = node.child_by_field_name("object")
    if (
        calls
        and node is not None
        and node.type == "variable_name"
        and nodes.node_text(node) in receivers
    ):
        for method, call in reversed(calls):
            _call(run, method, call, receivers, rel, names, scope)
        return
    if _passes(expression, receivers):
        # docs-check ci4 C.13: `service('auth')->routes($routes)` adds routes with no literal.
        run.out.count(REASON_ROUTES_PASSED)


def _call(run: _Run, method: str, call, receivers, rel: str, names, scope: _Scope) -> None:
    positional, named = arguments(call)
    line = nodes.line_span(call)[0]
    if method in VERB_CALLS:
        _verb(run, (VERB_CALLS[method],), False, call, 0, rel, names, scope)
    elif method == "match":
        # docs-check ci4 B.4: `match()` lowercases each verb and calls that method.
        verbs = strings(pick((0, "verbs"), positional, named))
        if verbs is not None and any(v.lower() in ("trace", "connect") for v in verbs):
            # `$this->{$verb}(...)` (system/Router/RouteCollection.php:935): RouteCollection has
            # no `trace()` or `connect()`, although both are in `Router::HTTP_METHODS`.
            run.out.count(REASON_MATCH_VERB_NO_METHOD)
            return
        if verbs is None or any(v.lower() not in VERB_CALLS for v in verbs):
            run.out.count(REASON_VERBS_NOT_LITERAL)
            return
        methods = tuple(dict.fromkeys(VERB_CALLS[v.lower()] for v in verbs))
        _verb(run, methods, False, call, 1, rel, names, scope)
    elif method == "add":
        # docs-check ci4 B.4: `add()` is `create('*', ...)`: every HTTP method.
        _verb(run, HTTP_METHODS, True, call, 0, rel, names, scope)
    elif method == "map":
        # docs-check ci4 B.4: `map()` calls `add()` for each `from => to`.
        items = array_items(pick((0, "routes"), positional, named))
        options = _options(run, pick((1, "options"), positional, named), _ROUTE_OPTIONS, names)
        if items is None:
            run.out.count(REASON_PATH_NOT_LITERAL)
            return
        if options is None:
            return
        for key, to in items:
            from_ = literal(key) if key is not None else None
            if from_ is None:
                run.out.count(REASON_PATH_NOT_LITERAL)
                continue
            _create(run, rel, line, HTTP_METHODS, True, from_, to, options, scope, names)
    elif method == "cli":
        run.out.count(REASON_CLI_ROUTE)
    elif method == "view":
        run.out.count(REASON_VIEW_ROUTE)
        _shadow(run, SPECIAL_VIEW, call, rel, scope)
    elif method == "addRedirect":
        run.out.count(REASON_REDIRECT_ROUTE)
        _shadow(run, SPECIAL_REDIRECT, call, rel, scope)
    elif method in ("group", "environment"):
        _group(run, method, positional, receivers, rel, names, scope)
    elif method in ("resource", "presenter"):
        _resource(run, method, call, rel, names, scope)
    elif method == "setDefaultNamespace":
        # An unread namespace is counted once per string handler it leaves unread.
        value = literal(pick((0, "value"), positional, named))
        run.default_namespace = UNREAD if value is None else value
    elif method == "setAutoRoute":
        run.auto_route = _bool(pick((0, "value"), positional, named))
    elif method == "addPlaceholder":
        _add_placeholder(run, positional)
    elif method == "setDefaultConstraint":
        value = literal(pick((0, "placeholder"), positional, named))
        if value is None:
            run.default_placeholder = None
        elif run.placeholders is not None and value in run.placeholders:
            run.default_placeholder = value
    elif method not in _CONFIG_CALLS:
        run.out.count(REASON_METHOD_UNKNOWN)


def _add_placeholder(run: _Run, positional) -> None:
    """docs-check ci4 B.6: literal, and read only for routes declared after it."""
    if run.placeholders is None:
        return
    if len(positional) == 2:
        name, pattern = literal(positional[0]), literal(positional[1])
        pairs = [(name, pattern)]
    else:
        items = array_items(positional[0]) if len(positional) == 1 else None
        entries = items if items else [(None, None)]
        pairs = [(literal(k) if k is not None else None, literal(v)) for k, v in entries]
    if any(n is None or p is None for n, p in pairs):
        run.placeholders = None
        return
    for name, pattern in pairs:
        run.placeholders[str(name).lower()] = str(pattern)


def _verb(run: _Run, methods, any_verb: bool, call, offset: int, rel, names, scope) -> None:
    positional, named = arguments(call)
    from_ = literal(pick((offset, "from"), positional, named))
    if from_ is None:
        run.out.count(REASON_PATH_NOT_LITERAL)
        return
    options = _options(run, pick((offset + 2, "options"), positional, named), _ROUTE_OPTIONS, names)
    if options is None:
        return
    to = pick((offset + 1, "to"), positional, named)
    line = nodes.line_span(call)[0]
    _create(run, rel, line, methods, any_verb, from_, to, options, scope, names)


def _shadow(run: _Run, special: str, call, rel: str, scope: _Scope) -> None:
    """`view()` or `addRedirect()`, kept as an unpublished route that refuses those it shadows.

    `view()` is `create(Method::GET, $from, $to, $routeOptions)` with the caller's options
    (system/Router/RouteCollection.php:1060); `addRedirect()` is `create('*', $from, ...,
    ['redirect' => $status])` (:610). A `from` that is not literal proves no key.
    """
    positional, named = arguments(call)
    from_ = literal(pick((0, "from"), positional, named))
    if from_ is None:
        return
    view = special == SPECIAL_VIEW
    hosted = any(_scope_value(run, scope, k) is not _ABSENT for k in ("hostname", "subdomain"))
    conditional = ""
    if scope.env:
        conditional = _ENV
    elif hosted or (view and _maybe_hosted(pick((2, "options"), positional, named))):
        conditional = _HOST
    full = _compose(scope.prefix, from_)
    declaration = RouteDeclaration(
        "", "", _spelling(run, rel), nodes.line_span(call)[0], path="/" + full.strip("/"),
        methods=("GET",) if view else tuple(HTTP_METHODS), provider=NAME,
    )  # fmt: skip
    key = _route_key(full, run.placeholders)
    uri = full.strip("/").lower()
    run.routes.append(
        _Route(declaration, not view, key, uri, conditional, (), False, False, special)
    )


def _maybe_hosted(node) -> bool:
    """Whether `view()` options may hold `hostname` or `subdomain`; unread options may."""
    if node is None or node.type == "null":
        return False
    items = array_items(node)
    if items is None:
        return True
    names = [literal(key) if key is not None else None for key, _ in items]
    return any(name is None or name in ("hostname", "subdomain") for name in names)


def _options(run: _Run, node, allowed, names) -> dict | None:
    """A literal options array as `{key: value}`, or None with a counted reason."""
    if node is None or node.type == "null":
        return {}
    items = array_items(node)
    if items is None:
        run.out.count(REASON_OPTIONS_NOT_LITERAL)
        return None
    found: dict[str, object] = {}
    for key, value in items:
        name = literal(key) if key is not None else None
        if name is None:
            run.out.count(REASON_OPTIONS_NOT_LITERAL)
            return None
        if name not in allowed:
            run.out.count(REASON_OPTION_UNKNOWN)
            return None
        if name == "filter":
            found[name] = _filter_list(value, names)
        elif name in ("only", "except"):
            listed = strings(value)
            found[name] = UNREAD if listed is None else listed
        elif name in ("hostname", "subdomain", "offset", "priority"):
            found[name] = True
        elif name == "websafe":
            # `isset($options['websafe'])`: any value but null turns it on.
            found[name] = getattr(value, "type", "") != "null"
        else:
            text = literal(value)
            found[name] = UNREAD if text is None else text
    return found


def _filter_list(node, names) -> object:
    """docs-check ci4 B.7: a filter is a string or a list; an alias or a class name."""
    single = literal(node) or class_constant_name(node, names)
    if single is not None:
        return (single,)
    items = array_items(node)
    if items is None or any(k is not None for k, _ in items):
        return UNREAD
    found = tuple(literal(v) or class_constant_name(v, names) for _, v in items)
    return UNREAD if any(f is None for f in found) else found


# --- groups --------------------------------------------------------------------------------


def _group(run: _Run, method: str, positional, receivers, rel, names, scope: _Scope) -> None:
    """docs-check ci4 B.8 `group(name, [options], callback)`, B.10 `environment(env, callback)`."""
    if not positional:
        run.out.count(REASON_GROUP_CALLBACK_UNREAD)
        return
    callback = positional[-1]
    if method == "environment":
        _callback(run, callback, receivers, rel, names, replace(scope, env=True))
        return
    name = literal(positional[0])
    if name is None:
        run.out.count(REASON_GROUP_NAME_NOT_LITERAL)
        return
    rest = positional[1:]
    options: dict | None = {}
    has_options = len(rest) > 1
    if has_options:
        options = _options(run, rest[0], _ROUTE_OPTIONS, names)
    if options is None or not rest:
        if not rest:
            run.out.count(REASON_GROUP_CALLBACK_UNREAD)
        return
    prefix = (scope.prefix or "") + "/" + name if name else None
    inner = scope
    if prefix is not None:
        inner = replace(inner, prefix=prefix.strip("/"))
    group_filter = options.pop("filter", _ABSENT)
    if group_filter is not _ABSENT:
        inner = replace(inner, group_filter_key=True)
    merged = dict(scope.merged)
    merged.update(options)
    inner = replace(
        inner,
        merged=tuple(merged.items()),
        replaced=tuple(options.items()) if has_options else scope.replaced,
    )
    if group_filter is UNREAD:
        run.out.count(REASON_FILTER_NOT_LITERAL)
        inner = replace(inner, group_filters_unread=True)
    elif isinstance(group_filter, tuple):
        # docs-check ci4 B.8: `array_merge($currentFilter, (array) $options['filter'])`.
        inner = replace(inner, group_filters=scope.group_filters + group_filter)
    _callback(run, callback, receivers, rel, names, inner)


def _callback(run: _Run, node, receivers, rel, names, scope: _Scope) -> None:
    """A group callback runs as `$callback($this)`: its first parameter is the collection."""
    if node is None or node.type not in ("anonymous_function", "arrow_function"):
        run.out.count(REASON_GROUP_CALLBACK_UNREAD)
        return
    parameters = nodes.child(node, "formal_parameters")
    first = next(iter(nodes.children(parameters, "simple_parameter")), None)
    variable = nodes.child(first, "variable_name") if first is not None else None
    inner = {nodes.node_text(variable)} if variable is not None else set()
    if node.type == "arrow_function":
        body = node.child_by_field_name("body")
        if body is not None:
            _expression(run, body, frozenset(inner | set(receivers)), rel, names, scope)
        return
    use = nodes.child(node, "anonymous_function_use_clause")
    for captured in nodes.children(use, "variable_name"):
        if nodes.node_text(captured) in receivers:
            inner.add(nodes.node_text(captured))
    body = nodes.child(node, "compound_statement")
    if body is not None:
        _statements(run, body.children, frozenset(inner), rel, names, scope)


def _scope_value(run: _Run, scope: _Scope, key: str) -> object:
    """A group option in force: the 4.5.0+ merge, or the older inner-replaces rule.

    docs-check ci4 B.8: "Prior to v4.5.0 ... options passed to the outer group() are not merged
    with the inner". With no version read, the two rules must agree or the value is unread.
    """
    merged = dict(scope.merged).get(key, _ABSENT)
    replaced = dict(scope.replaced).get(key, _ABSENT)
    if merged is replaced or merged == replaced:
        return merged
    newer = _at_least(run, GROUP_OPTIONS_MERGE)
    if newer is None:
        return UNREAD
    return merged if newer else replaced


def _option(run: _Run, options: dict, scope: _Scope, key: str) -> object:
    """`array_merge($this->currentOptions, $options)`: the route's own option wins."""
    if key in options:
        return options[key]
    return _scope_value(run, scope, key)


# --- routes --------------------------------------------------------------------------------


def _compose(prefix: str | None, from_: str) -> str:
    """docs-check ci4 B.8: `$prefix . $from`, trimmed of `/` unless it is `/`."""
    full = from_ if prefix is None else f"{prefix}/{from_}"
    return full if full == "/" else full.strip("/")


def _create(run: _Run, rel, line, methods, any_verb, from_, to, options, scope, names) -> None:
    """`RouteCollection::create()` for one route, or a counted reason."""
    namespace = _option(run, options, scope, "namespace")
    if namespace is _ABSENT:
        namespace = run.default_namespace
    handler = _handler(run, to, names, namespace)
    if handler is None:
        return
    full = _compose(scope.prefix, from_)
    path = "/" + full.strip("/")
    conditional = ""
    if scope.env:
        conditional = _ENV
        run.out.count(REASON_ENVIRONMENT_ROUTE)
    elif _option(run, options, scope, "hostname") is not _ABSENT:
        # docs-check ci4 B.10: served only for a matching host; published, and counted.
        conditional = _HOST
        run.out.count(REASON_HOSTNAME_ROUTE)
    elif _option(run, options, scope, "subdomain") is not _ABSENT:
        # `elseif` on `subdomain` (system/Router/RouteCollection.php:1357): it is ignored when
        # `hostname` is set, so only one condition is counted.
        conditional = _HOST
        run.out.count(REASON_SUBDOMAIN_ROUTE)
    filters, unclaimed = _route_filters(run, options, scope)
    filter_key: bool | None = "filter" in options
    if not filter_key and scope.group_filter_key:
        # A group `filter` sits in `currentOptions` from 4.5.0 (RouteCollection.php:686-689);
        # with the older inner-replaces rule its presence is not read.
        filter_key = True if _at_least(run, GROUP_OPTIONS_MERGE) else None
    template, template_reason, ci_reason = route_template(full.strip("/"), run.placeholders)
    if conditional != _ENV:
        for reason in (template_reason, ci_reason):
            if reason:
                run.out.count(reason)
    closure = handler == _CLOSURE
    files = (rel,) if closure else run.ctx.class_files(handler[0])
    if len(files) > 1:
        run.out.count(REASON_HANDLER_AMBIGUOUS)
    as_ = _option(run, options, scope, "as")
    declaration = RouteDeclaration(
        controller_class=handler[0],
        controller_method=handler[1],
        source_file=_spelling(run, rel),
        line=line,
        path=path,
        name=as_ if isinstance(as_, str) else None,
        methods=tuple(methods),
        method_path=from_,
        provider=NAME,
        via=VIA_ROUTE_FILE,
        handler_file=files[0] if len(files) == 1 else "",
        template=template,
        template_reason=template_reason,
        handler_kind=HANDLER_KIND_CLOSURE if closure else HANDLER_KIND_METHOD,
    )
    key = _route_key(full, run.placeholders)
    uri = full.strip("/").lower()
    run.routes.append(
        _Route(declaration, any_verb, key, uri, conditional, filters, unclaimed, filter_key)
    )


def _route_filters(run: _Run, options: dict, scope: _Scope) -> tuple[tuple[str, ...], bool]:
    """`(filters proved on the route, whether unproved filters may also apply)`."""
    own = options.get("filter", _ABSENT)
    unclaimed = False
    claimed: tuple[str, ...] = ()
    if own is UNREAD:
        run.out.count(REASON_FILTER_NOT_LITERAL)
        unclaimed = True
    elif isinstance(own, tuple):
        claimed = own
    if scope.group_filters or scope.group_filters_unread:
        merge = _at_least(run, GROUP_FILTERS_MERGE)
        if merge and not scope.group_filters_unread:
            # docs-check ci4 B.8: from 4.5.4 group filters merge into the route's.
            claimed = scope.group_filters + claimed
        else:
            if merge is False:
                run.out.count(REASON_GROUP_FILTER_VERSION)
            unclaimed = (
                unclaimed
                or scope.group_filters_unread
                or any(_filter_kind(run, f) in _RISKY for f in scope.group_filters)
            )
    if len(claimed) > 1 and not _at_least(run, MULTIPLE_FILTERS):
        # docs-check ci4 B.7: multiple filters are off by default before 4.5.0.
        run.out.count(REASON_FILTER_MULTIPLE_VERSION)
        unclaimed = unclaimed or any(_filter_kind(run, f) in _RISKY for f in claimed)
        claimed = ()
    for text in claimed:
        if spec_unread(text):
            run.out.count(REASON_FILTER_SPEC_UNREAD)
            unclaimed = True
    claimed = tuple(t for t in claimed if not spec_unread(t))
    return tuple(dict.fromkeys(claimed)), unclaimed


def spec_unread(text: str) -> bool:
    """`alias:a:b`: `[$alias, $arguments] = explode(':', $filter)` (Filters.php:615) drops `:b`."""
    return text.count(":") > 1


def _handler(run: _Run, to, names, namespace) -> tuple[str, str] | None:
    """The `(controller FQCN, method)` a `to` names, `_CLOSURE`, or None with a reason.

    docs-check ci4 B.5: a string is prefixed with the namespace unless it starts with `\\`
    (`Admin\\Users::index` IS prefixed); an array callable is always fully qualified.
    """
    if isinstance(to, str):
        text: str | None = to
    elif to is None:
        run.out.count(REASON_HANDLER_NOT_LITERAL)
        return None
    elif to.type in nodes.CLOSURE_KINDS:
        run.out.count(REASON_CLOSURE_ACTION)
        return _CLOSURE
    elif is_array(to):
        return _array_handler(run, to, names)
    else:
        text = literal(to)
    if text is None:
        run.out.count(REASON_HANDLER_NOT_LITERAL)
        return None
    head = text.split("/", 1)[0]
    if "$" in head:
        run.out.count(REASON_HANDLER_DYNAMIC)
        return None
    match = _STRING_HANDLER.fullmatch(text)
    if match is None:
        run.out.count(REASON_HANDLER_NO_METHOD if "::" not in head else REASON_HANDLER_NOT_LITERAL)
        return None
    controller, method = match.group(1), match.group(2)
    if not controller.startswith("\\"):
        if namespace is UNREAD or not isinstance(namespace, str):
            run.out.count(REASON_NAMESPACE_NOT_LITERAL)
            return None
        prefix = namespace.strip("\\")
        controller = f"{prefix}\\{controller}" if prefix else controller
    return controller.lstrip("\\"), method


def _array_handler(run: _Run, node, names) -> tuple[str, str] | None:
    """`[Ctl::class, 'm']`, or `[[Ctl::class, 'm'], '$1/$2']` with literal params."""
    items = array_items(node)
    if items is None or len(items) != 2 or any(k is not None for k, _ in items):
        run.out.count(REASON_HANDLER_NOT_LITERAL)
        return None
    first, second = items[0][1], items[1][1]
    if is_array(first):
        if literal(second) is None:
            run.out.count(REASON_HANDLER_NOT_LITERAL)
            return None
        return _array_handler(run, first, names)
    controller = class_constant_name(first, names)
    method = literal(second)
    if controller is None or not method or not method.isidentifier():
        run.out.count(REASON_HANDLER_NOT_LITERAL)
        return None
    return controller.lstrip("\\"), method


def route_template(uri: str, placeholders: dict[str, str] | None) -> tuple[str, str, str]:
    """`(route_template, TEMPLATE_* reason, codeigniter reason)` of a route path.

    docs-check ci4 B.6: each `(:name)` or raw `(...)` group is `{1}`, `{2}`... by position. A
    group whose pattern may match `/` (`(:any)`, `.*`), or a `.` outside a group, gives "" and
    `route-template-multi-segment`; any other regex mark gives `route-template-unparsed`.
    """
    out: list[str] = []
    position = 0
    multi = False
    i = 0
    while i < len(uri):
        char = uri[i]
        if char == "(":
            end = uri.find(")", i)
            inner = uri[i + 1 : end] if end > i else "("
            if "(" in inner:
                return "", TEMPLATE_UNPARSED, ""
            position += 1
            if inner.startswith(":"):
                if placeholders is None:
                    return "", TEMPLATE_UNPARSED, REASON_PLACEHOLDER_NOT_LITERAL
                pattern = placeholders.get(inner[1:].lower())
                if pattern is None:
                    return "", TEMPLATE_UNPARSED, REASON_PLACEHOLDER_UNKNOWN
            else:
                pattern = inner
            multi = multi or requirement_may_match_slash(pattern)
            out.append(f"{{{position}}}")
            i = end + 1
            continue
        if uri.startswith("{locale}", i):
            out.append("{locale}")
            i += len("{locale}")
            continue
        if char == ".":
            multi = True
        elif char in _ROUTE_META_END:
            return "", TEMPLATE_UNPARSED, ""
        out.append(char)
        i += 1
    if multi:
        return "", TEMPLATE_MULTI_SEGMENT, ""
    template, reason = brace_template("/" + "".join(out))
    return template, reason, ""


def _route_key(full: str, placeholders: dict[str, str] | None) -> str:
    """docs-check ci4 B.10: the duplicate key, after placeholders are replaced by their regex."""
    if placeholders is None:
        return full

    def swap(match: re.Match) -> str:
        pattern = placeholders.get(match.group(1).lower())
        return match.group(0) if pattern is None else f"({pattern})"

    return _PLACEHOLDER.sub(swap, full)


def _resource(run: _Run, kind: str, call, rel, names, scope: _Scope) -> None:
    """docs-check ci4 B.9: `resource()` and `presenter()` route tables."""
    positional, named = arguments(call)
    name = literal(pick((0, "name"), positional, named))
    if name is None:
        run.out.count(REASON_RESOURCE_NAME_NOT_LITERAL)
        return
    allowed = _RESOURCE_OPTIONS if kind == "resource" else _PRESENTER_OPTIONS
    options = _options(run, pick((1, "options"), positional, named), allowed, names)
    if options is None:
        return
    controller = options.pop("controller", _ABSENT)
    placeholder = options.pop("placeholder", _ABSENT)
    only = options.pop("only", _ABSENT)
    except_ = options.pop("except", _ABSENT)
    websafe = options.pop("websafe", False)
    if UNREAD in (controller, placeholder, only, except_):
        run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
        return
    if isinstance(controller, str):
        new_name = controller[:1].upper() + controller[1:]
    else:
        new_name = "\\".join(p[:1].upper() + p[1:] for p in name.split("/"))
    if isinstance(placeholder, str):
        raw = placeholder
    elif run.placeholders is None or run.default_placeholder is None:
        # The id pattern is part of the path: an unread one leaves every member path unread.
        run.out.count(REASON_PLACEHOLDER_NOT_LITERAL)
        return
    else:
        raw = run.placeholders.get(run.default_placeholder, run.default_placeholder)
    identifier = "(" + raw.strip("()") + ")"
    wanted = list(RESOURCE_METHODS if kind == "resource" else PRESENTER_METHODS)
    if isinstance(only, tuple):
        # `explode(',', ...)` for a string, with no trim.
        wanted = list(only[0].split(",") if len(only) == 1 else only)
    if isinstance(except_, tuple):
        removed = set(except_[0].split(",") if len(except_) == 1 else except_)
        wanted = [m for m in wanted if m not in removed]
    table = RESOURCE_TABLE if kind == "resource" else PRESENTER_TABLE
    if kind == "resource" and websafe:
        table = table + RESOURCE_WEBSAFE
    line = nodes.line_span(call)[0]
    for method, verb, suffix, params in table:
        if method not in wanted:
            continue
        from_ = name + suffix.replace("{id}", identifier)
        to = f"{new_name}::{method}{params}"
        _create(run, rel, line, (verb,), False, from_, to, dict(options), scope, names)


# --- Config\Filters ------------------------------------------------------------------------


def _filter_kind(run: _Run, text: str) -> str:
    """The guard kind of a filter alias or class, `unknown` unless proved.

    An alias the application re-maps in `$aliases` to another class is `unknown`: a custom
    filter may do authorization.
    """
    if spec_unread(text):
        return GUARD_KIND_UNKNOWN
    alias = text.partition(":")[0].strip()
    if "\\" in alias:
        if any(fold(cls) == fold(alias) for cls in BUILTIN_FILTERS.values()):
            return GUARD_KIND_MIDDLEWARE
        for cls, kind in SHIELD_FILTERS.values() if run.shield else ():
            if fold(cls) == fold(alias):
                return kind
        return GUARD_KIND_UNKNOWN
    if alias in BUILTIN_FILTERS:
        expected, kind = BUILTIN_FILTERS[alias], GUARD_KIND_MIDDLEWARE
    elif run.shield and alias in SHIELD_FILTERS:
        expected, kind = SHIELD_FILTERS[alias]
    else:
        return GUARD_KIND_UNKNOWN
    declared = run.aliases.get(alias, _ABSENT)
    if declared is _ABSENT or (isinstance(declared, str) and fold(declared) == fold(expected)):
        return kind
    return GUARD_KIND_UNKNOWN


def _section(node, key: str) -> object:
    """The value node under literal `key` of an array literal; `_ABSENT`; UNREAD when unread."""
    items = array_items(node)
    if items is None:
        return UNREAD
    for k, value in items:
        name = literal(k) if k is not None else None
        if k is not None and name is None:
            return UNREAD
        if name == key:
            return value
    return _ABSENT


def _read_filters(run: _Run) -> None:
    """docs-check ci4 C.11: `$aliases`, `$required`, `$globals`, `$methods`, `$filters`."""
    rel = FILTERS
    parsed = run.ctx.parse(_spelling(run, rel))
    if parsed is None:
        run.out.count(REASON_FILTERS_CONFIG_MISSING)
        return
    props, has_methods = _class_properties(parsed.root)
    try:
        if props is None or has_methods:
            # A method may change any property at runtime.
            raise _Blocked
        _read_aliases(run, props.get("aliases"), parsed.names)
        _read_rules(run, props)
    except _Blocked:
        run.out.count(REASON_FILTERS_CONFIG_UNREAD)
        run.filters_blocked = True
        run.rules = []


class _Blocked(Exception):
    """A `Config\\Filters` property that is not a literal."""


def _read_aliases(run: _Run, node, names) -> None:
    if node is None:
        return
    items = array_items(node)
    if items is None:
        raise _Blocked
    for key, value in items:
        alias = literal(key) if key is not None else None
        if alias is None:
            raise _Blocked
        cls = class_constant_name(value, names) or literal(value)
        run.aliases[alias] = cls.lstrip("\\") if cls else UNREAD


def _before(node) -> list | None:
    """The `before` entries of a `['before' => [...], 'after' => [...]]` value."""
    value = _section(node, "before")
    if value is UNREAD:
        raise _Blocked
    if value is _ABSENT:
        return None
    items = array_items(value)
    if items is None:
        raise _Blocked
    return items


def _read_rules(run: _Run, props: dict) -> None:
    def add(section: str, text: str | None, node, **extra) -> None:
        if text is None:
            raise _Blocked
        line = nodes.line_span(node)[0]
        unread = spec_unread(text)
        if unread:
            run.out.count(REASON_FILTER_SPEC_UNREAD)
        rule = _Rule(
            len(run.rules), section, text, _filter_kind(run, text), line, unread=unread, **extra
        )
        run.rules.append(rule)

    required = _before(props["required"]) if props.get("required") is not None else None
    if required and _at_least(run, REQUIRED_FILTERS) is not None:
        if not _at_least(run, REQUIRED_FILTERS):
            run.out.count(REASON_FILTER_REQUIRED_VERSION)
        else:
            for _, value in required:
                add("required", literal(value), value)
    globals_ = _before(props["globals"]) if props.get("globals") is not None else None
    for key, value in globals_ or []:
        if key is None:
            add("globals", literal(value), value)
            continue
        except_ = _section(value, "except")
        patterns: object = None
        if except_ is not _ABSENT:
            listed = strings(except_) if except_ is not UNREAD else None
            patterns = UNREAD if listed is None else listed
        add("globals", literal(key), value, patterns=patterns)
    methods = array_items(props["methods"]) if props.get("methods") is not None else []
    if methods is None:
        raise _Blocked
    keys = [literal(key) if key is not None else None for key, _ in methods]
    for verb, (_, value) in zip(keys, methods, strict=True):
        listed = strings(value)
        if verb is None or listed is None:
            raise _Blocked
        if verb.upper() == "CLI":
            continue
        upper = _at_least(run, METHODS_UPPERCASE)
        if upper:
            # Filters.php:727-742: the exact request method first, then the deprecated
            # lowercase key. Any other spelling never matches an uppercase method.
            if verb != verb.upper() and (verb != verb.lower() or verb.upper() in keys):
                continue
            proved = True
        else:
            # docs-check ci4 C.11: lowercase keys before 4.5.0.
            proved = upper is not None and verb == verb.lower()
        for alias in listed:
            add("methods", alias, value, verb=verb.upper(), verb_proved=proved)
    filters = array_items(props["filters"]) if props.get("filters") is not None else []
    if filters is None:
        raise _Blocked
    for key, value in filters:
        before = _section(value, "before")
        if before is _ABSENT:
            continue
        if literal(before) == "" or (is_array(before) and array_items(before) == []):
            # `$paths === '' || $paths === []` matches every URI (Filters.php:862).
            paths: object = ()
        else:
            listed = strings(before) if before is not UNREAD else None
            paths = UNREAD if listed is None else listed
        add("filters", literal(key) if key is not None else None, value, patterns=paths)


def _route_shape(uri: str) -> tuple[str, str, bool]:
    """`(literal prefix, literal suffix, fully literal)` of a route path, lower-cased."""
    marks = [i for i, c in enumerate(uri) if c in _ROUTE_META]
    if not marks:
        return uri, uri, True
    last = max(i for i, c in enumerate(uri) if c in _ROUTE_META_END)
    return uri[: marks[0]], uri[last + 1 :], False


def _one_pattern(uri: str, raw: str) -> str:
    """Whether a filter pattern matches every URL of the route: "all", "none" or "undecided".

    docs-check ci4 C.11: patterns are relative to baseURL, `*` is `.*`, lower-cased, anchored.
    """
    pattern = raw.strip("/ ").lower()
    prefix, suffix, whole = _route_shape(uri)
    marks = [i for i, c in enumerate(pattern) if c in _PATTERN_META]
    star = pattern.find("*")
    if marks:
        cut = marks[0] if star < 0 else min(marks[0], star)
        head = pattern[:cut]
        return "none" if not (prefix.startswith(head) or head.startswith(prefix)) else "undecided"
    if whole:
        regex = ".*".join(re.escape(part) for part in pattern.split("*"))
        return "all" if re.fullmatch(regex, uri) else "none"
    head = pattern if star < 0 else pattern[:star]
    tail = pattern if star < 0 else pattern[pattern.rfind("*") + 1 :]
    if not (prefix.startswith(head) or head.startswith(prefix)):
        return "none"
    if not (suffix.endswith(tail) or tail.endswith(suffix)):
        return "none"
    if star >= 0 and set(pattern[star:]) == {"*"} and prefix.startswith(head):
        return "all"
    return "undecided"


def covers(uri: str, patterns) -> str:
    """`all` when one pattern matches every URL, `none` when none matches any, else undecided."""
    results = [_one_pattern(uri, p) for p in patterns]
    if "all" in results:
        return "all"
    if all(r == "none" for r in results):
        return "none"
    return "undecided"


def _applies(run: _Run, rule: _Rule, route: _Route) -> str:
    """`yes`, `no` or `undecided` for one config rule on one route, counting the undecided."""
    if rule.section == "required" or (rule.section == "globals" and rule.patterns is None):
        return "yes"
    if rule.section == "filters" and rule.patterns == ():
        return "yes"
    if rule.section == "methods":
        # A `*` route serves the verbs no verb route on its key holds: its declaration's.
        verbs = set(route.declaration.methods)
        if rule.verb not in verbs:
            return "no"
        if not rule.verb_proved:
            run.out.count(REASON_FILTER_METHODS_CASE)
            return "undecided"
        if verbs == {rule.verb}:
            return "yes"
        run.out.count(REASON_FILTER_METHODS_PARTIAL)
        return "undecided"
    globals_ = rule.section == "globals"
    reason = REASON_FILTER_EXCEPT_UNDECIDED if globals_ else REASON_FILTER_PATTERN_UNDECIDED
    decided = "undecided" if rule.patterns is UNREAD else covers(route.uri, rule.patterns)
    if decided == "undecided":
        run.out.count(reason)
        return "undecided"
    if rule.section == "globals":
        # `except` patterns: a route every URL of which is excepted does not get the filter.
        return "no" if decided == "all" else "yes"
    return "yes" if decided == "all" else "no"


# --- controller attributes -----------------------------------------------------------------


def _attributes_mode(run: _Run) -> str:
    """`absent` before 4.7.0, `off`, `unread`, or `on`.

    The default is true (system/Config/Routing.php:106) from 4.7.0 (v4.7.0.md:286). Only a
    literal `false` turns them off (`=== false`, system/Router/Router.php:781).
    """
    if run.version is not None and run.version < CONTROLLER_ATTRIBUTES:
        return "absent"
    if run.attributes_setting is False:
        return "off"
    if _at_least(run, CONTROLLER_ATTRIBUTES) is None or run.attributes_setting is UNREAD:
        return "unread"
    return "on"


def _attribute_guards(run: _Run, routes: list[_Route]) -> set[tuple[str, str | None]]:
    """`#[Filter]` on handler classes and methods; the holders that carry one.

    `Router::processRouteAttributes()` (system/Router/Router.php:776-826) skips closures
    (:786) and reads class then method attributes; `getFilters()` appends their filters after
    the route's (:253-266). `#[Restrict]` is a route condition (a 404), counted, never a guard.
    `#[Cache]` is ignored. Unread mode counts once, publishes no guard, and leaves the routes
    of a `#[Filter]` holder undecided rather than unguarded.
    """
    handled: dict[str, dict[str, set[str]]] = {}
    for route in routes:
        declaration = route.declaration
        if declaration.is_closure or not declaration.handler_file:
            continue
        by_class = handled.setdefault(declaration.handler_file, {})
        by_class.setdefault(fold(declaration.controller_class), set()).add(
            declaration.controller_method
        )
    mode = run.attributes_mode = _attributes_mode(run)
    if mode == "unread":
        run.out.count(REASON_ATTRIBUTES_SETTING_UNREAD)
    holders: set[tuple[str, str | None]] = set()
    for path, classes in sorted(handled.items()):
        facts = run.ctx.facts(path)
        for fact in facts.attributes if facts is not None else ():
            kind = {fold(FILTER_ATTRIBUTE): "Filter", fold(RESTRICT_ATTRIBUTE): "Restrict"}.get(
                fold(fact.fqcn)
            )
            methods = classes.get(fold(fact.class_fqn))
            if kind is None or methods is None:
                continue
            if fact.target == "class":
                member = None
            elif fact.target == "method" and fact.member in methods:
                member = fact.member
            else:
                continue
            if mode == "absent":
                run.out.count(REASON_ATTRIBUTES_VERSION)
            elif mode == "off":
                run.out.count(REASON_ATTRIBUTES_DISABLED)
            elif kind == "Restrict":
                run.out.count(REASON_ATTRIBUTE_RESTRICT)
            elif mode == "unread":
                holders.add((fold(fact.class_fqn), member))
            else:
                guard = _attribute_guard(run, fact, member, path)
                if guard is not None:
                    run.out.guards.append(guard)
    return holders


def _attribute_guard(run: _Run, fact, member, path: str) -> AccessGuard | None:
    """`Filter(string $by, array $having = [])` (Filter.php:44-47) as the route filter spec
    `getFilters()` builds: `by`, or `by:` plus `having` joined by `,` (Filter.php:65-72)."""
    named = dict(fact.named)
    if set(named) - {"by", "having"}:
        run.out.count(REASON_ATTRIBUTE_NOT_LITERAL)
        return None
    by = named.get("by") or (fact.positional[0] if fact.positional else None)
    having = named.get("having") or (fact.positional[1] if len(fact.positional) > 1 else None)
    args: tuple[str, ...] = ()
    if having is not None:
        if having.value is not None:
            # A string for `array $having` is a TypeError; the Router logs it and drops the
            # attribute (Router.php:799-806).
            run.out.count(REASON_ATTRIBUTE_INVALID)
            return None
        if not _LIST_TEXT.fullmatch(having.text.strip()):
            run.out.count(REASON_ATTRIBUTE_NOT_LITERAL)
            return None
        args = tuple(re.findall(r"'([^'\\]*)'", having.text))
    if by is None or not by.value:
        run.out.count(REASON_ATTRIBUTE_NOT_LITERAL)
        return None
    label = by.value + (":" + ",".join(args) if args else "")
    if spec_unread(label):
        run.out.count(REASON_FILTER_SPEC_UNREAD)
        return None
    return AccessGuard(
        fact.class_fqn, member, path, fact.line, label, True, provider=NAME,
        via=VIA_ATTRIBUTE, guard_attribute="Filter", guard_kind=_filter_kind(run, label),
    )  # fmt: skip  # fmt: skip


# --- publication ---------------------------------------------------------------------------


def _tables(route: _Route) -> set[str]:
    return {"*"} if route.any_verb else set(route.declaration.methods)


def _deduplicate(run: _Run) -> list[_Route]:
    """docs-check ci4 B.10: per verb table, the first route of a key wins.

    A key shared with an environment or hostname route depends on the runtime, so its
    unconditional routes in that table are refused. Across tables, `_verb_over_any` applies.
    A `view()` takes part as a GET route and is dropped after: a route that loses a table to it
    is counted under `REASON_VIEW_COLLISION`. `addRedirect()` is `_redirected`.
    """
    order = {id(r): i for i, r in enumerate(run.routes)}
    by_key: dict[str, list[_Route]] = {}
    for route in run.routes:
        by_key.setdefault(route.key, []).append(route)
    kept: list[_Route] = []
    for routes in by_key.values():
        routes = _redirected(run, routes)
        viewed = {m for r in routes if r.special == SPECIAL_VIEW for m in r.declaration.methods}
        held = {m for r in routes if not r.any_verb for m in r.declaration.methods}
        conditional: set[str] = set()
        conditional_views: set[str] = set()
        for route in routes:
            if route.conditional:
                conditional |= _tables(route)
                if route.special:
                    conditional_views |= _tables(route)
        taken: dict[str, _Route] = {}
        here: list[_Route] = []
        certain: set[str] = set()
        for route in routes:
            tables = _tables(route)
            if route.conditional == _ENV:
                continue
            if route.conditional == _HOST:
                here.append(route)
                continue
            if tables & conditional:
                if not route.special:
                    by_view = bool(tables & conditional_views)
                    run.out.count(
                        REASON_VIEW_COLLISION if by_view else REASON_DUPLICATE_CONDITIONAL
                    )
                continue
            lost = tables & set(taken)
            left = tables - lost
            if lost and not route.special:
                by_view = any(taken[t].special == SPECIAL_VIEW for t in lost)
                run.out.count(REASON_VIEW_COLLISION if by_view else REASON_DUPLICATE)
            for table in left:
                taken[table] = route
            if not left:
                continue
            if left != tables:
                methods = tuple(m for m in route.declaration.methods if m in left)
                route.declaration = replace(route.declaration, methods=methods)
            if not route.any_verb:
                certain |= left
            here.append(route)
        kept.extend(_verb_over_any(run, routes, here, held, certain, viewed))
    kept = [r for r in kept if not r.special]
    kept.sort(key=lambda r: order[id(r)])
    return kept


def _redirected(run: _Run, routes: list[_Route]) -> list[_Route]:
    """The routes of one key an `addRedirect()` leaves served: none, or all but the redirects.

    `create()` sets `routes['*'][$routeKey]['redirect']` (system/Router/RouteCollection.php:1428)
    only after the first-wins return (:1413-1415), and `checkRoutes()` asks `isRedirect()`
    (:617) for every matched route of every verb (system/Router/Router.php:461). So a redirect
    turns each route on its key into a redirect, whatever the order, unless an unconditional `*`
    route came first and holds `routes['*'][$routeKey]`. A conditional redirect may register.
    """
    redirects = [r for r in routes if r.special == SPECIAL_REDIRECT]
    rest = [r for r in routes if r.special != SPECIAL_REDIRECT]
    if not redirects:
        return routes

    def cancelled(redirect: _Route) -> bool:
        if redirect.conditional:
            return False
        at = next(i for i, r in enumerate(routes) if r is redirect)
        return any(r.any_verb and not r.conditional and not r.special for r in routes[:at])

    if all(cancelled(r) for r in redirects):
        return rest
    for route in rest:
        if not route.special and route.conditional != _ENV:
            run.out.count(REASON_REDIRECT_COLLISION)
    return []


def _verb_over_any(
    run: _Run, routes, here, held: set[str], certain: set[str], viewed: set[str]
) -> list[_Route]:
    """A verb route beats a `*` route on its key; the `*` route serves the other verbs.

    `$this->routes[$verb] + $this->routes['*']` (system/Router/RouteCollection.php:504): a
    PHP array union, so the verb table's entry for a key wins. `loadRoutesOptions()`
    (:1568-1575) then fills the verb route's missing option keys from the `*` route's, and
    `getFiltersForRoute()` (:1159-1172) reads `filter` from that result: a verb route with no
    `filter` key runs the `*` route's filters.
    """
    star = [r for r in routes if r.any_verb]
    if not star or not held:
        return here
    out: list[_Route] = []
    for route in here:
        if route.special:
            out.append(route)
            continue
        if route.any_verb:
            methods = tuple(m for m in route.declaration.methods if m not in held)
            lost = set(route.declaration.methods) & held
            if lost & viewed:
                # A `view()` holds GET in the GET table, which `getRoutes()` reads first.
                run.out.count(REASON_VIEW_COLLISION)
            elif lost & (held - certain):
                # A verb whose verb table is runtime-dependent: no claim either way.
                run.out.count(REASON_DUPLICATE_CONDITIONAL)
            if not methods:
                if not lost & viewed:
                    run.out.count(REASON_DUPLICATE)
                continue
            route.declaration = replace(route.declaration, methods=methods)
            out.append(route)
            continue
        out.append(route)
        if route.filter_key is True:
            continue
        first = star[0]
        if not any(r.conditional for r in star):
            if first.filter_key is False:
                continue
            if first.filter_key is True and route.filter_key is False:
                route.filters = first.filters
                route.unclaimed = route.unclaimed or first.unclaimed
                continue
        elif all(r.filter_key is False for r in star):
            continue
        run.out.count(REASON_ANY_OPTIONS_UNDECIDED)
        route.unclaimed = True
    return out


def _attribute_authorized(run: _Run, declaration: RouteDeclaration) -> bool:
    """Whether a published `#[Filter]` authorization guard sits on the route's handler."""
    holder = fold(declaration.controller_class)
    return any(
        guard.guard_kind == GUARD_KIND_AUTHORIZATION
        and fold(guard.holder_class) == holder
        and guard.holder_method in (None, declaration.controller_method)
        for guard in run.out.guards
    )


def _publish(run: _Run) -> None:
    out = run.out
    routes = _deduplicate(run)
    attributed = _attribute_guards(run, routes)
    filters_file = _spelling(run, FILTERS)
    pending: list[tuple[RouteDeclaration, dict[str, RouteGuard], str]] = []
    for route in routes:
        declaration = route.declaration
        guards: dict[str, RouteGuard] = {}
        undecided = route.unclaimed
        # Authorization from a guard that runs for every request, and from a `$methods` rule.
        deciding = bypassable = False
        for text in route.filters:
            kind = _filter_kind(run, text)
            deciding = deciding or kind == GUARD_KIND_AUTHORIZATION
            key = AccessGuard("", None, "", 0, text, True, provider=NAME, guard_kind=kind).node_key
            guards[key] = RouteGuard(
                route_key=declaration.node_key, node_key=key, label=text,
                guard_attribute=FILTER, via=VIA_FILTER, source_file=declaration.source_file,
                line=declaration.line, provider=NAME, guard_kind=kind,
            )  # fmt: skip
        for rule in run.rules:
            decided = _applies(run, rule, route)
            if decided == "undecided" and rule.kind in _RISKY:
                undecided = True
            if rule.unread and decided != "no":
                # A filter runs, but which one and with which arguments is not read.
                undecided = True
                continue
            if decided != "yes":
                continue
            # `processMethods()` looks up the raw request method (system/Filters/Filters.php:723)
            # while routing upper-cases it (system/Router/RouteCollection.php:554): a `post`
            # request reaches a POST route and skips `$methods['POST']`.
            bypassed = rule.section == "methods" and rule.kind == GUARD_KIND_AUTHORIZATION
            bypassable = bypassable or bypassed
            deciding = deciding or (rule.kind == GUARD_KIND_AUTHORIZATION and not bypassed)
            key = config_rule_key(NAME, FILTERS, rule.index)
            guards[key] = RouteGuard(
                route_key=declaration.node_key, node_key=key, label=rule.label,
                guard_attribute=FILTER, via=VIA_FILTER_CONFIG, source_file=filters_file,
                line=rule.line, provider=NAME, guard_kind=rule.kind,
                extra_node_fields=(("filter_section", rule.section),), decides=not bypassed,
            )  # fmt: skip
        if bypassable and not deciding and not _attribute_authorized(run, declaration):
            run.out.count(REASON_METHODS_CASE_BYPASS)
            undecided = True
        why = ""
        holder = fold(declaration.controller_class)
        if run.filters_blocked or (
            run.attributes_mode == "unread"
            and not declaration.is_closure
            and ({(holder, None), (holder, declaration.controller_method)} & attributed)
        ):
            why = AUTHORIZATION_ACCESS_CONTROL_BLOCKED
        elif undecided:
            why = AUTHORIZATION_ACCESS_CONTROL_UNDECIDED
        pending.append((declaration, guards, why))

    # One route node per `node_key`, as the pass publishes them: only the guards every
    # declaration sharing it carries are proved, and any undecided one makes it undecided.
    shared: dict[str, list[tuple[dict[str, RouteGuard], str]]] = {}
    for declaration, guards, why in pending:
        out.routes.append(declaration)
        shared.setdefault(declaration.node_key, []).append((guards, why))
    for key, entries in shared.items():
        common = set.intersection(*(set(g) for g, _ in entries))
        out.route_guards.extend(g for k, g in entries[0][0].items() if k in common)
        why = next((w for _, w in entries if w), "")
        if why:
            out.undecided += 1
            out.route_undecided[key] = why
