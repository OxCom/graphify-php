# Configuration

graphify-php has no configuration file. A build reads a few environment variables and the
wrapper's own flags.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GRAPHIFY_PHP_STATUS` | `graphify-out/.graphify-php-status.json` | Path of the status record the resolvers write and the wrapper reads |
| `GRAPHIFY_PHP_GRAPH` | unset | Graph artifact the wrapper protects. Overrides `GRAPHIFY_OUT` |
| `GRAPHIFY_OUT` | `graphify-out` | graphify's output directory. The wrapper protects `$GRAPHIFY_OUT/graph.json` when `GRAPHIFY_PHP_GRAPH` is unset |
| `GRAPHIFY_PHP_PHPSTAN_TIMEOUT` | `900` | Seconds the PHPStan run may take |
| `GRAPHIFY_PHP_EXTERNAL_STUBS` | unset | `1` binds a proven call into `vendor/` to an `external` stub node instead of dropping it |

### `GRAPHIFY_PHP_STATUS`

One machine can index many repositories, and their builds must not share a record. An empty value
falls back to the default. The default path is relative to the working directory.

### `GRAPHIFY_PHP_GRAPH` and `GRAPHIFY_OUT`

`--graph` wins over both. Without it, `GRAPHIFY_PHP_GRAPH` names the file directly. Otherwise the
wrapper uses `graph.json` under `GRAPHIFY_OUT`, the same override graphify reads.

### `GRAPHIFY_PHP_PHPSTAN_TIMEOUT`

The value is parsed as a float. An empty or non-numeric value falls back to 900 seconds.

### `GRAPHIFY_PHP_EXTERNAL_STUBS`

Only the exact value `1` turns stubs on. With it, the resolver mints one body-less `external`
node per vendor class a proven call targets. It is off by default because graphify core refuses
stubs for `calls`: they become hub nodes. The status record counts `external_stubbed`,
`stub_nodes_added` and `stub_edges_added`. All three stay zero with the flag off.

### Tests only

`GRAPHIFY_PHP_REFERENCE_PROJECT` names a Symfony project root for two integration tests,
`tests/test_container_integration.py` and `tests/test_phpstan_integration.py`. They skip
themselves without it. The package itself never reads it.

## Wrapper flags

```bash
graphify-php-build [--graph PATH] [--expect RESOLVER] -- update .
```

Options before `--` belong to the wrapper. Everything after it goes to graphify unchanged. A
shared namespace would break when graphify adds a flag the wrapper already uses.

| Flag | Default | Meaning |
|---|---|---|
| `--graph PATH` | `$GRAPHIFY_OUT/graph.json` | Graph artifact to protect |
| `--expect RESOLVER` | `php_member_calls`, `php_routes`, `php_symfony_config` | Resolver that must report. Repeatable. Any `--expect` replaces the whole default list |
| `-h`, `--help` | | Print help. Works without graphify installed |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every expected resolver reported `completed` or `skipped` |
| `2` | An expected resolver reported `failed`, or never finished |
| `3` | An expected resolver never reported, or no record was written |
| other | graphify's own exit code, when graphify itself failed |

On a non-zero gate result the wrapper restores the graph that existed before the build. When no
graph existed, it deletes the one the build wrote. An unreadable record counts as exit `3`.

The wrapper deletes any old status record before it starts. A record from an earlier build would
otherwise satisfy this build's gate.

## Status record

The record is JSON with three top-level keys: `schema` (currently `1`), `written_at` and
`resolvers`. Each resolver merges its own entry into `resolvers` as it finishes. A crash after
the second of three resolvers still leaves the first two on disk. An entry holds:

| Field | Meaning |
|---|---|
| `name` | Resolver name |
| `state` | `started`, `completed`, `skipped` or `failed` |
| `reason` | Why it skipped, or the exception text |
| `files_seen` | Files the resolver saw |
| `eligible_sites` | Calls it could have resolved |
| `resolved_sites` | Calls it did resolve |
| `edges_added` | Edges appended to the graph |
| `external_stubbed`, `stub_nodes_added`, `stub_edges_added` | Stub counters, zero with stubs off |
| `unresolved` | Reason string to count |
| `started_at`, `finished_at` | Timestamps |

Counters always carry the denominator. "Resolved 400 calls" means nothing without "out of how
many", and a regression that halves coverage still produces a large number.

The unit of an `unresolved` count depends on the reason. Source reasons count sites. `probe:`
reasons count candidate names. `scoped_method_*` reasons count scoped groups. The graph-lookup
reasons `target_not_in_graph` and `self_call` count target records, so a two-arm site can appear
twice.
