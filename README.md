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
2. asks each configured **call target source** in precedence order for the receiver's class —
   the precise source (a PHP type engine that already knows the project's types) first, the
   tree-sitter parser second. A site the first source proves is never offered to the second, so
   a weaker source can add reach but can never overwrite a stronger source's answer;
3. maps each `(class FQN, method)` to a graphify node id;
4. appends `calls` edges carrying the target's confidence (`EXTRACTED` when the type was read
   from source, `INFERRED` when it was derived) and `_origin: "graphify-php"`, which is how a
   reader tells our edges from graphify's own `ast` and `semantic` ones and can drop them
   without a rebuild.

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

## Extending

Adding a third source — a language server, an LSIF index — means implementing one protocol,
`CallTargetSource` in `ports.py`, and adding it to the factory in `__init__.py`. `resolver.py`
depends on the protocols and nothing else, and does not change.

## License

Apache-2.0.
