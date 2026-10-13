# Architecture

graphify has cross-file member-call resolvers for Swift, Python, Ruby, TypeScript, C#, Java and
Rust, and none for PHP. Its PHP extractor marks `$obj->method()` as a member call. The shared
cross-file pass then drops it, because a bare `method` collides across the corpus. graphify-php
fills that gap through graphify's own resolver registry, without forking graphify.

The core rule: **a wrong call edge is worse than a missing one.** A source emits a target only
when it can prove it. Every other call is counted under a fixed reason and never guessed.

## Ports and adapters

`ports.py` defines the protocols. Each layer depends only on them.

| Module | Role |
|---|---|
| `__init__.py` | `register()` and `default_sources()`, the only place concrete sources are named |
| `resolver.py` | Collects unresolved calls and asks sources in order. Imports nothing from `syntax/` or `sources/` |
| `graph_adapter.py` | The only module that knows graphify's node and edge schema |
| `sources/phpstan_source.py` | Runs the project's own PHPStan with the collector in `php/` |
| `sources/tree_sitter_source.py` + `syntax/` | Parser-only source |
| `status.py` + `cli.py` | Status record and the publish gate |

## Three resolvers

`register()` adds three `LanguageResolver`s to `graphify.resolver_registry`:

- **`php_member_calls`** binds member and scoped calls to method nodes.
- **`php_routes`** publishes `#[Route]` declarations as nodes.
- **`php_symfony_config`** reads Symfony YAML under `config/`. Without pyyaml it records a skip
  with reason `pyyaml-not-installed`.

Each one writes its own status record. The build gate requires all three by default.

## The member-call pass

The pass runs inside graphify's build, after extraction:

1. It collects every member call graphify left unresolved, from `per_file[*]["raw_calls"]`.
2. It hands the sources the PHP files graphify parsed as the corpus they may read declarations
   from. The build bounds that set, never a filesystem walk.
3. It asks each call target source, in precedence order, for the receiver's class.
4. It maps each `(class FQN, method)` pair to a graphify node id.
5. It appends a `calls` edge for each proven target.

### Source precedence

`default_sources()` orders the sources: `PhpStanSource` first, then `TreeSitterSource`. PHPStan
knows untyped properties, chains and docblocks that the parser must refuse, so it answers first.

A site the first source proves is never offered to the second. A weaker source can add reach. It
can never overwrite a stronger source's answer. Every path through the resolver, the exception
path included, writes a status record.

The tree-sitter source reads what the code states. Its collectors cover promoted constructor
parameters, typed properties, constructor-body assignments and local `new` bindings. A new kind
of type evidence is a new collector in `syntax/collectors.py`. The walker does not change.

### Scoped calls

`self::`, `parent::`, `static::` and `Class::method()` reach graphify with the scope as the
callee and the method name dropped. The resolver recovers the name without parsing PHP. It asks
the graph which methods that class declares and offers each one as a candidate. The type source
refuses every name not written on that line, so each site yields at most one edge.

graphifyy 0.9.87 and later resolves these scoped calls itself. It binds `Class::method()` to the
method and emits no coarse class edge.

## Edges

`graph_adapter.py` writes each added `calls` edge with these keys:

| Key | Value |
|---|---|
| `_resolver` | `"graphify-php"`. Select, count or drop this package's edges without a rebuild |
| `_via` | The technique that proved the target, for example `tree_sitter:promoted-parameter` or `phpstan:interface` |
| `confidence` | `EXTRACTED` (score 1.0) when the type was read from source, `INFERRED` (score 0.8) when derived |
| `_refines` | The class node of a coarse `caller -> Class` edge this method-level edge narrows |

The marker is not `_origin`. graphify overwrites `_origin` with `"ast"` on every edge after the
resolvers run, where it means the eviction tier, not the author.

`_refines` appears only on graphify releases older than 0.9.87. There, `Class::method()` already
has a coarse `caller -> Class` edge. The method-level edge connects different endpoints, so both
are published. `_refines` lets a consumer collapse the pair instead of counting the call twice.
On 0.9.87 and later there is no coarse edge and the key stays off.

The adapter matches graphify's schema. Class labels are short names, so an FQN is matched on its
last segment and re-checked against the file path. Method labels look like `.name()`. A graphify
schema change is fixed in this module only.

## Routes

The `php_routes` pass publishes what `#[Route]` attributes declare. Each declaration becomes one
node, labelled with the path template. It carries the route name, the HTTP methods and whether
the path was composed whole. One `handled_by` edge links it to the controller method.

The node key is `Class::method#path`, so two controller methods declaring one path stay two
nodes. The relation is `handled_by`, not `routed_to`, because `routed_to` already means messenger
transport routing.

A declaration whose controller method is missing from the graph still gets its node. Only the
edge is withheld, counted under `controller_not_in_graph`.

## Unresolved reasons

Every call left alone is counted under a fixed constant. The counts form a histogram and the work
list for the next version, so a reason string never contains a variable. A source's reasons are
prefixed with the source name, for example `tree_sitter:chained-call` or
`phpstan:phpstan-receiver-not-typed`.

The resolver owns `no_source_resolved`, `target_not_in_graph`, `self_call`,
`scoped_method_not_recovered` and `scoped_method_ambiguous`. The tree-sitter source refuses
chained calls, dynamic names, `__call` dispatch and container or reflection receivers, among
others.

## Extending

A third source, such as a language server or an LSIF index, implements one protocol:
`CallTargetSource` in `ports.py`. Add it to `default_sources()` in `__init__.py`. `resolver.py`
does not change.
