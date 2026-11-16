"""Laravel: routes files, route and controller middleware, and the handler grant calls.

What is read, and from where:

- `bootstrap/app.php` `->withRouting(web:, api:, apiPrefix:)` names the route files the
  application loads. `web:` files are loaded under the `web` middleware group, `api:` files
  under the `api` group and the `apiPrefix` (default `api`). A routes file is read only when it
  is proved loaded: named there, or `require`d / `->group()`ed from a file that is. A file under
  `routes/` with `Route::` calls that nothing loads is counted, never guessed.
- `Route::get|post|put|patch|delete|options|any|match`, `Route::resource|apiResource|
  resources|apiResources`, and groups (`Route::prefix()->...->group()`, `Route::group([...])`)
  with Laravel's own composition: the prefix is `trim(old, '/') . '/' . trim(new, '/')`, the
  route name prefix (`as`) is plain concatenation, middleware lists are merged in order, an
  inner `controller` overrides the outer one and a `namespace` joins the outer one unless it
  starts with `\\`. Route-level `->prefix()` and `->name()` apply to the built route, in order.
- `Route::singleton|apiSingleton`, nested `photos.comments` resources and `->shallow()`.
- `any` is the installed version's `Router::$verbs` (`composer.lock`), v13.35.0 adding QUERY.
  Features are gated at minor precision (`SINCE_*`).
- Route middleware is a route-scoped guard. `HasMiddleware::middleware()` on a controller, and
  13.x `#[Middleware]` / `#[Authorize]` / `#[WithoutMiddleware]`, are method guards on each
  handler method they apply to. `$this->authorize()` and `Gate::*` in a handler body are grant
  calls.

Strings are runtime values, so a `'Controller@method'` action names a class only when the
string is fully qualified, after the group namespace is prepended: a `use` line does not apply
to a string. Every other form is refused with a fixed `laravel-*` reason and produces no edge.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace

from ..syntax import nodes
from ..syntax.attributes import (
    TARGET_CLASS,
    TARGET_METHOD,
    attribute_fqcn,
    declared_attributes,
    fold,
    resolve_class_name,
)
from ..syntax.routes import (
    GUARD_KIND_AUTHENTICATION,
    GUARD_KIND_AUTHORIZATION,
    GUARD_KIND_MIDDLEWARE,
    HANDLER_KIND_CLOSURE,
    HANDLER_KIND_METHOD,
    HANDLER_KIND_UNRESOLVED,
    STATIC_RECEIVER,
    TEMPLATE_REQUIREMENT_UNREAD,
    THIS_RECEIVER,
    AccessGuard,
    GrantCall,
    RouteDeclaration,
    arguments,
    array_items,
    brace_template,
    class_constant_name,
    collect_access_checks,
    is_array,
    literal,
    pick,
    requirement_may_match_slash,
    strings,
)
from .provider import ProviderContext, ProviderResult, RouteGuard

NAME = "laravel"

ROUTE_FACADE = "Illuminate\\Support\\Facades\\Route"
GATE_FACADE = "Illuminate\\Support\\Facades\\Gate"
HAS_MIDDLEWARE = "Illuminate\\Routing\\Controllers\\HasMiddleware"
CONTROLLER_MIDDLEWARE = "Illuminate\\Routing\\Controllers\\Middleware"
# docs-check laravel C.10: 13.x controller attributes, class and method targets, repeatable.
ATTRIBUTE_MIDDLEWARE = "Illuminate\\Routing\\Attributes\\Controllers\\Middleware"
ATTRIBUTE_WITHOUT_MIDDLEWARE = "Illuminate\\Routing\\Attributes\\Controllers\\WithoutMiddleware"
ATTRIBUTE_AUTHORIZE = "Illuminate\\Routing\\Attributes\\Controllers\\Authorize"
_CONTROLLER_ATTRIBUTES = {
    fold(c): c for c in (ATTRIBUTE_MIDDLEWARE, ATTRIBUTE_WITHOUT_MIDDLEWARE, ATTRIBUTE_AUTHORIZE)
}
# `Route` with no import in a namespace-less routes file: Laravel's default facade alias.
_ROUTE_NAMES = frozenset({fold(ROUTE_FACADE), "route"})
_BARE_ROUTE = "route"

BOOTSTRAP = "bootstrap/app.php"
APP_CONFIG = "config/app.php"
COMPOSER_LOCK = "composer.lock"
FRAMEWORK_PACKAGE = "laravel/framework"
ROUTES_DIR = "routes"
DEFAULT_API_PREFIX = "api"

# `_via` and `guard_attribute` of every middleware guard, route-scoped or on a controller.
MIDDLEWARE = "middleware"
VIA_ROUTE_FILE = "route_file"
VIA_ATTRIBUTE = "attribute"

# Middleware aliases by what they do, keyed by the alias before any `:parameters`.
# docs-check laravel C.12: only `can` is authorization; these are sign-in checks.
AUTHENTICATION_ALIASES = frozenset(
    {"auth", "auth.basic", "auth.session", "guest", "verified", "password.confirm"}
)
AUTHORIZATION_ALIASES = frozenset({"can"})
AUTHENTICATION_CLASSES = frozenset(
    fold(c)
    for c in (
        "Illuminate\\Auth\\Middleware\\Authenticate",
        "Illuminate\\Auth\\Middleware\\AuthenticateWithBasicAuth",
        "Illuminate\\Auth\\Middleware\\EnsureEmailIsVerified",
        "Illuminate\\Auth\\Middleware\\RedirectIfAuthenticated",
        # docs-check laravel C.12: the `password.confirm` alias.
        "Illuminate\\Auth\\Middleware\\RequirePassword",
    )
)

# `authorize` throws on refusal; `allows`, `denies`, `check`, `any` and `none` return a bool.
# docs-check laravel C.11: `check`, `any` and `none` take `$abilities`. `allowIf` / `denyIf`
# take no ability string, so they are not grant calls here.
_ABILITY = (0, "ability")
_ABILITIES = (0, "abilities")
_ARGUMENTS = (1, "arguments")
GRANT_CALLS = {
    (THIS_RECEIVER, "authorize"): GrantCall(False, _ABILITY, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "authorize"): GrantCall(False, _ABILITY, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "allows"): GrantCall(True, _ABILITY, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "denies"): GrantCall(True, _ABILITY, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "check"): GrantCall(True, _ABILITIES, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "any"): GrantCall(True, _ABILITIES, _ARGUMENTS),
    (STATIC_RECEIVER + GATE_FACADE, "none"): GrantCall(True, _ABILITIES, _ARGUMENTS),
}

# `Router::$verbs` up to 12.x; a GET route also answers HEAD (`Route::__construct`).
ALL_VERBS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
# docs-check laravel B.4: QUERY joins `Router::$verbs`, and `Route::query()` exists, from v13.35.0.
ALL_VERBS_13 = (*ALL_VERBS, "QUERY")
# `(major, minor)` of the first release with each feature, from the framework CHANGELOG.md:
# QUERY and `Router::query()` (:243), `#[Middleware]` / `#[Authorize]` (:1577-1578), parent
# class attributes (:1347), `#[WithoutMiddleware]` (:789).
SINCE_QUERY = (13, 35)
SINCE_CONTROLLER_ATTRIBUTES = (13, 0)
SINCE_ATTRIBUTE_INHERITANCE = (13, 5)
SINCE_WITHOUT_MIDDLEWARE_ATTRIBUTE = (13, 20)
# `Illuminate\Routing\Controller` declares no attribute (Routing/Controller.php:7), so an
# inherited-attribute walk may stop there.
BASE_CONTROLLER = "Illuminate\\Routing\\Controller"
VERB_METHODS = {
    "get": ("GET", "HEAD"),
    "post": ("POST",),
    "put": ("PUT",),
    "patch": ("PATCH",),
    "delete": ("DELETE",),
    "options": ("OPTIONS",),
    "any": ALL_VERBS,
}

# `ResourceRegistrar`'s default actions: (method, verbs, suffix). `{}` is the parameter.
RESOURCE_ACTIONS = (
    ("index", ("GET", "HEAD"), ""),
    ("create", ("GET", "HEAD"), "/create"),
    ("store", ("POST",), ""),
    ("show", ("GET", "HEAD"), "/{}"),
    ("edit", ("GET", "HEAD"), "/{}/edit"),
    ("update", ("PUT", "PATCH"), "/{}"),
    ("destroy", ("DELETE",), "/{}"),
)
API_RESOURCE_ACTIONS = ("index", "store", "show", "update", "destroy")
# docs-check laravel B.8: with `->shallow()` these drop the parent segments and name prefix.
_SHALLOW_ACTIONS = frozenset({"show", "edit", "update", "destroy"})
# The actions whose URI segment `ResourceRegistrar::$verbs` sets (ResourceRegistrar.php:56-59).
_VERB_SEGMENT_ACTIONS = frozenset({"create", "edit"})
# docs-check laravel B.8: singleton actions, in `ResourceRegistrar` order: (method, verbs,
# suffix). `show, edit, update` by default; `creatable()` adds create, store and destroy,
# `destroyable()` adds destroy; `apiSingleton` keeps only store, show, update and destroy.
SINGLETON_ACTIONS = (
    ("show", ("GET", "HEAD"), ""),
    ("edit", ("GET", "HEAD"), "/edit"),
    ("update", ("PUT", "PATCH"), ""),
    ("create", ("GET", "HEAD"), "/create"),
    ("store", ("POST",), ""),
    ("destroy", ("DELETE",), ""),
)
_SINGLETON_DEFAULTS = ("show", "edit", "update")
API_SINGLETON_ACTIONS = ("store", "show", "update", "destroy")

# Fixed refusal reasons. A reason never carries a variable.
REASON_BOOTSTRAP_UNREAD = "laravel-routing-bootstrap-unread"
REASON_ROUTING_USING = "laravel-routing-using-closure"
REASON_ROUTING_THEN = "laravel-routing-then-unread"
REASON_ROUTING_FILE_NOT_LITERAL = "laravel-routing-file-not-literal"
REASON_API_PREFIX_NOT_LITERAL = "laravel-api-prefix-not-literal"
REASON_FILE_NOT_LOADED = "laravel-routes-file-not-loaded"
REASON_FILE_UNREADABLE = "laravel-routes-file-unreadable"
REASON_REQUIRE_NOT_LITERAL = "laravel-require-not-literal"
REASON_IN_CONTROL_FLOW = "laravel-route-in-control-flow"
REASON_RECEIVER_VARIABLE = "laravel-route-receiver-variable"
REASON_METHOD_UNKNOWN = "laravel-route-method-unknown"
REASON_NO_CONTROLLER = "laravel-route-no-controller"
REASON_FALLBACK = "laravel-fallback-route"
REASON_GLOBAL_PATTERN = "laravel-route-pattern-global"
REASON_MODIFIER_UNKNOWN = "laravel-route-modifier-unknown"
REASON_PATH_NOT_LITERAL = "laravel-path-not-literal"
REASON_PREFIX_NOT_LITERAL = "laravel-prefix-not-literal"
REASON_NAME_NOT_LITERAL = "laravel-name-not-literal"
REASON_VERBS_NOT_LITERAL = "laravel-verbs-not-literal"
# Counted for every closure action, which is published as a route with no handler node.
REASON_CLOSURE_ACTION = "laravel-closure-action"
# `_action`'s answer for a closure: no class, no method.
_CLOSURE = ("", "")
# `_action`'s answer for a proved path whose handler is not bound: published `unresolved`.
_UNRESOLVED = ("", "\0unresolved")
REASON_ACTION_UNREADABLE = "laravel-action-unreadable"
REASON_STRING_ACTION_SHORT = "laravel-string-action-not-qualified"
# A `namespace` set by a registrar chain straight before a verb (`Route::namespace(..)->get()`),
# which is not a group: whether Laravel applies it to a string action is not proved here.
REASON_STRING_ACTION_NAMESPACE = "laravel-string-action-namespace-group"
# docs-check laravel B.5, B.6: a group `namespace` that is not a literal string.
REASON_NAMESPACE_NOT_LITERAL = "laravel-group-namespace-not-literal"
# docs-check laravel B.6: in a `controller` group, a string without `@` is prefixed with the
# group controller unless `class_exists()` says it is a class, which this rule cannot decide.
REASON_CONTROLLER_GROUP_CLASS = "laravel-controller-group-class-string"
REASON_CONTROLLER_NOT_LITERAL = "laravel-group-controller-not-literal"
REASON_GROUP_ATTRIBUTE_UNKNOWN = "laravel-group-attribute-unknown"
REASON_GROUP_CALLBACK_UNREAD = "laravel-group-callback-unread"
REASON_MIDDLEWARE_NOT_LITERAL = "laravel-middleware-not-literal"
REASON_CAN_NOT_LITERAL = "laravel-can-not-literal"
REASON_WITHOUT_NOT_LITERAL = "laravel-without-middleware-not-literal"
REASON_WITHOUT_UNMATCHED = "laravel-without-middleware-unmatched"
REASON_WHERE_NOT_LITERAL = "laravel-where-not-literal"
REASON_HANDLER_AMBIGUOUS = "laravel-handler-class-ambiguous"
REASON_ROUTE_KEY_GUARDS_DIFFER = "laravel-route-key-guards-differ"
REASON_RESOURCE_OPTION_NOT_LITERAL = "laravel-resource-option-not-literal"
REASON_RESOURCE_OPTION_UNKNOWN = "laravel-resource-option-unknown"
REASON_RESOURCE_NAME_NOT_LITERAL = "laravel-resource-name-not-literal"
REASON_RESOURCE_NESTED = "laravel-resource-nested"
REASON_RESOURCE_PARAMETER_UNPROVED = "laravel-resource-parameter-unproved"
REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL = "laravel-controller-middleware-not-literal"
REASON_CONTROLLER_MIDDLEWARE_CLOSURE = "laravel-controller-middleware-closure"
REASON_CONTROLLER_MIDDLEWARE_UNPROVED = "laravel-controller-middleware-unproved"
REASON_CONTROLLER_CONSTRUCTOR_MIDDLEWARE = "laravel-controller-constructor-middleware"
REASON_CONTROLLER_MIDDLEWARE_EXCLUDED = "laravel-controller-middleware-excluded-by-route"
# docs-check laravel C.10: an attribute argument (middleware, ability, only, except) that is
# not a literal of the documented shape.
REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL = "laravel-controller-attribute-not-literal"
# docs-check laravel C.10: the attributes exist from 13.x; an installed 12.x or older has none.
REASON_CONTROLLER_ATTRIBUTE_VERSION = "laravel-controller-attribute-before-13"
# docs-check laravel B.4: no `laravel/framework` version read from `composer.lock`. Counted
# once, and only where the version changes what is read.
REASON_VERSION_UNREAD = "laravel-version-unread"
# docs-check laravel A.2: `health:` is a bare GET route with no group, served by the framework.
REASON_HEALTH_ROUTE = "laravel-health-route"
# docs-check laravel A.1: `config/app.php` lists its aliases literally and `Route` is not the
# facade there, so a bare `Route::` in a namespace-less file is not proved to be the facade.
REASON_ROUTE_ALIAS_REMOVED = "laravel-route-alias-removed"
# `Route::controller(X)->get('b', 'm')`: `Router::convertToControllerAction` reads only the
# group stack and overwrites `controller` (Routing/Router.php:635, :642), so X is not bound.
# The route is published with `handler_kind: unresolved`. Laravel rejects it at registration:
# `uses` `m` has no `@`, so `makeInvokable` throws "Invalid route action" (RouteAction.php:92).
REASON_REGISTRAR_CONTROLLER = "laravel-registrar-controller-rejected-by-framework"
# A registrar `prefix`, `name`, `where`, `namespace` or `controller` straight before a resource
# reaches `ResourceRegistrar` as an option (Routing/RouteRegistrar.php:175), which reads none
# of them as a group would.
REASON_RESOURCE_REGISTRAR_ATTRIBUTE = "laravel-resource-registrar-attribute"
# `Route::resourceVerbs([...])` rewrites the `create` / `edit` URI segments for the whole app
# (Routing/Router.php:1422, ResourceRegistrar.php:738-745).
REASON_RESOURCE_VERBS = "laravel-resource-verbs-changed"
# `#[WithoutMiddleware]` exists from v13.20.0; an older 13.x ignores it.
REASON_WITHOUT_ATTRIBUTE_VERSION = "laravel-without-middleware-attribute-before-13-20"
# A controller attribute on a parent class (read from v13.5.0) or on an inherited handler
# method: counted, its guards not published.
REASON_CONTROLLER_ATTRIBUTE_INHERITED = "laravel-controller-attribute-inherited"
# A parent class outside the corpus may carry controller attributes.
REASON_CONTROLLER_PARENT_UNREAD = "laravel-controller-parent-unread"

_CONTROL_FLOW = frozenset(
    {
        "if_statement",
        "foreach_statement",
        "for_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
        "match_expression",
    }
)
_REGISTRAR = frozenset(
    {
        "prefix",
        "middleware",
        "name",
        "as",
        "controller",
        "domain",
        "where",
        "withoutMiddleware",
        "namespace",
        "scopeBindings",
        "withoutScopedBindings",
    }
)
# `RouteRegistrar::$aliases` (Routing/RouteRegistrar.php:94-99): the key a method stores under.
_REGISTRAR_KEYS = {
    "name": "as",
    "scopeBindings": "scope_bindings",
    "withoutScopedBindings": "scope_bindings",
}
# Registrar keys `ResourceRegistrar` does not apply as a group would (RouteRegistrar.php:175;
# ResourceRegistrar.php:637-697 reads only `as`, `middleware`, `excluded_middleware`,
# `wheres`, `missing` and `metadata`, and `as` with a `.` separator).
_RESOURCE_UNREAD_KEYS = frozenset({"prefix", "as", "where", "namespace", "controller"})
# Group array keys and the registrar method each one means.
# docs-check laravel B.6: an array group takes the keys AFTER `RouteRegistrar` aliasing (`as`,
# `scope_bindings`, `excluded_middleware`). A raw `withoutMiddleware` key is not read by
# Laravel, so it is an unknown attribute here.
_GROUP_KEYS = {
    "prefix": "prefix",
    "middleware": "middleware",
    "as": "as",
    "namespace": "namespace",
    "domain": "domain",
    "where": "where",
    "excluded_middleware": "withoutMiddleware",
    "controller": "controller",
    "scope_bindings": "scopeBindings",
}
# Route modifiers that change neither the path, the handler nor the middleware.
_INERT_MODIFIERS = frozenset(
    {"domain", "scopeBindings", "withoutScopedBindings", "withTrashed", "missing"}
)
_WHERE_KEEP = frozenset(
    {"whereNumber", "whereAlpha", "whereAlphaNumeric", "whereUuid", "whereUlid"}
)
_SINGLETONS = frozenset({"singleton", "apiSingleton"})
_TERMINALS = frozenset(
    {*VERB_METHODS, "query", "match", "resource", "apiResource", "resources", "apiResources",
     "group", *_SINGLETONS}
)  # fmt: skip
_NO_CONTROLLER = frozenset({"redirect", "permanentRedirect", "view"})
_CONFIG_CALLS = frozenset({"macro", "mixin", "bind", "model", "aliasMiddleware"})
_PATTERN_CALLS = frozenset({"pattern", "patterns"})
_RESOURCE_OPTIONS = frozenset(
    {"only", "except", "names", "name", "parameters", "parameter", "middleware",
     "withoutMiddleware", "shallow"}
)  # fmt: skip
_SINGLETON_OPTIONS = frozenset(
    {"only", "except", "names", "name", "middleware", "withoutMiddleware", "creatable",
     "destroyable"}
)  # fmt: skip
# Plurals whose singular is not the word minus its final `s`, or not certain to be.
_UNSAFE_PLURAL_ENDINGS = (
    "ss", "us", "is", "ies", "ves", "oes", "ses", "xes", "zes", "ches", "shes",
)  # fmt: skip
_UNCOUNTABLE = frozenset({"news", "series", "species", "means", "chassis", "bus", "gas"})
_BINDING_FIELD = re.compile(r"\{(\w+):\w+(\?)?\}")
_PLACEHOLDER_NAME = re.compile(r"\{(\w+)\??\}")


@dataclass(frozen=True)
class _Middleware:
    """One middleware entry as written, or a `can` ability. `label` is what the node shows."""

    text: str
    label: str
    kind: str
    subject: str
    source_file: str
    line: int


@dataclass(frozen=True)
class _Group:
    """The attributes in force at one point of a routes file."""

    prefix: str = ""
    prefix_ok: bool = True
    name: str = ""
    name_ok: bool = True
    middleware: tuple[_Middleware, ...] = ()
    without: tuple[str, ...] = ()
    without_ok: bool = True
    controller: str | None = None
    controller_ok: bool = True
    # docs-check laravel B.5, B.6: the merged group namespace, "" when none is in force, and
    # the reason that refuses every string action when it cannot be read.
    namespace: str = ""
    namespace_reason: str = ""
    wheres: tuple[tuple[str, str | None], ...] = ()


@dataclass
class _Route:
    declaration: RouteDeclaration
    middleware: list[_Middleware]
    without: tuple[str, ...]
    without_ok: bool
    handler_file: str


@dataclass
class _Run:
    ctx: ProviderContext
    out: ProviderResult
    corpus: dict[str, str]
    routes: list[_Route] = field(default_factory=list)
    loaded: set[str] = field(default_factory=set)
    loading: list[str] = field(default_factory=list)
    # The installed `laravel/framework` `(major, minor)`, None when `composer.lock` gives none;
    # minor is None for a branch alias such as `13.x-dev`.
    version: tuple[int, int | None] | None = None
    version_counted: bool = False
    # True when some file calls `resourceVerbs` / `ResourceRegistrar::verbs` with arguments.
    resource_verbs: bool = False
    # False when `config/app.php` proves the bare `Route` alias is not the facade.
    route_alias: bool = True
    # `#[WithoutMiddleware]` per `(controller, method)`, and handlers where one is unread.
    without: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    without_unread: set[tuple[str, str]] = field(default_factory=set)


class LaravelProvider:
    name = NAME
    packages = ("laravel/framework",)

    def collect(self, ctx: ProviderContext) -> ProviderResult:
        out = ProviderResult()
        run = _Run(ctx, out, _corpus_index(ctx))
        run.version = installed_version(ctx.read(COMPOSER_LOCK))
        run.route_alias = _route_alias_kept(run)
        run.resource_verbs = _resource_verbs_changed(run)
        _read_bootstrap(run)
        _count_unloaded(run)
        _publish(run)
        return out


PROVIDER = LaravelProvider()


# --- files ---------------------------------------------------------------------------------


def _rel(ctx: ProviderContext, path: str) -> str:
    """`path` relative to the repository root, `/`-separated."""
    if os.path.isabs(path):
        path = os.path.relpath(path, os.path.realpath(ctx.repo_root))
    return os.path.normpath(path).replace(os.sep, "/")


def _corpus_index(ctx: ProviderContext) -> dict[str, str]:
    """Repo-relative path -> the corpus spelling, so a route names the file the build parsed."""
    return {_rel(ctx, p): p for p in ctx.files}


def _spelling(run: _Run, rel: str) -> str:
    return run.corpus.get(rel, rel)


def _routes_files(run: _Run) -> list[str]:
    """`routes/*.php`, from the corpus and from the disk."""
    found = {rel for rel in run.corpus if _in_routes_dir(rel)}
    directory = os.path.join(run.ctx.repo_root, ROUTES_DIR)
    try:
        listed = os.listdir(directory)
    except OSError:
        listed = []
    found.update(f"{ROUTES_DIR}/{n}" for n in listed if n.endswith(".php"))
    return sorted(found)


def _in_routes_dir(rel: str) -> bool:
    head, _, tail = rel.partition("/")
    return head == ROUTES_DIR and tail.endswith(".php") and "/" not in tail


def installed_major(lock: bytes | None) -> int | None:
    """The `laravel/framework` major version in `composer.lock` `packages`, else None."""
    version = installed_version(lock)
    return version[0] if version is not None else None


def installed_version(lock: bytes | None) -> tuple[int, int | None] | None:
    """`(major, minor)` of `laravel/framework` in `composer.lock` `packages`, else None.

    docs-check laravel B.4: features are gated at minor precision (`SINCE_*`). Only the version
    is read; detection still reads `composer.json`. A branch alias (`13.x-dev`) has no minor.
    """
    if lock is None:
        return None
    try:
        data = json.loads(lock.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        return None
    for package in packages:
        if isinstance(package, dict) and package.get("name") == FRAMEWORK_PACKAGE:
            version = package.get("version")
            match = (
                re.match(r"v?(\d+)(?:\.(\d+))?(?:[.\-+]|$)", version)
                if isinstance(version, str)
                else None
            )
            if match is None:
                return None
            minor = match.group(2)
            return int(match.group(1)), int(minor) if minor is not None else None
    return None


def _since(run: _Run, since: tuple[int, int]) -> bool | None:
    """Whether the installed version is at least `since`; None, counted once, when unknown."""
    version = run.version
    if version is not None and version[0] != since[0]:
        return version[0] > since[0]
    if version is not None and version[1] is not None:
        return version[1] >= since[1]
    if version is not None and since[1] == 0:
        return True
    if not run.version_counted:
        run.version_counted = True
        run.out.count(REASON_VERSION_UNREAD)
    return None


_RESOURCE_VERBS_CALL = re.compile(
    rb"(?:resourceVerbs|ResourceRegistrar\s*::\s*verbs)\s*\(\s*[^\s)]", re.IGNORECASE
)


def _resource_verbs_changed(run: _Run) -> bool:
    """True when any corpus or routes file calls `resourceVerbs` with an argument.

    `Router::resourceVerbs` merges into the static `ResourceRegistrar::$verbs`
    (Routing/Router.php:1422-1425, ResourceRegistrar.php:738-745), usually from a service
    provider, so the whole corpus is searched as text. A comment that matches only refuses.
    """
    paths = dict.fromkeys([*run.ctx.files, *(_spelling(run, r) for r in _routes_files(run))])
    for path in paths:
        source = run.ctx.read(path)
        if source is not None and _RESOURCE_VERBS_CALL.search(source):
            return True
    return False


def _route_alias_kept(run: _Run) -> bool:
    """False only when `config/app.php` returns literal `aliases` whose `Route` is not the facade.

    docs-check laravel A.1: the alias comes from `Facade::defaultAliases()` merged with
    `app.aliases`, so a literal `aliases` array without `Route` removes it. A non-literal one
    (`Facade::defaultAliases()->merge([...])`) keeps the default.
    """
    parsed = run.ctx.parse(_spelling(run, APP_CONFIG))
    if parsed is None:
        return True
    statement = next((c for c in parsed.root.children if c.type == "return_statement"), None)
    value = next((c for c in statement.children if c.is_named), None) if statement else None
    items = array_items(value)
    if items is None:
        return True
    aliases = next((v for k, v in items if literal(k) == "aliases"), None)
    entries = array_items(aliases)
    if entries is None:
        return True
    for key, target in entries:
        name = literal(key)
        if name is None:
            return True
        if name.casefold() == _BARE_ROUTE:
            written = class_constant_name(target, parsed.names) or literal(target) or ""
            return fold(written) == fold(ROUTE_FACADE)
    return False


def _file_path(node, current_rel: str) -> str | None:
    """`__DIR__ . '/x.php'` or `base_path('x.php')` as a repo-relative path, else None."""
    if node is None:
        return None
    if node.type == "binary_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        operators = [c for c in node.children if not c.is_named]
        if (
            len(operators) == 1
            and nodes.node_text(operators[0]) == "."
            and left is not None
            and nodes.node_text(left) == "__DIR__"
        ):
            tail = literal(right)
            if tail is not None:
                return _inside(os.path.dirname(current_rel) + "/" + tail)
        return None
    if node.type == "function_call_expression":
        function = node.child_by_field_name("function")
        if nodes.node_text(function).lstrip("\\") == "base_path":
            positional, named = arguments(node)
            if len(positional) == 1 and not named:
                value = literal(positional[0])
                if value is not None:
                    return _inside(value)
    return None


def _inside(path: str) -> str | None:
    normal = os.path.normpath(path.lstrip("/")).replace(os.sep, "/")
    if normal.startswith("..") or os.path.isabs(normal):
        return None
    return normal


# --- PHP values ----------------------------------------------------------------------------


def _string_map(node) -> dict[str, str] | None:
    """`['a' => 'b', ...]` with literal keys and values only."""
    items = array_items(node)
    if items is None:
        return None
    found: dict[str, str] = {}
    for key, value in items:
        k, v = literal(key), literal(value)
        if k is None or v is None:
            return None
        found[k] = v
    return found


def _is_callable(node) -> bool:
    return node is not None and node.type in nodes.CLOSURE_KINDS


# --- bootstrap -----------------------------------------------------------------------------


def _read_bootstrap(run: _Run) -> None:
    """The route files `withRouting` loads, each read under the group Laravel gives it."""
    parsed = run.ctx.parse(_spelling(run, BOOTSTRAP))
    calls = _named_calls(parsed.root, "withRouting") if parsed is not None else []
    if len(calls) != 1:
        run.out.count(REASON_BOOTSTRAP_UNREAD)
        return
    call = calls[0]
    _, named = arguments(call)
    line = nodes.line_span(call)[0]
    if "using" in named and not _is_callable(named.get("then")):
        # `using:` replaces the default loading entirely, unless `then` is callable: then
        # `$using` is overwritten with the default callback (ApplicationBuilder.php:162-163).
        # A `then` that is not a closure literal may or may not be callable, so it is unread.
        run.out.count(REASON_ROUTING_USING)
        return
    if "then" in named:
        run.out.count(REASON_ROUTING_THEN)
    if "health" in named:
        # docs-check laravel A.2: a framework route with no controller; nothing is published.
        run.out.count(REASON_HEALTH_ROUTE)
    source = _spelling(run, BOOTSTRAP)
    web = _middleware_entry("web", source, line)
    for target in _routing_files(run, named.get("web")):
        _load(run, target, _Group(middleware=(web,)))
    api_files = _routing_files(run, named.get("api"))
    prefix = DEFAULT_API_PREFIX
    if "apiPrefix" in named:
        prefix = literal(named["apiPrefix"])
        if prefix is None:
            if api_files:
                run.out.count(REASON_API_PREFIX_NOT_LITERAL)
            return
    api = _middleware_entry("api", source, line)
    for target in api_files:
        _load(run, target, _Group(prefix=prefix.strip("/"), middleware=(api,)))


def _routing_files(run: _Run, node) -> list[str]:
    if node is None:
        return []
    items = array_items(node)
    values = [v for _, v in items] if items is not None else [node]
    found = []
    for value in values:
        target = _file_path(value, BOOTSTRAP)
        if target is None:
            run.out.count(REASON_ROUTING_FILE_NOT_LITERAL)
            continue
        found.append(target)
    return found


def _named_calls(node, name: str) -> list:
    found = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "member_call_expression" and (
            nodes.node_text(current.child_by_field_name("name")) == name
        ):
            found.append(current)
        stack.extend(current.children)
    return found


def _count_unloaded(run: _Run) -> None:
    """A routes file with `Route::` calls that nothing proved loaded is counted once."""
    for rel in _routes_files(run):
        if rel in run.loaded:
            continue
        parsed = run.ctx.parse(_spelling(run, rel))
        if parsed is not None and _has_route_call(parsed.root, parsed.names):
            run.out.count(REASON_FILE_NOT_LOADED)


def _has_route_call(node, names) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "scoped_call_expression" and _is_route_facade(current, names):
            return True
        stack.extend(current.children)
    return False


def _is_route_facade(call, names) -> bool:
    return _route_scope(call, names) in _ROUTE_NAMES


def _route_scope(call, names) -> str:
    """The case-folded class a static call's scope names, "" when it is not a name."""
    scope = call.child_by_field_name("scope")
    if scope is None or scope.type not in ("name", "qualified_name"):
        return ""
    return fold(resolve_class_name(nodes.node_text(scope).strip(), names.at(call)))


# --- routes files --------------------------------------------------------------------------


def _load(run: _Run, rel: str, group: _Group) -> None:
    if rel in run.loading:
        return
    parsed = run.ctx.parse(_spelling(run, rel))
    if parsed is None:
        run.out.count(REASON_FILE_UNREADABLE)
        return
    run.loaded.add(rel)
    run.loading.append(rel)
    try:
        _statements(run, parsed.root.children, parsed.names, rel, group)
    finally:
        run.loading.pop()


def _statements(run: _Run, statements, names, rel: str, group: _Group) -> None:
    for statement in statements:
        if statement.type == "expression_statement":
            expression = next((c for c in statement.children if c.is_named), None)
            if expression is not None:
                _expression(run, expression, names, rel, group)
        elif statement.type in _CONTROL_FLOW:
            if _has_route_call(statement, names):
                run.out.count(REASON_IN_CONTROL_FLOW)
        elif statement.type == "compound_statement":
            _statements(run, statement.children, names, rel, group)


def _expression(run: _Run, expression, names, rel: str, group: _Group) -> None:
    if expression.type in ("require_expression", "require_once_expression",
                           "include_expression", "include_once_expression"):  # fmt: skip
        operand = next((c for c in expression.children if c.is_named), None)
        target = _file_path(operand, rel)
        if target is None:
            run.out.count(REASON_REQUIRE_NOT_LITERAL)
            return
        _load(run, target, group)
        return
    chain = _chain(expression)
    if chain is None:
        return
    root, calls = chain
    if root.type != "scoped_call_expression":
        if calls[0][0] in VERB_METHODS or calls[0][0] in _REGISTRAR or calls[-1][0] == "group":
            run.out.count(REASON_RECEIVER_VARIABLE)
        return
    if not _is_route_facade(root, names):
        return
    if _route_scope(root, names) == _BARE_ROUTE and not run.route_alias:
        run.out.count(REASON_ROUTE_ALIAS_REMOVED)
        return
    index = next((i for i, (m, _) in enumerate(calls) if m in _TERMINALS), None)
    if index is None:
        _not_a_route(run, calls[0][0])
        return
    terminal, call = calls[index]
    if terminal == "query" and not _since(run, SINCE_QUERY):
        # `Router::query()` (Routing/Router.php:230) exists from v13.35.0 (CHANGELOG.md:243).
        run.out.count(REASON_METHOD_UNKNOWN)
        return
    # `RouteRegistrar::attribute` keeps one value per key, so a repeated call replaces the
    # earlier one (Routing/RouteRegistrar.php:160); only `withoutMiddleware` accumulates
    # (:146-150), and `name` is stored as `as` (:95).
    attributes: dict[str, tuple[str, object]] = {}
    withouts: list[tuple[str, object]] = []
    for method, registrar in calls[:index]:
        if method not in _REGISTRAR:
            run.out.count(REASON_METHOD_UNKNOWN)
            return
        if method == "withoutMiddleware":
            withouts.append((method, registrar))
            continue
        key = _REGISTRAR_KEYS.get(method, method) or ""
        attributes.pop(key, None)
        attributes[key] = (method, registrar)
    rest = calls[index:]
    if terminal in VERB_METHODS or terminal in ("match", "query"):
        _registrar_route(run, attributes, withouts, rest, names, rel, group)
        return
    if attributes or withouts:
        if terminal in ("resources", "apiResources"):
            # `RouteRegistrar` has no `resources` method; `__call` throws
            # (Routing/RouteRegistrar.php:322-345).
            run.out.count(REASON_METHOD_UNKNOWN)
            return
        if terminal != "group" and set(attributes) & _RESOURCE_UNREAD_KEYS:
            run.out.count(REASON_RESOURCE_REGISTRAR_ATTRIBUTE)
            return
    current = group
    for method, registrar in [*attributes.values(), *withouts]:
        current = _apply(run, current, method, registrar, names, rel)
        if current is None:
            return
    if terminal in ("resource", "apiResource"):
        _resource(run, rest, names, rel, current)
    elif terminal in _SINGLETONS:
        _singleton(run, rest, names, rel, current)
    elif terminal in ("resources", "apiResources"):
        _resources(run, rest, names, rel, current)
    elif len(rest) != 1:
        run.out.count(REASON_METHOD_UNKNOWN)
    else:
        _group(run, call, index == 0, names, rel, current)


def _registrar_route(run: _Run, attributes, withouts, calls, names, rel: str, group: _Group):
    """A verb after registrar calls: the attributes become the route action, not a group.

    `RouteRegistrar::compileAction` merges them into the action
    (Routing/RouteRegistrar.php:304). `Router::createRoute` builds the URI with the group
    prefix (Router.php:589, :710), then `Route::__construct` prepends the action `prefix`
    (Route.php:189, :829): `Route::prefix('p')->get('b')` inside group `g` is `p/g/b`.
    `RouteGroup::merge` puts group middleware first and appends the action `as` to the group
    `as` (RouteGroup.php:41-43, :160). A registrar `controller` binds nothing (Router.php:635,
    :642).
    """
    current = group
    prefix = attributes.pop("prefix", None)
    controller = attributes.pop("controller", None)
    for method, registrar in [*attributes.values(), *withouts]:
        current = _apply(run, current, method, registrar, names, rel)
        if current is None:
            return
    if "namespace" in attributes:
        # Not a group: whether a registrar `namespace` reaches a string action is unproved.
        current = replace(current, namespace_reason=REASON_STRING_ACTION_NAMESPACE)
    first = None
    if prefix is not None:
        positional, _ = arguments(prefix[1])
        first = literal(positional[0]) if positional else None
        if first is None:
            run.out.count(REASON_PREFIX_NOT_LITERAL)
            return
    _route(run, calls, names, rel, current, first, controller is not None)


def _not_a_route(run: _Run, first: str) -> None:
    if first in _NO_CONTROLLER:
        run.out.count(REASON_NO_CONTROLLER)
    elif first == "fallback":
        run.out.count(REASON_FALLBACK)
    elif first in _PATTERN_CALLS:
        run.out.count(REASON_GLOBAL_PATTERN)
    elif first not in _CONFIG_CALLS:
        # A macro, or a registrar chain that never reaches a route or `->group()`.
        run.out.count(REASON_METHOD_UNKNOWN)


def _chain(expression):
    """`(root call, [(method name, call node), ...])`, innermost first, or None."""
    calls = []
    node = expression
    while node is not None and node.type == "member_call_expression":
        calls.append((nodes.node_text(node.child_by_field_name("name")), node))
        node = node.child_by_field_name("object")
    if node is None:
        return None
    if node.type == "scoped_call_expression":
        calls.append((nodes.node_text(node.child_by_field_name("name")), node))
        calls.reverse()
        return node, calls
    if node.type == "variable_name" and calls:
        calls.reverse()
        return node, calls
    return None


# --- groups --------------------------------------------------------------------------------


def _group(run: _Run, call, with_array: bool, names, rel: str, group: _Group) -> None:
    """`->group($routes)` after registrar calls, or `Route::group(array $attributes, $routes)`."""
    positional, named = arguments(call)
    if not with_array:
        _callback(run, positional[0] if positional else named.get("routes"), names, rel, group)
        return
    items = array_items(positional[0]) if positional else None
    if items is None:
        run.out.count(REASON_GROUP_ATTRIBUTE_UNKNOWN)
        return
    current = group
    for key, value in items:
        method = _GROUP_KEYS.get(literal(key) or "")
        if method is None:
            run.out.count(REASON_GROUP_ATTRIBUTE_UNKNOWN)
            return
        current = _apply(run, current, method, value, names, rel, value_node=True)
        if current is None:
            return
    callback = positional[1] if len(positional) > 1 else named.get("routes")
    _callback(run, callback, names, rel, current)


def _callback(run: _Run, callback, names, rel: str, group: _Group) -> None:
    if callback is not None and callback.type == "anonymous_function":
        # A `$router` parameter is counted where its calls are, as a variable receiver.
        body = nodes.child(callback, "compound_statement")
        _statements(run, body.children if body is not None else [], names, rel, group)
        return
    if callback is not None and callback.type == "arrow_function":
        body = callback.child_by_field_name("body")
        if body is not None:
            _expression(run, body, names, rel, group)
        return
    target = _file_path(callback, rel)
    if target is not None:
        _load(run, target, group)
        return
    run.out.count(REASON_GROUP_CALLBACK_UNREAD)


def _apply(run: _Run, group: _Group, method: str, call, names, rel: str, value_node=False):
    """`group` with one registrar attribute applied, or None when it cannot be read."""
    if value_node:
        value, extra = call, None
    else:
        positional, _ = arguments(call)
        value = positional[0] if positional else None
        extra = positional[1] if len(positional) > 1 else None
    if method == "prefix":
        text = literal(value)
        if text is None:
            run.out.count(REASON_PREFIX_NOT_LITERAL)
            return replace(group, prefix_ok=False)
        return replace(group, prefix=_join_prefix(group.prefix, text))
    if method in ("name", "as"):
        text = literal(value)
        if text is None:
            run.out.count(REASON_NAME_NOT_LITERAL)
            return replace(group, name_ok=False)
        return replace(group, name=group.name + text)
    if method == "middleware":
        found = _middleware_list(run, value, names, rel)
        return replace(group, middleware=group.middleware + found)
    if method == "withoutMiddleware":
        texts = _middleware_texts(value, names)
        if texts is None:
            run.out.count(REASON_WITHOUT_NOT_LITERAL)
            return replace(group, without_ok=False)
        return replace(group, without=group.without + texts)
    if method == "controller":
        # docs-check laravel B.6: an inner group `controller` overrides the outer one.
        fqcn = class_constant_name(value, names) or _qualified(literal(value))
        if fqcn is None:
            run.out.count(REASON_CONTROLLER_NOT_LITERAL)
            return replace(group, controller=None, controller_ok=False)
        return replace(group, controller=fqcn, controller_ok=True)
    if method == "namespace":
        return _merge_namespace(run, group, literal(value))
    if method == "where":
        wheres = _where(run, value, extra, value_node)
        return replace(group, wheres=group.wheres + wheres)
    if method in _INERT_MODIFIERS:
        return group
    run.out.count(REASON_GROUP_ATTRIBUTE_UNKNOWN)
    return None


def _join_prefix(old: str, new: str) -> str:
    """`RouteGroup::formatPrefix`: `trim(old, '/') . '/' . trim(new, '/')`."""
    new = new.strip("/")
    if not old:
        return new
    return f"{old.strip('/')}/{new}".strip("/") if new else old.strip("/")


def _merge_namespace(run: _Run, group: _Group, new: str | None) -> _Group:
    """`RouteGroup::formatNamespace`: joined to the outer one unless it starts with `\\`.

    docs-check laravel B.6: `trim(old, '\\') . '\\' . trim(new, '\\')`, or `trim(new, '\\')`
    when there is no outer namespace or the new one starts with `\\`.
    """
    if new is None:
        run.out.count(REASON_NAMESPACE_NOT_LITERAL)
        return replace(group, namespace="", namespace_reason=REASON_NAMESPACE_NOT_LITERAL)
    inner = new.strip("\\")
    if new.startswith("\\") or (not group.namespace and not group.namespace_reason):
        return replace(group, namespace=inner, namespace_reason="")
    if group.namespace_reason:
        # An unread outer namespace stays unread under a relative inner one.
        return group
    outer = group.namespace.strip("\\")
    return replace(group, namespace=f"{outer}\\{inner}")


def _where(run: _Run, value, extra, value_node: bool) -> tuple[tuple[str, str | None], ...]:
    """`where('id', '[0-9]+')` or `where(['id' => ...])`; None marks an unread pattern."""
    if not value_node and extra is not None:
        name = literal(value)
        if name is None:
            run.out.count(REASON_WHERE_NOT_LITERAL)
            return ()
        return ((name, literal(extra)),)
    items = array_items(value)
    if items is None:
        run.out.count(REASON_WHERE_NOT_LITERAL)
        return ()
    found = []
    for key, pattern in items:
        name = literal(key)
        if name is None:
            run.out.count(REASON_WHERE_NOT_LITERAL)
            continue
        found.append((name, literal(pattern)))
    return tuple(found)


# --- middleware ----------------------------------------------------------------------------


def _middleware_entry(text: str, source_file: str, line: int, subject: str = "") -> _Middleware:
    alias, _, parameters = text.partition(":")
    if alias in AUTHORIZATION_ALIASES:
        ability, _, models = parameters.partition(",")
        return _Middleware(text, ability, GUARD_KIND_AUTHORIZATION, subject or models,
                           source_file, line)  # fmt: skip
    if alias in AUTHENTICATION_ALIASES or fold(alias) in AUTHENTICATION_CLASSES:
        return _Middleware(text, text, GUARD_KIND_AUTHENTICATION, subject, source_file, line)
    return _Middleware(text, text, GUARD_KIND_MIDDLEWARE, subject, source_file, line)


def _middleware_texts(node, names) -> tuple[str, ...] | None:
    """Middleware as written: strings, `Foo::class`, or a list of those. None otherwise."""
    single = _one_middleware(node, names)
    if single is not None:
        return (single,)
    items = array_items(node)
    if items is None or any(key is not None for key, _ in items):
        return None
    found = tuple(_one_middleware(value, names) for _, value in items)
    if any(f is None for f in found):
        return None
    return found  # type: ignore[return-value]


def _one_middleware(node, names) -> str | None:
    return literal(node) or class_constant_name(node, names)


def _middleware_list(run: _Run, node, names, rel: str) -> tuple[_Middleware, ...]:
    """Each readable entry of `->middleware(...)`; an unreadable one is counted and skipped."""
    source = _spelling(run, rel)
    line = nodes.line_span(node)[0] if node is not None else 0
    single = _one_middleware(node, names)
    values = [node] if single is not None else None
    if values is None:
        items = array_items(node)
        if items is None or any(key is not None for key, _ in items):
            run.out.count(REASON_MIDDLEWARE_NOT_LITERAL)
            return ()
        values = [v for _, v in items]
    found = []
    for value in values:
        text = _one_middleware(value, names)
        if text is None:
            run.out.count(REASON_MIDDLEWARE_NOT_LITERAL)
            continue
        entry = _middleware_entry(text, source, nodes.line_span(value)[0] or line)
        if entry.kind == GUARD_KIND_AUTHORIZATION and not entry.label:
            run.out.count(REASON_CAN_NOT_LITERAL)
            continue
        found.append(entry)
    return tuple(found)


def _can(run: _Run, call, names, rel: str) -> _Middleware | None:
    """`->can($ability, $models)`: the `can:ability,models` middleware Laravel adds."""
    positional, named = arguments(call)
    return _ability_entry(
        run, pick((0, "ability"), positional, named), pick((1, "models"), positional, named),
        names, _spelling(run, rel), nodes.line_span(call)[0],
    )  # fmt: skip


def _ability_entry(run: _Run, ability_node, models, names, source: str, line: int):
    """The `can:ability,models` entry of `->can()` or `#[Authorize]`, None when unread.

    docs-check laravel C.10: `#[Authorize($ability, $models, ...)]` extends `#[Middleware]`
    with the same `can:` middleware, so both share the `can` guard node.
    """
    ability = literal(ability_node)
    if not ability:
        run.out.count(REASON_CAN_NOT_LITERAL)
        return None
    text = "can:" + ability
    subject = ""
    if models is not None:
        listed = _middleware_texts(models, names)
        # `'can:'.$ability.','.implode(',', Arr::wrap($models))`, when the models are literal.
        subject = ",".join(listed) if listed is not None else nodes.node_text(models)
        text += "," + subject if listed is not None else ",?"
    return _middleware_entry(text, source, line, subject)


# --- routes --------------------------------------------------------------------------------


def _route(run: _Run, calls, names, rel: str, group: _Group, registrar_prefix: str | None = None,
           registrar_controller: bool = False) -> None:  # fmt: skip
    (verb, call), *modifiers = calls
    positional, named = arguments(call)
    if verb == "match":
        verbs = strings(pick((0, "methods"), positional, named))
        if verbs is None:
            run.out.count(REASON_VERBS_NOT_LITERAL)
            return
        # Each verb once, in written order: `['get', 'GET']` is one method, not two.
        methods = tuple(dict.fromkeys(v.upper() for v in verbs))
        if "GET" in methods and "HEAD" not in methods:
            methods += ("HEAD",)
        uri_node = pick((1, "uri"), positional, named)
        action = pick((2, "action"), positional, named)
    else:
        methods = _verb_methods(run, verb)
        uri_node = pick((0, "uri"), positional, named)
        action = pick((1, "action"), positional, named)
    uri = literal(uri_node)
    if uri is None:
        run.out.count(REASON_PATH_NOT_LITERAL)
        return
    if not group.prefix_ok:
        run.out.count(REASON_PREFIX_NOT_LITERAL)
        return
    handler = _action(run, action, names, group, registrar_controller)
    if handler is None:
        return
    middleware = list(group.middleware)
    without = group.without
    without_ok = group.without_ok
    wheres = dict(group.wheres)
    name: str | None = group.name or None
    name_ok = group.name_ok
    # The registrar prefix is applied in `Route::__construct`, before any later `->prefix()`.
    prefixes: list[str] = [registrar_prefix] if registrar_prefix is not None else []
    for method, modifier in modifiers:
        args, kwargs = arguments(modifier)
        first = args[0] if args else None
        if method in ("name", "as"):
            text = literal(first)
            if text is None:
                run.out.count(REASON_NAME_NOT_LITERAL)
                name_ok = False
            else:
                # docs-check laravel B.7: `Route::name()` appends to the name it already has,
                # the group `as` included.
                name = (name or "") + text
        elif method == "prefix":
            # docs-check laravel B.7: `Route::prefix()` after the verb, applied in written order.
            text = literal(pick((0, "prefix"), args, kwargs))
            if text is None:
                run.out.count(REASON_PREFIX_NOT_LITERAL)
                return
            prefixes.append(text)
        elif method == "middleware":
            middleware.extend(_middleware_list(run, first, names, rel))
        elif method == "can":
            entry = _can(run, modifier, names, rel)
            if entry is not None:
                middleware.append(entry)
        elif method == "withoutMiddleware":
            texts = _middleware_texts(first, names)
            if texts is None:
                run.out.count(REASON_WITHOUT_NOT_LITERAL)
                without_ok = False
            else:
                without += texts
        elif method == "where":
            wheres.update(_where(run, first, args[1] if len(args) > 1 else None, False))
        elif method in _WHERE_KEEP or method == "whereIn":
            _where_kind(run, method, args, wheres)
        elif method in _INERT_MODIFIERS:
            continue
        else:
            run.out.count(REASON_MODIFIER_UNKNOWN)
            return
    path = _route_prefixed(_compose(group.prefix, uri), prefixes)
    fqcn, method_name = handler
    _add_route(
        run, fqcn, method_name, rel, nodes.line_span(call)[0], path, uri,
        name if name_ok else None, methods, middleware, without, without_ok, wheres,
    )  # fmt: skip


def _where_kind(run: _Run, method: str, args: list, wheres: dict) -> None:
    """`whereNumber('id')` and kin keep the placeholder; `whereIn` is a literal alternation."""
    if method == "whereIn":
        name = literal(args[0]) if args else None
        values = strings(args[1]) if len(args) > 1 else None
        if name is None:
            run.out.count(REASON_WHERE_NOT_LITERAL)
            return
        wheres[name] = None if values is None else "|".join(re.escape(v) for v in values)
        return
    names_ = strings(args[0]) if args else None
    if names_ is None:
        run.out.count(REASON_WHERE_NOT_LITERAL)
        return
    for one in names_:
        wheres[one] = "[0-9]+"


def _compose(prefix: str, uri: str) -> str:
    """`Router::prefix`: `trim(trim(prefix, '/') . '/' . trim(uri, '/'), '/') ?: '/'`."""
    joined = f"{prefix.strip('/')}/{uri.strip('/')}".strip("/")
    return "/" + joined if joined else "/"


def _route_prefixed(path: str, prefixes: list[str]) -> str:
    """`Route::prefix` on a built route, once per call in written order.

    docs-check laravel B.7: `rtrim(prefix, '/') . '/' . ltrim(uri, '/')`, then trimmed unless it
    is `/`. The route's URI already holds the group prefix (`Router::createRoute` applies it
    first), so a route-level prefix lands in front of it: `->prefix('v1')->prefix('v2')` on
    `b` gives `v2/v1/b`.
    """
    uri = path.lstrip("/") or "/"
    for prefix in prefixes:
        uri = prefix.rstrip("/") + "/" + uri.lstrip("/")
        uri = uri if uri == "/" else uri.strip("/")
    return "/" + uri.lstrip("/") if uri != "/" else "/"


def _verb_methods(run: _Run, verb: str) -> tuple[str, ...]:
    """The methods a verb call registers; `any` is the installed version's `Router::$verbs`."""
    if verb == "query":
        return ("QUERY",)
    if verb == "any":
        # `any()` is `self::$verbs` (Routing/Router.php:244); QUERY joins it in v13.35.0
        # (Router.php:136, CHANGELOG.md:243). Unknown means the list without QUERY.
        return ALL_VERBS_13 if _since(run, SINCE_QUERY) else ALL_VERBS
    return VERB_METHODS[verb]


def _action(
    run: _Run, action, names, group: _Group, registrar_controller: bool = False
) -> tuple[str, str] | None:
    """The `(controller FQCN, method)` an action names, or None with a counted reason.

    A closure or arrow function gives `_CLOSURE`: the route is published with no handler.
    A string action (`Foo::class` is one too) goes through `Router::convertToControllerAction`:
    the group controller, then the group namespace. An array action does not.
    """
    if action is None:
        run.out.count(REASON_ACTION_UNREADABLE)
        return None
    if _is_callable(action):
        run.out.count(REASON_CLOSURE_ACTION)
        return _CLOSURE
    in_controller_group = group.controller is not None or not group.controller_ok
    invokable = class_constant_name(action, names)
    if invokable is not None:
        if in_controller_group:
            # docs-check laravel B.6: `prependGroupController` keeps a string with no `@` only
            # when `class_exists()` says so.
            run.out.count(REASON_CONTROLLER_GROUP_CLASS)
            return None
        fqcn = _namespaced(run, invokable, group)
        return (fqcn.lstrip("\\"), "__invoke") if fqcn is not None else None
    if is_array(action):
        items = array_items(action)
        if items is None or len(items) != 2 or any(k is not None for k, _ in items):
            run.out.count(REASON_ACTION_UNREADABLE)
            return None
        fqcn = class_constant_name(items[0][1], names) or _qualified(literal(items[0][1]))
        method = literal(items[1][1])
        if fqcn is None or not method:
            run.out.count(REASON_ACTION_UNREADABLE)
            return None
        return fqcn, method
    text = literal(action)
    if text is None:
        run.out.count(REASON_ACTION_UNREADABLE)
        return None
    if "@" in text:
        # docs-check laravel B.6: a string with `@` is never prefixed with the group controller.
        class_part, _, method = text.partition("@")
    elif not in_controller_group and registrar_controller and "\\" not in text:
        # `Route::controller(X)->get('b', 'm')`: only the group stack's controller is read
        # (Routing/Router.php:635, :668-684), so `uses` stays `m`. The path is proved.
        run.out.count(REASON_REGISTRAR_CONTROLLER)
        return _UNRESOLVED
    elif not in_controller_group:
        run.out.count(REASON_ACTION_UNREADABLE)
        return None
    elif group.controller is None:
        run.out.count(REASON_CONTROLLER_NOT_LITERAL)
        return None
    elif "\\" in text or run.ctx.class_files(text):
        # `prependGroupController` keeps a string that `class_exists()` (Router.php:676):
        # one with a backslash may name a class, and a corpus class name does.
        run.out.count(REASON_CONTROLLER_GROUP_CLASS)
        return None
    else:
        # `prependGroupController`: a bare method name inside `Route::controller()->group()`.
        class_part, method = group.controller, text
    namespaced = _namespaced(run, class_part, group)
    if namespaced is None:
        return None
    fqcn = _qualified(namespaced)
    if fqcn is None or not method:
        run.out.count(REASON_STRING_ACTION_SHORT)
        return None
    return fqcn, method


def _namespaced(run: _Run, class_part: str, group: _Group) -> str | None:
    """`Router::prependGroupNamespace`, or None with a counted reason when it is unread.

    docs-check laravel B.5: the group namespace is prepended unless the class starts with `\\`
    or already starts with that namespace. A `use` line never applies to a string.
    """
    if group.namespace_reason:
        run.out.count(group.namespace_reason)
        return None
    namespace = group.namespace
    if not namespace or class_part.startswith("\\") or class_part.startswith(namespace):
        return class_part
    return f"{namespace}\\{class_part}"


def _qualified(text: str | None) -> str | None:
    """A class name in a string, only when it is fully qualified: no import applies to it."""
    if not text:
        return None
    name = text.lstrip("\\")
    if "\\" not in name or not all(part.isidentifier() for part in name.split("\\")):
        return None
    return name


def _add_route(run, fqcn, method_name, rel, line, path, uri, name, methods, middleware,
               without, without_ok, wheres) -> None:  # fmt: skip
    closure = (fqcn, method_name) == _CLOSURE
    unresolved = (fqcn, method_name) == _UNRESOLVED
    if unresolved:
        method_name = ""
    # A closure's or an unresolved handler's "handler file" is the routes file: it keys the
    # route node, see `node_key`.
    files = [rel] if closure or unresolved else run.ctx.class_files(fqcn)
    if len(files) > 1:
        run.out.count(REASON_HANDLER_AMBIGUOUS)
    template, template_reason = route_template(path, wheres)
    if template_reason:
        run.out.count(template_reason)
    declaration = RouteDeclaration(
        controller_class=fqcn,
        controller_method=method_name,
        source_file=_spelling(run, rel),
        line=line,
        path=path,
        name=name,
        methods=tuple(methods),
        method_path=uri,
        provider=NAME,
        via=VIA_ROUTE_FILE,
        handler_file=files[0] if len(files) == 1 else "",
        template=template,
        template_reason=template_reason,
        handler_kind=(
            HANDLER_KIND_CLOSURE
            if closure
            else HANDLER_KIND_UNRESOLVED
            if unresolved
            else HANDLER_KIND_METHOD
        ),
    )
    run.routes.append(
        _Route(declaration, list(middleware), tuple(without), without_ok, declaration.handler_file)
    )


# --- resources -----------------------------------------------------------------------------


def _resource(run: _Run, calls, names, rel: str, group: _Group) -> None:
    (kind, call), *options = calls
    positional, named = arguments(call)
    resource = literal(pick((0, "name"), positional, named))
    controller = class_constant_name(pick((1, "controller"), positional, named), names)
    if resource is None or controller is None:
        run.out.count(REASON_RESOURCE_NAME_NOT_LITERAL)
        return
    if pick((2, "options"), positional, named) is not None:
        run.out.count(REASON_RESOURCE_OPTION_UNKNOWN)
        return
    _resource_routes(run, kind, resource, controller, options, names, rel, group, call)


def _resources(run: _Run, calls, names, rel: str, group: _Group) -> None:
    (kind, call), *rest = calls
    positional, named = arguments(call)
    if rest or len(positional) != 1 or named:
        run.out.count(REASON_RESOURCE_OPTION_UNKNOWN)
        return
    items = array_items(positional[0])
    if items is None:
        run.out.count(REASON_RESOURCE_NAME_NOT_LITERAL)
        return
    single = "apiResource" if kind == "apiResources" else "resource"
    for key, value in items:
        resource = literal(key)
        controller = class_constant_name(value, names)
        if resource is None or controller is None:
            run.out.count(REASON_RESOURCE_NAME_NOT_LITERAL)
            continue
        _resource_routes(run, single, resource, controller, [], names, rel, group, call)


def _resource_routes(run, kind, resource, controller, options, names, rel, group, call) -> None:
    group, resource = _prefixed_resource(group, resource)
    segments = resource.split(".")
    if not all(segments):
        run.out.count(REASON_RESOURCE_NESTED)
        return
    if not group.prefix_ok:
        run.out.count(REASON_PREFIX_NOT_LITERAL)
        return
    actions = [a for a in RESOURCE_ACTIONS if kind == "resource" or a[0] in API_RESOURCE_ACTIONS]
    route_names: dict[str, str] = {}
    parameters: dict[str, str] = {}
    middleware = list(group.middleware)
    without = group.without
    without_ok = group.without_ok
    shallow = False
    for method, option in options:
        args, _ = arguments(option)
        first = args[0] if args else None
        if method not in _RESOURCE_OPTIONS:
            run.out.count(REASON_RESOURCE_OPTION_UNKNOWN)
            return
        if method in ("only", "except"):
            listed = strings(first)
            if listed is None:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
            keep = set(listed)
            actions = [a for a in actions if (a[0] in keep) == (method == "only")]
        elif method == "shallow":
            # docs-check laravel B.8: `shallow(bool $shallow = true)`.
            flag = True if first is None else _bool(first)
            if flag is None:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
            shallow = flag
        elif method in ("names", "parameters"):
            mapping = _string_map(first) if first is not None and is_array(first) else None
            single_name = literal(first) if method == "names" else None
            if mapping is None and single_name is None:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
            if method == "names" and mapping is None:
                # `->names('photo')`: the base of every route name.
                route_names = {a[0]: f"{single_name}.{a[0]}" for a in RESOURCE_ACTIONS}
            else:
                (route_names if method == "names" else parameters).update(mapping or {})
        elif method in ("name", "parameter"):
            key = literal(first)
            value = literal(args[1]) if len(args) > 1 else None
            if key is None or value is None:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
            (route_names if method == "name" else parameters)[key] = value
        elif method == "middleware":
            middleware.extend(_middleware_list(run, first, names, rel))
        else:
            texts = _middleware_texts(first, names)
            if texts is None:
                run.out.count(REASON_WITHOUT_NOT_LITERAL)
                without_ok = False
            else:
                without += texts
    *parents, last = segments
    wildcards = [_wildcard(s, parameters) for s in segments]
    parameter = wildcards[-1]
    if parents and (None in wildcards or parameter in wildcards[:-1]):
        # docs-check laravel B.8: every nested segment needs its wildcard, and
        # `getResourceUri` removes each `/{last}` from the nested URI, so a repeat is refused.
        run.out.count(
            REASON_RESOURCE_PARAMETER_UNPROVED if None in wildcards else REASON_RESOURCE_NESTED
        )
        return
    if parameter is None and any("{}" in suffix for _, _, suffix in actions):
        run.out.count(REASON_RESOURCE_PARAMETER_UNPROVED)
        return
    # docs-check laravel B.8: nested `photos.comments` is `photos/{photo}/comments`.
    nested = "".join(f"{s}/{{{w}}}/" for s, w in zip(parents, wildcards[:-1], strict=True)) + last
    line = nodes.line_span(call)[0]
    for action, verbs, suffix in actions:
        if run.resource_verbs and action in _VERB_SEGMENT_ACTIONS:
            run.out.count(REASON_RESOURCE_VERBS)
            continue
        # docs-check laravel B.8: shallow member routes drop the parents from URI and name.
        flat = shallow and action in _SHALLOW_ACTIONS
        uri = (last if flat else nested) + suffix.replace("{}", "{" + (parameter or "") + "}")
        name = group.name + route_names.get(action, f"{last if flat else resource}.{action}")
        if not group.name_ok:
            name = None
        path = _compose(group.prefix, uri)
        _add_route(run, controller, action, rel, line, path, uri, name, verbs, middleware,
                   without, without_ok, dict(group.wheres))  # fmt: skip  # fmt: skip


def _prefixed_resource(group: _Group, resource: str) -> tuple[_Group, str]:
    """A name with `/` registers its last segment inside a `prefix` group of the rest.

    `ResourceRegistrar::register` / `singleton` (Routing/ResourceRegistrar.php:88-92, :155-159)
    call `prefixedResource` / `prefixedSingleton`, which split at the last `/` (:251-261) and
    open `Router::group(['prefix' => ...])` (:220, :242). Route names keep the last segment.
    """
    if "/" not in resource:
        return group, resource
    head, _, last = resource.rpartition("/")
    return replace(group, prefix=_join_prefix(group.prefix, head)), last


def _wildcard(segment: str, parameters: dict[str, str]) -> str | None:
    """`ResourceRegistrar::getResourceWildcard`: the override or the singular, `-` to `_`.

    docs-check laravel B.8: the `-` to `_` replacement applies to an override too.
    """
    value = parameters.get(segment) or _singular(segment)
    return value.replace("-", "_") if value is not None else None


def _bool(node) -> bool | None:
    text = nodes.node_text(node).strip().lower() if node is not None else ""
    return {"true": True, "false": False}.get(text)


def _singleton(run: _Run, calls, names, rel: str, group: _Group) -> None:
    """`Route::singleton` / `Route::apiSingleton`: one resource instance, no wildcard.

    docs-check laravel B.8: `show, edit, update` by default; `creatable()` adds create, store
    and destroy, `destroyable()` adds destroy; `apiSingleton` keeps store, show, update and
    destroy. `only` replaces the kept list, `except` removes from it.
    """
    (kind, call), *options = calls
    positional, named = arguments(call)
    resource = literal(pick((0, "name"), positional, named))
    controller = class_constant_name(pick((1, "controller"), positional, named), names)
    if resource is None or controller is None:
        run.out.count(REASON_RESOURCE_NAME_NOT_LITERAL)
        return
    if pick((2, "options"), positional, named) is not None:
        run.out.count(REASON_RESOURCE_OPTION_UNKNOWN)
        return
    group, resource = _prefixed_resource(group, resource)
    if "." in resource or not resource:
        # A nested singleton is not read, so `shallow` never arises here: `show` would ignore
        # it (ResourceRegistrar.php:471-480) and edit/update/destroy would not (:492, :511, :530).
        run.out.count(REASON_RESOURCE_NESTED)
        return
    if not group.prefix_ok:
        run.out.count(REASON_PREFIX_NOT_LITERAL)
        return
    only: set[str] | None = set(API_SINGLETON_ACTIONS) if kind == "apiSingleton" else None
    except_: set[str] = set()
    added: tuple[str, ...] = ()
    route_names: dict[str, str] = {}
    middleware = list(group.middleware)
    without = group.without
    without_ok = group.without_ok
    for method, option in options:
        args, _ = arguments(option)
        first = args[0] if args else None
        if method not in _SINGLETON_OPTIONS:
            run.out.count(REASON_RESOURCE_OPTION_UNKNOWN)
            return
        if method == "creatable":
            added = ("create", "store", "destroy")
        elif method == "destroyable":
            if not added:
                added = ("destroy",)
        elif method in ("only", "except"):
            listed = strings(first)
            if listed is None:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
            if method == "only":
                only = set(listed)
            else:
                except_ = set(listed)
        elif method in ("names", "name"):
            mapping = _string_map(first) if method == "names" and is_array(first) else None
            single_name = literal(first) if method == "names" and mapping is None else None
            value = literal(args[1]) if method == "name" and len(args) > 1 else None
            key = literal(first) if method == "name" else None
            if mapping is not None:
                route_names.update(mapping)
            elif single_name is not None:
                route_names = {a[0]: f"{single_name}.{a[0]}" for a in SINGLETON_ACTIONS}
            elif key is not None and value is not None:
                route_names[key] = value
            else:
                run.out.count(REASON_RESOURCE_OPTION_NOT_LITERAL)
                return
        elif method == "middleware":
            middleware.extend(_middleware_list(run, first, names, rel))
        else:
            texts = _middleware_texts(first, names)
            if texts is None:
                run.out.count(REASON_WITHOUT_NOT_LITERAL)
                without_ok = False
            else:
                without += texts
    wanted = [a for a in (*_SINGLETON_DEFAULTS, *added)
              if (only is None or a in only) and a not in except_]  # fmt: skip
    by_name = {a[0]: a for a in SINGLETON_ACTIONS}
    line = nodes.line_span(call)[0]
    for action in wanted:
        if run.resource_verbs and action in _VERB_SEGMENT_ACTIONS:
            run.out.count(REASON_RESOURCE_VERBS)
            continue
        _, verbs, suffix = by_name[action]
        uri = resource + suffix
        name = group.name + route_names.get(action, f"{resource}.{action}")
        _add_route(run, controller, action, rel, line, _compose(group.prefix, uri), uri,
                   name if group.name_ok else None, verbs, middleware, without, without_ok,
                   dict(group.wheres))  # fmt: skip


def _singular(resource: str) -> str | None:
    """The route parameter for a resource name, only for plurals that are the singular + `s`.

    Laravel uses the full English inflector; anything this rule cannot be sure of is None.
    """
    word = resource.lower()
    if (
        not re.fullmatch(r"[a-z][a-z0-9_-]*s", resource)
        or word in _UNCOUNTABLE
        or word.endswith(_UNSAFE_PLURAL_ENDINGS)
    ):
        return None
    return resource[:-1].replace("-", "_")


# --- route template ------------------------------------------------------------------------


def route_template(path: str, wheres: dict[str, str | None]) -> tuple[str, str]:
    """`(route_template, reason)`: Laravel's URI with `{name}` / `{name?}` placeholders.

    `{post:slug}` loses its binding field, as `Route::parseUri` strips it from the URI. A
    literal requirement that may match `/` gives "" and `TEMPLATE_MULTI_SEGMENT`; a requirement
    that is not a literal keeps the template and gives `TEMPLATE_REQUIREMENT_UNREAD`.
    """
    unbound = _BINDING_FIELD.sub(lambda m: "{" + m.group(1) + (m.group(2) or "") + "}", path)
    multi = set()
    unread = False
    for name in _PLACEHOLDER_NAME.findall(unbound):
        if name not in wheres:
            continue
        pattern = wheres[name]
        if pattern is None:
            unread = True
        elif requirement_may_match_slash(pattern):
            multi.add(name)
    template, reason = brace_template(unbound, multi_segment=multi)
    if template and unread:
        return template, TEMPLATE_REQUIREMENT_UNREAD
    return template, reason


# --- controllers and publication -----------------------------------------------------------


def _controller_guards(run: _Run, handlers: dict[tuple[str, str], str]) -> list[AccessGuard]:
    """`HasMiddleware::middleware()` entries, as method guards on the handlers they cover."""
    by_file: dict[str, dict[str, set[str]]] = {}
    for (fqcn, method), path in handlers.items():
        by_file.setdefault(path, {}).setdefault(fold(fqcn), set()).add(method)
    found: list[AccessGuard] = []
    for path, classes in sorted(by_file.items()):
        parsed = run.ctx.parse(path)
        if parsed is None:
            continue
        for declaration in _class_declarations(parsed.root):
            names = parsed.names.at(declaration)
            fqcn = names.qualify_declaration(nodes.name_of(declaration))
            methods = classes.get(fold(fqcn))
            if methods:
                found.extend(_class_guards(run, declaration, fqcn, methods, names, path))
    return found


def _class_declarations(root) -> list:
    out = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == "class_declaration":
            out.append(current)
            continue
        stack.extend(current.children)
    return out


def _class_guards(run: _Run, declaration, fqcn: str, handled: set[str], names, path: str):
    body = nodes.child(declaration, "declaration_list")
    methods = {nodes.name_of(m): m for m in nodes.children(body, "method_declaration")}
    found = _has_middleware_guards(run, declaration, methods, fqcn, handled, names, path)
    found.extend(_attribute_guards(run, declaration, methods, fqcn, handled, names, path))
    return found


def _has_middleware_guards(run: _Run, declaration, methods, fqcn, handled, names, path):
    """`HasMiddleware::middleware()` entries, as guards on the handler methods they cover."""
    constructor = methods.get("__construct")
    if constructor is not None and _calls_this_middleware(constructor):
        run.out.count(REASON_CONTROLLER_CONSTRUCTOR_MIDDLEWARE)
    clause = nodes.child(declaration, "class_interface_clause")
    implemented = {
        fold(resolve_class_name(nodes.node_text(n).strip(), names))
        for n in nodes.children(clause, "name", "qualified_name")
    }
    declared = methods.get("middleware")
    if fold(HAS_MIDDLEWARE) not in implemented:
        if declared is not None and nodes.child(declared, "static_modifier") is not None:
            run.out.count(REASON_CONTROLLER_MIDDLEWARE_UNPROVED)
        return []
    if declared is None:
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_UNPROVED)
        return []
    entries = _returned_array(declared)
    if entries is None:
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL)
        return []
    found: list[AccessGuard] = []
    for value in entries:
        read = _controller_entry(run, value, names)
        if read is None:
            continue
        texts, only, except_ = read
        covered = sorted(
            m for m in handled if (only is None or m in only) and m not in (except_ or ())
        )
        line = nodes.line_span(value)[0]
        for text in texts:
            entry = _middleware_entry(text, path, line)
            if entry.kind == GUARD_KIND_AUTHORIZATION and not entry.label:
                run.out.count(REASON_CAN_NOT_LITERAL)
                continue
            for method in covered:
                found.append(
                    AccessGuard(
                        fqcn, method, path, line, entry.label, True, entry.subject,
                        provider=NAME, via=MIDDLEWARE, guard_attribute=MIDDLEWARE,
                        guard_kind=entry.kind,
                    )
                )  # fmt: skip
    return found


def _attribute_guards(run: _Run, declaration, methods, fqcn, handled, names, path):
    """13.x `#[Middleware]`, `#[Authorize]` and `#[WithoutMiddleware]` on a controller.

    docs-check laravel C.10: class and method targets, repeatable. Matched by strict FQCN,
    case-folded, through the file's imports. `#[WithoutMiddleware]` is kept per handler and
    applied in `_publish` with the route's own `withoutMiddleware`.
    """
    targets = [(declaration, None)] + [(methods[m], m) for m in sorted(handled) if m in methods]
    found_attributes = []
    for node, member in targets:
        for attribute in declared_attributes(node):
            kind = _CONTROLLER_ATTRIBUTES.get(fold(attribute_fqcn(attribute, names)))
            if kind is not None:
                found_attributes.append((attribute, member, kind))
    inherited, readable = _inherited_attributes(run, fqcn, handled, set(methods))
    if not found_attributes and not inherited and readable:
        return []
    supported = _since(run, SINCE_CONTROLLER_ATTRIBUTES)
    if supported is None:
        # An unknown version may remove middleware: nothing that handler carries is proved.
        if any(k == ATTRIBUTE_WITHOUT_MIDDLEWARE for _, _, k in found_attributes):
            run.without_unread.update((fqcn, m) for m in handled)
        return []
    if not supported:
        if found_attributes:
            run.out.count(REASON_CONTROLLER_ATTRIBUTE_VERSION, len(found_attributes))
        return []
    _inherited_effect(run, fqcn, handled, inherited, readable, set(methods))
    without_since = (
        _since(run, SINCE_WITHOUT_MIDDLEWARE_ATTRIBUTE)
        if any(k == ATTRIBUTE_WITHOUT_MIDDLEWARE for _, _, k in found_attributes)
        else None
    )
    found: list[AccessGuard] = []
    for attribute, member, kind in found_attributes:
        if kind == ATTRIBUTE_WITHOUT_MIDDLEWARE and without_since is False:
            # Read by `Route::excludedControllerMiddleware` from v13.20.0 (CHANGELOG.md:789).
            run.out.count(REASON_WITHOUT_ATTRIBUTE_VERSION)
            continue
        positional, named = arguments(attribute)
        offset = 1 if kind == ATTRIBUTE_AUTHORIZE else 0
        limits = []
        for spec in ((1 + offset, "only"), (2 + offset, "except")):
            node = pick(spec, positional, named)
            listed = strings(node) if is_array(node) else None
            if node is not None and listed is None:
                listed = False
            limits.append(listed)
        if False in limits:
            # docs-check laravel C.10: `only` / `except` are literal arrays of method names.
            run.out.count(REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL)
            if kind == ATTRIBUTE_WITHOUT_MIDDLEWARE:
                run.without_unread.update((fqcn, m) for m in handled)
            continue
        only, except_ = limits
        covered = sorted(
            m for m in handled
            if (member is None or m == member) and (only is None or m in only)
            and m not in (except_ or ())
        )  # fmt: skip
        line = nodes.line_span(attribute)[0]
        if kind == ATTRIBUTE_WITHOUT_MIDDLEWARE:
            texts = _middleware_texts(pick((0, "middleware"), positional, named), names)
            if texts is None or without_since is None:
                if texts is None:
                    run.out.count(REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL)
                run.without_unread.update((fqcn, m) for m in covered)
                continue
            for m in covered:
                run.without[(fqcn, m)] = run.without.get((fqcn, m), ()) + texts
            continue
        if kind == ATTRIBUTE_AUTHORIZE:
            entry = _ability_entry(
                run, pick((0, "ability"), positional, named),
                pick((1, "models"), positional, named), names, path, line,
            )  # fmt: skip
            entries = [entry] if entry is not None else []
        else:
            entries = _attribute_middleware(run, pick((0, "middleware"), positional, named),
                                            names, path, line)  # fmt: skip
        short = kind.rsplit("\\", 1)[-1]
        for entry in entries:
            for m in covered:
                found.append(
                    AccessGuard(
                        fqcn, m, path, line, entry.label, True, entry.subject,
                        provider=NAME, via=VIA_ATTRIBUTE, guard_attribute=short,
                        guard_kind=entry.kind,
                    )
                )  # fmt: skip
    return found


def _inherited_attributes(run: _Run, fqcn: str, handled, declared: set[str]):
    """`([(kind, member or None)], readable)`: controller attributes the parents declare.

    `Route::attributeProvidedControllerMiddleware` and `...Exclusions` walk
    `getParentClass()` for class attributes (Routing/Route.php:1201, :1266), and a
    handler method declared in a parent carries that declaration's attributes.
    `readable` is False when a parent is outside the corpus or ambiguous; the walk stops at
    `BASE_CONTROLLER`, which declares none.
    """
    parents = run.ctx.class_parents()
    found: list[tuple[str, str | None]] = []
    declared = set(declared)
    seen = {fold(fqcn)}
    current = fqcn
    while True:
        written = parents.get(fold(current))
        if not written or len(written) != 1:
            return found, False
        parent = written[0]
        if parent is None or fold(parent) == fold(BASE_CONTROLLER):
            return found, True
        key = fold(parent)
        files = run.ctx.class_files(parent)
        facts = run.ctx.facts(files[0]) if len(files) == 1 and key not in seen else None
        scope = next((c for c in facts.classes if fold(c.fqn) == key), None) if facts else None
        if facts is None or scope is None:
            return found, False
        seen.add(key)
        for fact in facts.attributes:
            kind = _CONTROLLER_ATTRIBUTES.get(fold(fact.fqcn))
            if kind is None or fold(fact.class_fqn) != key:
                continue
            if fact.target == TARGET_CLASS:
                found.append((kind, None))
            elif fact.target == TARGET_METHOD and fact.member in handled - declared:
                found.append((kind, fact.member))
        declared |= {m.name for m in scope.methods}
        current = parent


def _inherited_effect(run: _Run, fqcn: str, handled, inherited, readable: bool, declared):
    """Counts what the parents carry; a removal they may make leaves the handler unproved.

    Parent class attributes are read from v13.5.0 (CHANGELOG.md:1347); a `#[WithoutMiddleware]`
    among them removes route and group middleware too (Route.php:1305-1308, Router.php:847), so
    from v13.20.0 those handlers keep no route guard. Inherited guards are counted, not published.
    """
    class_level = [k for k, m in inherited if m is None]
    inherit = _since(run, SINCE_ATTRIBUTE_INHERITANCE) if class_level or not readable else False
    applied = [(k, m) for k, m in inherited if m is not None or inherit is not False]
    if applied:
        run.out.count(REASON_CONTROLLER_ATTRIBUTE_INHERITED, len(applied))
    affected: set[str] = set()
    undeclared = {m for m in handled if m not in declared}
    if not readable and (inherit is not False or undeclared):
        run.out.count(REASON_CONTROLLER_PARENT_UNREAD)
        affected |= set(handled) if inherit is not False else undeclared
    for kind, member in applied:
        if kind == ATTRIBUTE_WITHOUT_MIDDLEWARE:
            affected |= set(handled) if member is None else {member}
    if affected and _since(run, SINCE_WITHOUT_MIDDLEWARE_ATTRIBUTE) is not False:
        run.without_unread.update((fqcn, m) for m in affected)


def _attribute_middleware(run: _Run, node, names, path: str, line: int) -> list[_Middleware]:
    """`#[Middleware(...)]`'s middleware, classified by alias as route middleware is."""
    if _is_callable(node):
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_CLOSURE)
        return []
    texts = _middleware_texts(node, names)
    if texts is None:
        run.out.count(REASON_CONTROLLER_ATTRIBUTE_NOT_LITERAL)
        return []
    found = []
    for text in texts:
        entry = _middleware_entry(text, path, line)
        if entry.kind == GUARD_KIND_AUTHORIZATION and not entry.label:
            run.out.count(REASON_CAN_NOT_LITERAL)
            continue
        found.append(entry)
    return found


def _calls_this_middleware(method) -> bool:
    stack = [method]
    while stack:
        current = stack.pop()
        if current.type == "member_call_expression":
            receiver = current.child_by_field_name("object")
            if (
                nodes.node_text(receiver) == "$this"
                and nodes.node_text(current.child_by_field_name("name")) == "middleware"
            ):
                return True
        stack.extend(current.children)
    return False


def _returned_array(method) -> list | None:
    """The entries of `return [...]` when that is the method's only statement."""
    body = nodes.child(method, "compound_statement")
    statements = [c for c in body.children if c.is_named] if body is not None else []
    if len(statements) != 1 or statements[0].type != "return_statement":
        return None
    value = next((c for c in statements[0].children if c.is_named), None)
    items = array_items(value)
    if items is None or any(k is not None for k, _ in items):
        return None
    return [v for _, v in items]


def _controller_entry(run: _Run, value, names):
    """`(middleware texts, only, except)` of one entry, or None with a counted reason."""
    if _is_callable(value):
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_CLOSURE)
        return None
    single = _one_middleware(value, names)
    if single is not None:
        return (single,), None, None
    if value.type != "object_creation_expression":
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL)
        return None
    written = nodes.node_text(nodes.child(value, "name", "qualified_name")).strip()
    if not written or fold(resolve_class_name(written, names.at(value))) != fold(
        CONTROLLER_MIDDLEWARE
    ):
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL)
        return None
    positional, named = arguments(value)
    middleware = pick((0, "middleware"), positional, named)
    if _is_callable(middleware):
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_CLOSURE)
        return None
    texts = _middleware_texts(middleware, names)
    only_node = pick((1, "only"), positional, named)
    except_node = pick((2, "except"), positional, named)
    only = strings(only_node) if only_node is not None else None
    except_ = strings(except_node) if except_node is not None else None
    if (
        texts is None
        or (only_node is not None and only is None)
        or (except_node is not None and except_ is None)
    ):
        run.out.count(REASON_CONTROLLER_MIDDLEWARE_NOT_LITERAL)
        return None
    return texts, only, except_


def _publish(run: _Run) -> None:
    """Routes, their middleware guards, controller middleware and handler grant calls."""
    out = run.out
    handlers: dict[tuple[str, str], str] = {}
    for route in run.routes:
        if route.handler_file and not route.declaration.has_no_handler:
            handlers[(route.declaration.controller_class, route.declaration.controller_method)] = (
                route.handler_file
            )
    controller = _controller_guards(run, handlers)
    by_handler: dict[tuple[str, str], list[AccessGuard]] = {}
    for guard in controller:
        by_handler.setdefault((guard.holder_class, guard.holder_method or ""), []).append(guard)

    dropped: set[tuple[str, str]] = set()
    per_key: dict[str, list[frozenset]] = {}
    pending: list[tuple[RouteDeclaration, list[RouteGuard]]] = []
    for route in run.routes:
        declaration = route.declaration
        handler = (declaration.controller_class, declaration.controller_method)
        visible = {m.text for m in route.middleware} | {
            _guard_text(g) for g in by_handler.get(handler, ())
        }
        # docs-check laravel C.10: `#[WithoutMiddleware]` on the handler removes like the route's.
        without = route.without + run.without.get(handler, ())
        without_ok = route.without_ok and handler not in run.without_unread
        unmatched = not without_ok or any(w not in visible for w in without)
        if unmatched and without:
            out.count(REASON_WITHOUT_UNMATCHED)
        if not without_ok or unmatched:
            middleware: list[_Middleware] = []
            if by_handler.get(handler):
                dropped.add(handler)
        else:
            middleware = [m for m in route.middleware if m.text not in without]
            if any(_guard_text(g) in without for g in by_handler.get(handler, ())):
                dropped.add(handler)
        guards = list({_route_guard(declaration, m).node_key: _route_guard(declaration, m)
                       for m in middleware}.values())  # fmt: skip
        per_key.setdefault(declaration.node_key, []).append(frozenset(g.node_key for g in guards))
        pending.append((declaration, guards))

    # One route node per `node_key`, as the pass publishes them. When declarations sharing a
    # node disagree on middleware, only the guards every one of them carries are proved.
    first: dict[str, list[RouteGuard]] = {}
    for declaration, guards in pending:
        out.routes.append(declaration)
        first.setdefault(declaration.node_key, guards)
    for key, guards in first.items():
        sets = per_key[key]
        if len(set(sets)) > 1:
            out.count(REASON_ROUTE_KEY_GUARDS_DIFFER)
        common = frozenset.intersection(*sets)
        out.route_guards.extend(g for g in guards if g.node_key in common)

    for handler, guards in by_handler.items():
        if handler in dropped:
            out.count(REASON_CONTROLLER_MIDDLEWARE_EXCLUDED, len(guards))
            continue
        out.guards.extend(guards)

    pairs: dict[str, set] = {}
    for (fqcn, method), path in handlers.items():
        pairs.setdefault(path, set()).add((fqcn, method))
    for path, wanted in sorted(pairs.items()):
        parsed = run.ctx.parse(path)
        if parsed is None:
            continue
        for check in collect_access_checks(
            parsed.root, parsed.names, path, wanted, GRANT_CALLS, NAME
        ):
            out.checks.append(check)
            if check.reason:
                out.count(check.reason)


def _guard_text(guard: AccessGuard) -> str:
    if guard.guard_kind == GUARD_KIND_AUTHORIZATION:
        return "can:" + guard.label + ("," + guard.subject if guard.subject else "")
    return guard.label


def _route_guard(declaration: RouteDeclaration, middleware: _Middleware) -> RouteGuard:
    return RouteGuard(
        route_key=declaration.node_key,
        # `AccessGuard.node_key`, so a route guard and a grant call on one ability share a node.
        node_key=AccessGuard(
            "", None, "", 0, middleware.label, True, provider=NAME, guard_kind=middleware.kind
        ).node_key,
        label=middleware.label,
        guard_attribute=MIDDLEWARE,
        via=MIDDLEWARE,
        source_file=middleware.source_file,
        line=middleware.line,
        subject=middleware.subject,
        provider=NAME,
        guard_kind=middleware.kind,
    )
