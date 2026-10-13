# Setup

Get from a PHP repository to a graphify build with resolved PHP call edges.

## 1. Install

Install graphify-php from a clone, in editable mode, into the same Python environment as
graphify. With pipx:

```bash
git clone https://github.com/OxCom/graphify-php.git ~/src/graphify-php
pipx inject --editable --include-apps graphifyy ~/src/graphify-php
```

[Install](install.html) covers requirements, virtual environments, PHPStan, verification,
upgrades and troubleshooting.

The one runtime dependency is pyyaml, which the Symfony configuration pass reads YAML with.
graphify and tree-sitter come from the environment that runs the build. The package does not pin
them, because pinning would fight graphify's own pins.

## 2. Register

Registration is one call, made before graphify builds:

```python
import graphify_php

graphify_php.register()
```

`register()` adds three resolvers to graphify's resolver registry: `php_member_calls`,
`php_routes` and `php_symfony_config`. It is idempotent by name. It imports graphify lazily, so
importing the package never requires graphify.

`graphify-php-build` calls `register()` for you. Call it yourself only when you drive graphify
from your own Python code.

## 3. Run a build

```bash
graphify-php-build [--graph PATH] [--expect RESOLVER] -- update .
```

Everything after `--` goes to graphify unchanged. Options before it belong to the wrapper. See
[Configuration](configuration.html) for the flags, exit codes and environment variables.

The wrapper sets `PYTHONHASHSEED=0` before it calls graphify. graphify re-execs itself to pin
that seed for `update`, `extract`, `cluster-only` and `label`. The re-exec would replace the
process and lose both the registration and the gate. Setting the same seed skips the re-exec and
keeps graphify's determinism guarantee.

## Why the wrapper exists

graphify runs registered resolvers inside a `try` block:

```python
try:
    resolver.resolve(per_file, all_nodes, all_edges)
except Exception as exc:
    _LOG.warning("%s resolution failed, skipping: %s", resolver.name, exc)
```

A resolver that raises is skipped, and the build still publishes. A resolver that was never
registered is not even logged. Both produce a valid graph with fewer `calls` edges. That is the
same shape as the defect this package fixes, so the failure stays invisible.

Each resolver therefore writes its own status record. `graphify-php-build` reads it after the
build and refuses to publish when an expected resolver did not complete. On a refusal it restores
the previous graph and exits non-zero.

`skipped` with a reason counts as success. A repository without PHPStan in `vendor/` is a normal
skip. `failed` and silence are not.

## Verify it works

```bash
PYTHONPATH=src python -m pytest tests/test_cli_end_to_end.py -q
```

The test generates a small PHP project in a temporary directory. It runs graphify over it twice,
once plain and once with `register()` called first, and asserts the difference. It needs no
reference application and writes nothing outside `tmp_path`.

## Next steps

- [Architecture](architecture.html): how sources, precedence and edges fit together.
- [PHPStan source](phpstan.html): the precise source and how it runs.
- [Evaluation](evaluation.html): measure a resolver's precision.
