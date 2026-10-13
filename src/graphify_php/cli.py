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
    4  the shrink guard refused the new graph (see shrink.py); graph and manifest restored
    *  graphify's own exit code, when graphify itself failed
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import shrink, shrink_cli, status
from .config_resolver import RESOLVER_NAME as RESOLVER_NAME_CONFIG
from .resolver import RESOLVER_NAME, RESOLVER_NAME_ROUTES

EXIT_OK = 0
EXIT_RESOLVER_FAILED = 2
EXIT_RESOLVER_MISSING = 3
EXIT_SHRINK_REFUSED = 4

MESH_MANAGED_ENV = "GRAPHIFY_MESH_MANAGED"

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
        # graphify exits with a message string in places (`sys.exit("ERROR: ...")`). That is a
        # failure, and the message is all the caller gets, so it goes to stderr here.
        code = exc.code
        if isinstance(code, int):
            return code
        if code is None:
            return 0
        print(code, file=sys.stderr)
        return 1
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
        return argv[:cut], argv[cut + 1 :]
    return [], argv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphify-php-build",
        description="Run graphify with graphify_php registered, refusing to publish a graph "
        "whose expected resolvers did not complete.",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="graph artifact to protect (default: $GRAPHIFY_OUT/graph.json)",
    )
    parser.add_argument(
        "--expect",
        action="append",
        dest="expect",
        default=None,
        metavar="RESOLVER",
        help="resolver that must report (repeatable, default: every resolver "
        "this package registers)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, run_graphify: Callable[[Sequence[str]], int] | None = None
) -> int:
    ours, passthrough = _split_argv(sys.argv[1:] if argv is None else argv)
    opts = _parser().parse_args(ours)
    expected = opts.expect or [RESOLVER_NAME, RESOLVER_NAME_ROUTES, RESOLVER_NAME_CONFIG]
    graph = opts.graph or default_graph_path()

    # A record from an earlier build would satisfy this build's gate, which is the one failure
    # the gate exists to catch. Start from nothing so silence stays visible as silence.
    record_path = status.status_path()
    record_path.unlink(missing_ok=True)

    backup = graph.with_suffix(graph.suffix + ".graphify-php-prev")
    had_previous = graph.exists()
    if had_previous:
        shutil.copy2(graph, backup)
    # graphify's incremental `update` trusts the manifest to say which files are already in the
    # graph. A graph rolled back without its manifest would be skipped over next build, so the
    # shrink guard snapshots and restores the pair together.
    manifest = graph.parent / "manifest.json"
    manifest_backup = manifest.with_suffix(manifest.suffix + ".graphify-php-prev")
    had_manifest = manifest.exists()
    if had_manifest:
        shutil.copy2(manifest, manifest_backup)

    try:
        runner = run_graphify or _run_graphify
        code = runner(passthrough)
        if code != EXIT_OK:
            print(f"graphify-php-build: graphify exited {code}", file=sys.stderr)
            return code

        try:
            code, message = check(status.read(), expected)
        except Exception as exc:  # a record the gate cannot read is a record that proves nothing
            code, message = EXIT_RESOLVER_MISSING, f"status record unreadable: {exc}"
        if code == EXIT_OK:
            return _shrink_gate(
                graph,
                backup if had_previous else None,
                manifest,
                manifest_backup if had_manifest else None,
                passthrough,
            )

        # Refusing to publish means the graph a reader would pick up must be the one that was
        # there before, not the short one this build just wrote.
        if had_previous:
            os.replace(backup, graph)
            restored = f"previous graph at {graph} and its manifest left untouched"
        else:
            graph.unlink(missing_ok=True)
            restored = f"no graph published at {graph}"
        _restore_manifest(manifest, manifest_backup if had_manifest else None)
        print(f"graphify-php-build: {message}", file=sys.stderr)
        print(f"graphify-php-build: refusing to publish; {restored}", file=sys.stderr)
        return code
    finally:
        for left in (backup, manifest_backup):
            if left.exists():
                left.unlink(missing_ok=True)


def _restore_manifest(manifest: Path, manifest_backup: Path | None) -> None:
    """Put back the manifest that matches the graph being kept.

    The failed build's manifest lists files as indexed that the kept graph does not hold, and the
    next incremental `update` would skip them.
    """
    if manifest_backup is not None:
        os.replace(manifest_backup, manifest)
    else:
        # No manifest before this build: dropping the new one makes the next build a full one.
        manifest.unlink(missing_ok=True)


def _shrink_gate(
    graph: Path,
    graph_backup: Path | None,
    manifest: Path,
    manifest_backup: Path | None,
    passthrough: Sequence[str],
) -> int:
    """Refuse a graph that lost nodes or edges nothing on disk explains.

    The resolver gate only proves the resolvers ran. A build can pass it and still publish a
    graph missing half its call edges, which is the case this guard exists for.
    """
    if os.environ.get(MESH_MANAGED_ENV) == "1":
        print(
            f"graphify-php-build: {MESH_MANAGED_ENV}=1, shrink guard left to graphify-mesh",
            file=sys.stderr,
        )
        return EXIT_OK
    policy = shrink_cli.env_policy()
    if policy == shrink.POLICY_OFF or graph_backup is None:
        return EXIT_OK
    if manifest_backup is None and policy == shrink.POLICY_ATTRIBUTED:
        # With no old manifest every file on disk reads as changed, which explains every loss.
        # Attribution cannot judge this build, so the legacy count floor does.
        print(
            f"graphify-php-build: no previous manifest at {manifest}; shrink guard cannot "
            f"attribute loss, using the {shrink.POLICY_COUNT} policy",
            file=sys.stderr,
        )
        policy = shrink.POLICY_COUNT

    try:
        report = shrink.classify_loss(
            shrink_cli.load_json(graph_backup, missing_ok=False),
            shrink_cli.load_json(graph, missing_ok=False),
            shrink_cli.load_json(manifest_backup, missing_ok=False) if manifest_backup else {},
            shrink_cli.load_json(manifest, missing_ok=True),
            Path(repo_root_from(passthrough)),
            policy=policy,
            tolerance=shrink_cli.env_tolerance(),
        )
        verdict = report.verdict
        summary = (
            f"shrink guard ({policy}) refused: nodes {report.nodes['old']} -> "
            f"{report.nodes['new']}, unexplained {report.nodes['unexplained']} "
            f"(floor {report.nodes['floor']} of {report.nodes['base']}); edges "
            f"{report.edges['old']} -> {report.edges['new']}, unexplained "
            f"{report.edges['unexplained']}, resolver lost {report.edges['resolver_lost']} "
            f"of {report.edges['resolver_old']}"
        )
        reasons = report.reasons
    except Exception as exc:
        # A guard that cannot judge the build fails closed, like the resolver gate does. Any
        # exception, not only bad input: one that escaped would leave the short graph published.
        verdict, reasons = "refuse", []
        summary = f"shrink guard could not run: {type(exc).__name__}: {exc}"
    if verdict == "accept":
        return EXIT_OK

    os.replace(graph_backup, graph)
    _restore_manifest(manifest, manifest_backup)
    print(f"graphify-php-build: {summary}", file=sys.stderr)
    for reason in reasons:
        print(f"graphify-php-build:   - {reason}", file=sys.stderr)
    print(
        f"graphify-php-build: refusing to publish; previous graph at {graph} and its "
        "manifest restored",
        file=sys.stderr,
    )
    return EXIT_SHRINK_REFUSED


def repo_root_from(args: Sequence[str]) -> str:
    """The repository graphify was pointed at, which is what the type sources resolve against.

    graphify takes it as a positional path (`graphify update <path>`), so the cwd is not it: a
    build launched from anywhere else would hand the sources a root with no PHP under it.
    Resolved, so the shrink guard matches graphify's absolute paths through a symlinked root.
    """
    for arg in args[1:]:
        if not arg.startswith("-") and Path(arg).is_dir():
            return str(Path(arg).resolve())
    return str(Path.cwd().resolve())


def _console_main() -> None:
    ours, passthrough = _split_argv(sys.argv[1:])
    # Help needs no graphify, and `register` imports it; an environment without graphify
    # would otherwise lose `--help` to a ModuleNotFoundError. A bare `--help` with no `--`
    # lands in graphify's half by the split rule, but nothing else is there to pass on, so it
    # is ours.
    help_flags = {"-h", "--help"}
    if help_flags & set(ours) or (
        not ours and len(passthrough) == 1 and passthrough[0] in help_flags
    ):
        _parser().print_help()
        sys.exit(0)

    from . import register

    register(repo_root=repo_root_from(passthrough))
    sys.exit(main())


if __name__ == "__main__":
    _console_main()
