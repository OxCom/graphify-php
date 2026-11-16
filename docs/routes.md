# Routes

A second registered pass publishes what `#[Route]` attributes declare. Each declaration becomes
one node — labelled with the path template, carrying the route name, the HTTP methods and
whether the path was composed whole — and one `handled_by` edge to the controller method that
serves it. `route_path` holds the full composed path, `""` when it could not be composed.

Every `#[Route]` on a method is a route of its own, stacked in separate groups or listed in one.
Only the first class-level `#[Route]` is the prefix. An invokable class with no method routes
turns each class-level `#[Route]` into a route for `__invoke`. A path may be a string literal,
`self::NAME` for a string constant declared in the same class body, or a `.` concatenation of
those; `static::NAME`, inherited constants and any `self::NAME` in a trait (where `self` is the
using class) are refused as `path-not-literal`.

The node key is `Class::method#path`, so two controller methods declaring the same path stay two
nodes. An older measurement of the reference application found more route declarations than
distinct path templates, the repeats being different HTTP verbs on one path.

The relation is `handled_by`, not `routed_to` — the latter already means messenger transport
routing, and merging the two would put HTTP paths and message transports in one query.

A declaration whose controller method is missing from the graph still gets its node; only the
edge is withheld, and the omission is counted under `controller_not_in_graph`. The path is a
fact about the codebase either way.

Route nodes follow `route_schema: 2` and carry these fields after the ones above. The reason a
template is `""` or only partly trusted is also on the node, in `route_template_reason` (`""`
when there is none):

- `route_template`: the path with `{name}` placeholders and a leading `/`, `{name?}` for an
  optional one. Symfony inline requirements and defaults are dropped: `{id<\d+>}` is `{id}`,
  `{page<\d+>?1}` and `{page?1}` are `{page?}`. A `defaults:` key also makes a trailing
  placeholder optional. It is `""` when the path could not be composed, or with a counted
  reason: `route-template-multi-segment` when an inline or `requirements:` regex may match `/`
  (anything not provably slash-free counts), `route-template-optional-literal` for an optional
  literal part such as `[/edit]` (other providers), `route-template-unparsed` for a brace that is
  not a placeholder. A `requirements:` value reads like a route path: a string literal,
  `self::NAME` for a string constant declared once in the same class body, or a `.` of those.
  The whole `requirements:` array may be `self::NAME` for a literal array constant declared once
  in the same class body, and that array or an inline one may spread (`...self::OTHER`) another
  such constant; later keys win, and a cycle or any unreadable element leaves the whole array
  unread. Any other value (`static::`, another class, an inherited or trait constant) keeps the template
  and is counted as `route-template-requirement-unread`. The status record shows these under the
  route source's prefix, e.g. `tree_sitter_routes:route-template-multi-segment`.
- `declared_in`: the declaring file, equal to `source_file`.
- `effective_guards`: the sorted ids of the guard nodes proved to apply: route-scoped guards
  (`access_control`), guards on the handler method and guards on its class. `checks_access`
  targets are not included. `[]` when none.
- `authorization_state`: `covered` when an effective guard has `guard_kind: "authorization"`;
  otherwise `undecided` when the `access_control` step could not decide the route or an
  effective guard is `unknown`; otherwise `not_proven`.
- `authorization_reason`: `""` unless undecided, then `access-control-undecided`,
  `access-control-blocked` (unread `security.yaml`, a config blocker, or a route path or prefix
  that is not a literal) or `guard-kind-unknown`.
- `handler_kind`: `method` when the node has a `handled_by` edge, `closure` when the action is a
  closure or arrow function (no handler node exists, so no edge), `unresolved` when the named
  handler is not in the graph (counted under `controller_not_in_graph`, no edge). `method` holds
  exactly when a `handled_by` edge leaves the node. A node shared by several declarations is
  `method` when any of them produced the edge.

## Laravel provider

The Laravel provider (`frameworks/laravel.py`) reads routes files loaded from
`bootstrap/app.php` `withRouting(web:, api:, apiPrefix:)`. Behaviour follows the framework source
as recorded in a docs check; each rule cites its item in a `docs-check laravel` comment.

Covered:

- Verbs, `match`, and `any` as the installed `Router::$verbs`. The version comes from
  `composer.lock` `packages` at minor precision. QUERY and `Route::query()` start at v13.35.0.
  With no version or no minor read (`13.x-dev`), `any` leaves QUERY out and
  `laravel-version-unread` is counted once.
- Registrar calls straight before a verb (`Route::prefix('p')->get('b', ...)`) are the route
  action, not a group. Inside a group with prefix `g` the path is `p/g/b`. Group middleware comes
  before registrar middleware, and the registrar `as` is appended. A repeated registrar call
  replaces the earlier one; only `withoutMiddleware` accumulates.
- `Route::controller(X)->get('b', 'm')` does not bind `X`: the route is published with
  `handler_kind: "unresolved"` under `laravel-registrar-controller-rejected-by-framework`,
  unless an enclosing group sets a controller. Laravel itself rejects such a route at
  registration (`RouteAction::makeInvokable` throws "Invalid route action"), so it is not an
  unguarded endpoint.
- Groups: prefix, `as`, middleware, `withoutMiddleware`, `where`, `controller` (an inner one
  overrides), `namespace` (joined unless it starts with `\`). Array groups take the aliased keys
  `as`, `scope_bindings` and `excluded_middleware`.
- Route-level `->prefix()` in front of the built URI, applied in written order. Route-level
  `->name()` appends to the name the route already has.
- String actions get the group namespace unless they start with `\` or with that namespace.
  A `use` line never applies to a string. In a `controller` group, `Foo@bar` keeps its class and
  a bare method name takes the group controller. A bare string that contains `\` or names a
  corpus class is refused, because `class_exists()` decides it at runtime. Array actions get
  neither the controller nor the namespace.
- Name prefixes (`as`) concatenate with no separator: `name('admin')` and `name('index')` give
  `adminindex`.
- `resource`, `apiResource`, `resources`, `apiResources`, nested `photos.comments`, `->shallow()`,
  `singleton` and `apiSingleton` with `creatable()` and `destroyable()`. A name with `/`
  (`admin/photos`) registers its last segment inside a prefix group of the rest.
- Controller middleware from `HasMiddleware::middleware()` and the `#[Middleware]` and
  `#[Authorize]` attributes (from v13.0.0) and `#[WithoutMiddleware]` (from v13.20.0) on a class
  or a method. `#[Authorize('x')]` shares the guard node of `->can('x')` and `can:x`.
  `#[WithoutMiddleware]` removes route and group middleware as well as controller middleware.
  From v13.5.0 a parent class's attributes apply too: they are counted under
  `laravel-controller-attribute-inherited`, not published. From v13.20.0 an inherited
  `#[WithoutMiddleware]`, or a parent outside the corpus (`laravel-controller-parent-unread`),
  leaves that handler with no proved route guard. `Illuminate\Routing\Controller` ends the walk.
- `withRouting(using:, then:)`: a closure `then` makes Laravel ignore `using` and load the
  `web` / `api` files.
- Guard kinds: `can` is `authorization`; `auth`, `auth:*`, `auth.basic`, `auth.session`, `guest`,
  `verified` and `password.confirm` are `authentication`; any other alias is `middleware`.
- Grant calls: `$this->authorize` and `Gate::authorize` (throwing), `Gate::allows`, `denies`,
  `check`, `any` and `none` (conditional).

Counted under a `laravel-*` reason, never published: `redirect`, `permanentRedirect` and `view`
routes (`laravel-route-no-controller`), the `health:` route (`laravel-health-route`), a
non-literal namespace, a class string in a `controller` group, and a bare `Route::` when
`config/app.php` lists literal `aliases` without the facade (`laravel-route-alias-removed`).
Resource `create` and `edit` routes are refused when any file calls `resourceVerbs(...)`
(`laravel-resource-verbs-changed`). A registrar `prefix`, `name`, `where`, `namespace` or
`controller` before a resource is refused (`laravel-resource-registrar-attribute`):
`ResourceRegistrar` gets them as options and does not apply them as a group would.

Not covered: Laravel 10 `RouteServiceProvider`, Lumen, `Route::fallback`, invokable string
actions (`'App\Ctl'`), nested singletons, and `$this->middleware()` in a controller constructor.

## Laravel closure routes

A Laravel route whose action is a closure or arrow function (`Route::get('/', fn () => ...)`) is
published like an array action: path, methods, name, template and route-scope guards
(`middleware`, `can`). It gets `handler_kind: "closure"` and no `handled_by` edge, and it is
still counted under `laravel-closure-action`. Its node key is
`laravel:closure:<routes file>:<line>#<path>`, with the routes file repo-relative. A closure has
no method or class, so no method-scope or class-scope guard and no `checks_access` edge applies;
`authorization_state` comes from the route-scope guards alone.

## CodeIgniter 4 provider

The CodeIgniter provider (`frameworks/codeigniter.py`) is selected by a direct
`codeigniter4/framework` or `codeigniter4/codeigniter4` requirement. It reads the route files of
`app/Config/Routing.php` `$routeFiles` (a literal list of `APPPATH . 'Config/...'`), else
`app/Config/Routes.php`, in order. Each rule cites its item in a `docs-check ci4` comment. The
`codeigniter4/framework` version comes from `composer.lock`; where it matters and is unread,
`codeigniter-version-unread` is counted once.

Covered:

- `get`, `post`, `put`, `patch`, `delete`, `options`, `head`; `match()` (verbs lowercased);
  `add()` and `map()` as all nine `Router::HTTP_METHODS`.
- String handlers `Ctl::method/$1`, prefixed with the `namespace` option or the default namespace
  (`App\Controllers`, `Routing::$defaultNamespace`, or a literal `setDefaultNamespace()`) unless
  they start with `\`: `Admin\Users::index` is prefixed. Array callables `[Ctl::class, 'm']` are
  fully qualified through the imports. Closures are published with `handler_kind: "closure"`.
- `route_path` is the path as written, with a leading `/`. `(:num)`, `(:alpha)`, `(:alphanum)`,
  `(:segment)`, `(:hash)`, a literal `addPlaceholder()` declared before use, and a raw group
  that cannot match `/` become `{1}`, `{2}`... by position. `(:any)`, `.*`, or a `.` in the path
  give `route_template: ""` with `route-template-multi-segment`.
- `group()` prefixes. Group options merge into inner groups from 4.5.0 (inner replaces before);
  group filters merge into route filters from 4.5.4. Before 4.5.4, or with no version read, no
  group filter is claimed (`codeigniter-group-filter-version`).
- `resource()` and `presenter()` tables with `only`, `except` (list or CSV), `websafe`,
  `controller` and `placeholder`. The id placeholder follows the source (`any`, `.*`), not the
  guide's `(:segment)`, so member templates are `""` unless a placeholder is given.
- Route `filter` options as route-scope guards (`_via: "filter"`, the label keeps arguments).
  `app/Config/Filters.php` before-filters as route-scope guards keyed
  `codeigniter:config#app/Config/Filters.php:<index>` (`_via: "filter_config"`): `$required`
  (4.5.0+), `$globals` minus `except`, `$methods` for routes of only that verb, and `$filters`
  when a pattern provably matches every URL of the route. A partial or undecidable match is
  counted, and makes the route `undecided` when the filter is authorization or unknown.
- Guard kinds: Shield (only with a direct `codeigniter4/shield` requirement) `session`,
  `tokens`, `hmac`, `jwt`, `chain` are authentication, `group` and `permission` authorization;
  the built-ins, `auth-rates` and `force-reset` are middleware. An alias the application maps to
  another class, and every other alias or class, is `unknown`.
- `#[Filter(by, having)]` on controller classes and methods from 4.7.0, where
  `Routing::$useControllerAttributes` defaults to true: the label is the route filter spec
  `by` or `by:having,csv`, on the route filter's guard node. A literal `false` is counted per
  attribute (`codeigniter-controller-attributes-disabled`), as is any attribute before 4.7.0
  (`codeigniter-controller-attributes-version`). A non-literal setting, no readable
  `Routing.php`, or no version read is counted once and leaves the routes of `#[Filter]`
  holders `undecided`. A string `having` is a TypeError at runtime
  (`codeigniter-controller-attribute-invalid`). `#[Restrict]` is a route condition
  (`codeigniter-controller-attribute-restrict`), not a guard; `#[Cache]` is ignored.
- Per key, a verb route beats an `add()`/`map()` route (`RouteCollection.php:504`), which keeps
  the remaining verbs. A verb route with no `filter` option key runs the `*` route's filters
  (`loadRoutesOptions()`, `:1568-1575`); when either key's presence or the `*` route is
  runtime-dependent the route is `undecided` (`codeigniter-route-any-options-undecided`).
- `$methods`: the exact uppercase key, or from 4.5.0 the deprecated lowercase key when the
  uppercase one is missing. An empty `$filters` path (`''` or `[]`) matches every route; an
  empty `except` matches none. A filter spec with more than one `:` is unread
  (`codeigniter-filter-spec-unread`) and makes a route it may apply to `undecided`.
  `codeigniter-methods-filter-case-bypass` means a route's only authorization guard is a
  `$methods` filter, which a lowercase request method skips, so the route is `undecided`
  (`access-control-undecided`) while the guard edge stays published.

Counted under a `codeigniter-*` reason, never published: `cli()`, `view()`, `addRedirect()`,
routes inside `environment()`, a `$routes` passed to any call (`service('auth')->routes($routes)`),
module route discovery, auto routing, a duplicate path and verb after the first, an
unconditional route that shares its key and table with a conditional one, and `match()` with
TRACE or CONNECT (`codeigniter-match-verb-no-method`: no such `RouteCollection` method).
Hostname routes are published and counted (`codeigniter-hostname-route`); a `subdomain` is
ignored when `hostname` is set, else counted as `codeigniter-subdomain-route`.
A route that loses its GET to a `view()` on its key is refused as
`codeigniter-route-key-view-collision`; its other verbs still publish. Every route on the key of
an `addRedirect()` is refused as `codeigniter-route-key-redirect-collision`, unless an
unconditional `add()`/`map()` route on that key came first and voids the redirect.

Not covered: auto-routed controllers, module `Config/Routes.php` files, Shield's own routes,
`$user->can()` grant calls (a typed receiver; follow-up), filter regex patterns beyond `*`,
Registrar-added globals, and `{locale}` rewriting.

## Access guards

Guards are `access_guard` nodes. Three relations reach them:

- `guarded_by` with `_via: "attribute"`: from a method or class to each `#[IsGranted]` on it.
  A literal role is one shared node (`guard#ROLE_X`); any other first argument is kept as source
  text and scoped to its class. The edge carries `guard_scope` and `guard_attribute: "IsGranted"`.
- `checks_access` with `_via: "call"`: from a route handler to the guard named by a direct
  `$this->isGranted(...)` (`conditional: true`) or `$this->denyAccessUnlessGranted(...)`
  (`conditional: false`) in its body. These share the `#[IsGranted]` node. A call whose name
  matches `^can[A-Z]` or `^assert\w*Access` goes to a class-scoped node labelled with the method
  name (`conditional: null`). `GRAPHIFY_PHP_ACCESS_CHECK_PATTERNS` replaces the patterns with a
  comma-separated list; an invalid list falls back to the defaults and is counted. Every
  `checks_access` edge carries `check_kind`: `framework` for `isGranted` and
  `denyAccessUnlessGranted`, `pattern` for a name-matched project method.
- `guarded_by` with `_via: "access_control"`: from a route node to the
  `config/packages/security.yaml` rule Symfony applies to it. The edge is added only when that
  rule is proved to be the first match for every URL the route template can produce.
  Otherwise the route is counted under `access-control-undecided`. The proof compares the
  literal text before the first placeholder, then goes segment by segment: a placeholder is one
  segment with the value set of its literal requirement (`\d+`, a bounded `\d{n}` or `\d{n,m}`
  with n >= 1, a word alternation, or Symfony's default `[^/]+`), checked against rule segments that are literal, `\d+` or `[^/]+`, an optional
  trailing group of literal alternatives, and an optional `$`. A rule without `$` is a prefix
  match. A same-class `self::NAME` requirement counts as literal, as in `route_template`. Any
  other non-literal requirement, a defaulted or optional placeholder at the deciding segment,
  `{_locale}`/`{_format}`, or any other rule shape leaves the route undecided. A routes import
  that sets `requirements`, `defaults`, `locale` or `format` turns the segment step off
  (`access-control-segment-facts-unread`).

Every guard node and every `guarded_by` and `checks_access` edge carries `guard_kind`, and every
guard node carries `guard_key`, the key its id is hashed from. Symfony keys are unchanged
(`guard#ROLE_X`, `Class::guard#<text>`, `Class::check#<method>`, `access_control#<index>`).
Other providers key a guard `guard#<provider>:<guard_kind>:<label>` (or
`Class::guard#<provider>:<guard_kind>:<text>`) and a config rule
`<provider>:config#<file>:<index>`. Symfony's `guard_kind`:

- an `access_control` rule without `allow_if`: `public` for no roles or a `PUBLIC_ACCESS` /
  `IS_AUTHENTICATED_ANONYMOUSLY` role, `authentication` for an `IS_AUTHENTICATED_*`,
  `IS_REMEMBERED` or `IS_IMPERSONATOR` role, else `authorization`. A request passes on any one
  role, so the weakest role decides;
- `allow_if`: split at top-level `and` / `&&`; `authorization` when one of those conjuncts is
  exactly a positive `is_granted('X')` with X a role, else `unknown`. A top-level `or` or `||`
  gives `unknown`; one inside parentheses does not. With `roles` too, the weaker of the two kinds
  wins;
- `#[IsGranted]` and `isGranted` / `denyAccessUnlessGranted`: a literal goes through the role
  rule, anything else is `unknown`;
- a pattern-matched helper call is `unknown`.

The `php_routes` status record carries `route_schema: 2`, `authorization_state` (route nodes per
state) and `authorization_reason` (undecided routes per reason). It counts `handlers_total` and
`handlers_unguarded` (handlers with no `authorization` guard edge from the method, its class or
its routes, and no `check_kind: framework` edge). A
pattern-only check does not cover a handler. Handlers with no `guarded_by` edge fall into exactly
one of `handlers_check_framework`, `handlers_check_pattern_only` and `handlers_unchecked`, and
`handlers_unguarded` equals the last two summed. The record also counts
`checks_access_declarations`, `checks_access_edges_added`, `access_control_edges_added` and
`routes_access_control_undecided`.

Route schema 2 changes what the `handlers_*` counters mean, so their numbers move without any
change in the application. Before, any guard counted; now only an `authorization` guard does,
and a sign-in-only or public `access_control` rule no longer covers a handler. On the reference
Symfony application (220 handlers) `handlers_unguarded` went from 40 to 130,
`handlers_check_framework` from 2 to 17, `handlers_check_pattern_only` from 13 to 20 and
`handlers_unchecked` from 27 to 110. That is a stricter count, not a regression.

Limits:

- Inherited route methods are not followed, although Symfony reads routes from parent methods.
- A `use ... as` alias for `Route` or `IsGranted` is not followed.
- Two stacked routes with the same path on one method collapse to one node.
- A shared literal guard node keeps the subject and location of the first declaration seen.
- `access_control` is read from top-level `security.yaml` only, and only a proved first match
  gets an edge. When a `when@<env>` block, another file under `config/packages/`, or a PHP/XML
  file there may declare `access_control`, every such edge is refused as
  `access-control-overridden-elsewhere`.
- `route_path` is the attribute-declared path. A routes-config import prefix is not applied,
  and when one exists access_control edges are refused as `access-control-route-prefix-unread`.
  Routing PHP and XML files and `src/Kernel.php` get a text check for `prefix` calls only.
- `checks_access` reads only direct `$this` calls in handler bodies. Closures, arrow functions,
  anonymous classes and helper methods are not followed.
