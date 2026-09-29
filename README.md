# graphify-php

PHP member-call resolution for [graphify](https://github.com/OxCom/graphify). It binds
`$this->emails->reminderSubmitWeek($user)` to the method node it actually calls, so a PHP graph
has a call graph instead of a list of names.

## The measurement

graphify ships cross-file member-call resolvers for Swift, Python, Ruby, TypeScript, C#, Java
and Rust, and none for PHP. Its PHP extractor marks `$obj->method()` as a member call and the
shared cross-file pass then drops it, because a bare `method` collides across the corpus and a
name-only match is not evidence.

On the reference Symfony application, measured from the published graph:

| | |
|---|---|
| PHP callable nodes | 3426 |
| `calls` edges in the whole graph | 2172 |
| PHP callables with an outgoing `calls` edge | 24.4 % |
| PHP callables with an incoming `calls` edge | 20.1 % |

Three quarters of the PHP callables in that graph are connected to nothing they call. Every
"what calls this", "what does this touch" and blast-radius question over PHP is answered from
that quarter.

## What it does

The pass runs inside graphify's build, after extraction:

1. collects every member call graphify left unresolved, from `per_file[*]["raw_calls"]`;
2. hands the sources the PHP files graphify parsed as the `corpus` they may read declarations
   from. A leaf service class has nothing unresolved in it, so it never appears among the sites,
   and a source indexing only those files cannot tell whether it declares the method being
   called on it. The set is bounded by the build, never by a filesystem walk;
3. asks each configured **call target source** in precedence order for the receiver's class —
   the precise source (a PHP type engine that already knows the project's types) first, the
   tree-sitter parser second. A site the first source proves is never offered to the second, so
   a weaker source can add reach but can never overwrite a stronger source's answer;
4. maps each `(class FQN, method)` to a graphify node id;
5. appends `calls` edges carrying the target's confidence (`EXTRACTED` when the type was read
   from source, `INFERRED` when it was derived) and `_resolver: "graphify-php"`, which is how a
   reader tells our edges from graphify's own and can drop them without a rebuild. `_via` names
   the technique that proved each one (`tree_sitter:promoted-parameter`, `tree_sitter:self-scope`,
   ...).

The marker is not `_origin`: graphify overwrites that on every edge with `"ast"` after the
resolvers run (`extract.py:8285-8289`), where it means the eviction tier rather than the author.

### Scoped calls

`self::`, `parent::`, `static::` and `Class::method()` reach graphify with `is_member_call`
false and the **scope** as the callee — the method name is dropped
(`extractors/engine.py:6147-6152`). The resolver recovers it without parsing PHP: it asks the
graph which methods that class declares and offers each as a candidate, and the type source
refuses every name not actually written on that line. A class with ten methods costs ten
lookups and yields at most one edge.

`Class::method()` additionally already has a coarse `caller -> Class` edge, because graphify
resolves the scope name to the class node. Our method-level edge is a **second** edge between
different endpoints, so both are published. The narrow one carries `_refines` naming the coarse
one, so a consumer can collapse the pair instead of counting the call twice.

A source returns a target only when it can prove it. Chained calls, dynamic names, `static::`
and anything through `__call` are counted by reason, not guessed at: a wrong call edge is worse
than a missing one, and every counted reason is a line on the next version's work list.

## Install and register

```bash
pip install graphify-php
```

No runtime dependencies. graphify and tree-sitter come from the environment that runs the build;
pinning them here would fight graphify's own pins.

Registration is one call, made before graphify builds:

```python
import graphify_php

graphify_php.register()
```

It is idempotent and imports graphify lazily, so importing the package never requires graphify
to be installed.

## Run a build

```bash
graphify-php-build -- build .
```

Everything after `--` is passed to graphify unchanged; options before it belong to the wrapper:

| Option | Meaning |
|---|---|
| `--graph PATH` | the graph artifact to protect (default `$GRAPHIFY_OUT/graph.json`) |
| `--expect RESOLVER` | a resolver that must report; repeatable, defaults to `php_member_calls` |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | every expected resolver reported `completed` or `skipped` |
| 2 | an expected resolver reported `failed`, or never finished |
| 3 | an expected resolver never reported at all |
| other | graphify's own exit code, when graphify itself failed |

The wrapper sets `PYTHONHASHSEED=0` before calling graphify. graphify re-execs itself to pin
that seed for `update`, `extract`, `cluster-only` and `label` (`__main__.py:486-525`), and the
re-exec would replace the process — losing both the registration and the gate. Setting the seed
graphify would have set skips the re-exec and keeps its determinism guarantee.

## Why the wrapper exists

graphify runs registered resolvers like this:

```python
try:
    resolver.resolve(per_file, all_nodes, all_edges)
except Exception as exc:
    _LOG.warning("%s resolution failed, skipping: %s", resolver.name, exc)
```

A resolver that raises is skipped and the build still publishes. A resolver that was never
registered — because the seam moved in a graphify upgrade, or because an install left this
package out — is not even logged. Both produce a valid graph with fewer `calls` edges, which is
exactly the shape of the defect this package was written to fix. The failure would be invisible
at the one moment it matters, and the warning goes to graphify's logger, which nothing running
the build reads.

So each resolver writes its own record — state, and counters that include the denominator,
because "resolved 400 calls" says nothing without "out of how many" — and `graphify-php-build`
reads that record and refuses to publish when an expected resolver did not complete. On a
refusal the previous graph is restored and the exit code is non-zero.

`skipped` with a reason is a success: a repository with no PHP type engine in `vendor/` is a
normal skip, and conflating it with a failure is how a missing layer becomes noise. `failed`,
and silence, are not.

The record path defaults to `graphify-out/.graphify-php-status.json` and is overridable with
`GRAPHIFY_PHP_STATUS`, so one machine can index many repositories without their builds sharing
a file.

## Routes

A second registered pass publishes what `#[Route]` attributes declare. Each declaration becomes
one node — labelled with the path template, carrying the route name, the HTTP methods and
whether the path was composed whole — and one `handled_by` edge to the controller method that
serves it. On the reference Symfony application: **156 route nodes, 156 edges, no refusals**.

The node key is `Class::method#path`, so two controller methods declaring the same path stay two
nodes. That is not hypothetical: the reference application declares 156 routes over 135 distinct
path templates, the repeats being different HTTP verbs on one path.

The relation is `handled_by`, not `routed_to` — the latter already means messenger transport
routing, and merging the two would put HTTP paths and message transports in one query.

A declaration whose controller method is missing from the graph still gets its node; only the
edge is withheld, and the omission is counted under `controller_not_in_graph`. The path is a
fact about the codebase either way.

## Verifying it works

```bash
PYTHONPATH=src python -m pytest tests/test_cli_end_to_end.py -q
```

That test generates a small PHP project in a temporary directory, runs graphify over it twice —
once plain, once with `register()` called first — and asserts the difference. On the generated
fixture the plain build produces **1** `calls` edge and the registered build **5**. It needs no
reference application and writes nothing outside `tmp_path`.

## Extending

Adding a third source — a language server, an LSIF index — means implementing one protocol,
`CallTargetSource` in `ports.py`, and adding it to the factory in `__init__.py`. `resolver.py`
depends on the protocols and nothing else, and does not change.

## License

Apache-2.0.
