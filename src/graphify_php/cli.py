"""`graphify-php-build` — run graphify with this package registered, and gate the publish.

The wrapper exists because graphify's resolver driver is deliberately forgiving:

    except Exception as exc:
        _LOG.warning("%s resolution failed, skipping: %s", resolver.name, exc)

A resolver that raises is skipped and the build still publishes. A resolver that was never
registered — because the seam moved in an upgrade, or because an install left this package out —
is not even logged. Both produce a valid graph with fewer `calls` edges, which is exactly the
shape of the defect this package was written to fix, so the failure is invisible at the one
moment it matters.

So the gate is here rather than in the resolver: run the build, then read the record the resolver
wrote, and refuse to publish unless every expected resolver reported a terminal state. `skipped`
with a reason is a success — a repository without PHPStan in `vendor/` is a normal skip. `failed`
is not, and neither is silence.

Exit codes:

    0  every expected resolver reported `completed` or `skipped`
    2  an expected resolver reported `failed` (or never finished)
    3  an expected resolver never reported at all, or no record was written
    *  graphify's own exit code, when graphify itself failed
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import status
from .resolver import RESOLVER_NAME, RESOLVER_NAME_ROUTES

EXIT_OK = 0
EXIT_RESOLVER_FAILED = 2
EXIT_RESOLVER_MISSING = 3

_TERMINAL_OK = ("completed", "skipped")


def default_graph_path() -> Path:
    """Where graphify publishes, honouring the same `GRAPHIFY_OUT` override graphify reads."""
    out = os.environ.get("GRAPHIFY_PHP_GRAPH")
    if out:
        return Path(out)
    return Path(os.environ.get("GRAPHIFY_OUT", "graphify-out")) / "graph.json"


def check(record: dict, expected: Sequence[str]) -> tuple[int, str]:
    """Decide whether this build may publish. Pure, so the gate is testable without a build."""
    resolvers = {r.get("name"): r for r in (record.get("resolvers") or [])}
    if not resolvers:
        return EXIT_RESOLVER_MISSING, (
            f"no resolver status record at {status.status_path()}: "
            f"{', '.join(expected)} never reported"
        )
    for name in expected:
        entry = resolvers.get(name)
        if entry is None:
            return EXIT_RESOLVER_MISSING, (
                f"resolver {name!r} never reported; graphify ran without it "
                f"(registration seam moved, or the package is not installed)"
            )
        state = entry.get("state", "")
        if state not in _TERMINAL_OK:
            reason = entry.get("reason") or "no reason recorded"
            return EXIT_RESOLVER_FAILED, f"resolver {name!r} state={state}: {reason}"
    return EXIT_OK, ""


def _run_graphify(args: Sequence[str]) -> int:
    from graphify.__main__ import main as graphify_main

    # graphify re-execs itself with PYTHONHASHSEED=0 for `update`, `extract`, `cluster-only` and
    # `label` (__main__.py:486-525), which would replace this process — losing the registration
    # made a moment ago and the gate that runs after. It skips the re-exec when the caller has
    # already chosen a seed, so choosing the same one it would have keeps us in-process and
    # keeps graphify's determinism guarantee intact.
    os.environ.setdefault("PYTHONHASHSEED", "0")

    argv = sys.argv
    sys.argv = ["graphify", *args]
    try:
        graphify_main()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = argv


def _split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Our options before `--`, graphify's after it.

    A shared namespace would break the moment graphify adds a flag we already use, and the
    wrapper must stay transparent to every graphify option, present and future.
    """
    argv = list(argv)
    if "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    return [], argv


def main(argv: Sequence[str] | None = None,
         run_graphify: Callable[[Sequence[str]], int] | None = None) -> int:
    ours, passthrough = _split_argv(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="graphify-php-build",
        description="Run graphify with graphify_php registered, refusing to publish a graph "
                    "whose expected resolvers did not complete.",
    )
    parser.add_argument("--graph", type=Path, default=None,
                        help="graph artifact to protect (default: $GRAPHIFY_OUT/graph.json)")
    parser.add_argument("--expect", action="append", dest="expect", default=None,
                        metavar="RESOLVER",
                        help="resolver that must report (repeatable, default: every resolver "
                             "this package registers)")
    opts = parser.parse_args(ours)
    expected = opts.expect or [RESOLVER_NAME, RESOLVER_NAME_ROUTES]
    graph = opts.graph or default_graph_path()

    # A record from an earlier build would satisfy this build's gate, which is the one failure
    # the gate exists to catch. Start from nothing so silence stays visible as silence.
    record_path = status.status_path()
    record_path.unlink(missing_ok=True)

    backup = graph.with_suffix(graph.suffix + ".graphify-php-prev")
    had_previous = graph.exists()
    if had_previous:
        shutil.copy2(graph, backup)

    try:
        runner = run_graphify or _run_graphify
        code = runner(passthrough)
        if code != EXIT_OK:
            print(f"graphify-php-build: graphify exited {code}", file=sys.stderr)
            return code

        code, message = check(status.read(), expected)
        if code == EXIT_OK:
            return EXIT_OK

        # Refusing to publish means the graph a reader would pick up must be the one that was
        # there before, not the short one this build just wrote.
        if had_previous:
            os.replace(backup, graph)
            restored = f"previous graph at {graph} left untouched"
        else:
            graph.unlink(missing_ok=True)
            restored = f"no graph published at {graph}"
        print(f"graphify-php-build: {message}", file=sys.stderr)
        print(f"graphify-php-build: refusing to publish; {restored}", file=sys.stderr)
        return code
    finally:
        if backup.exists():
            backup.unlink(missing_ok=True)


def repo_root_from(args: Sequence[str]) -> str:
    """The repository graphify was pointed at, which is what the type sources resolve against.

    graphify takes it as a positional path (`graphify update <path>`), so the cwd is not it: a
    build launched from anywhere else would hand the sources a root with no PHP under it.
    """
    for arg in args[1:]:
        if not arg.startswith("-") and Path(arg).is_dir():
            return arg
    return "."


def _console_main() -> None:
    from . import register

    _, passthrough = _split_argv(sys.argv[1:])
    register(repo_root=repo_root_from(passthrough))
    sys.exit(main())


if __name__ == "__main__":
    _console_main()
