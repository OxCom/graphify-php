"""`graphify-shrink-check` — run the attributed-loss shrink guard on two builds from the shell.

graphify-mesh calls this out of process and reads two things: the LossReport JSON on stdout and
the exit code. Both are a contract:

    0  accept
    1  refuse
    2  usage or I/O error (no report is printed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .shrink import (
    DEFAULT_POLICY,
    DEFAULT_TOLERANCE,
    POLICIES,
    POLICY_ATTRIBUTED,
    POLICY_COUNT,
    classify_loss,
)

EXIT_ACCEPT = 0
EXIT_REFUSE = 1
EXIT_ERROR = 2

NO_MANIFEST_REASON = "no previous manifest; attribution impossible, count policy used"

POLICY_ENV = "GRAPHIFY_SHRINK_POLICY"
TOLERANCE_ENV = "GRAPHIFY_SHRINK_TOLERANCE"


class InputError(Exception):
    pass


def load_json(path: Path, *, missing_ok: bool) -> dict:
    """A missing manifest is a first build or a wiped cache, so it reads as empty. A missing graph
    is not something the guard can judge, so it is an error."""
    if missing_ok and not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def env_policy() -> str:
    return os.environ.get(POLICY_ENV) or DEFAULT_POLICY


def env_tolerance() -> float:
    raw = os.environ.get(TOLERANCE_ENV)
    if not raw:
        return DEFAULT_TOLERANCE
    try:
        return float(raw)
    except ValueError as exc:
        raise InputError(f"{TOLERANCE_ENV}={raw!r} is not a number") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphify-shrink-check",
        description="Compare two graphify builds and refuse a shrink that nothing on disk "
        "explains. Prints a LossReport as JSON; exit 0 accept, 1 refuse, 2 error.",
    )
    parser.add_argument("--old-graph", type=Path, required=True)
    parser.add_argument("--new-graph", type=Path, required=True)
    parser.add_argument("--old-manifest", type=Path, required=True)
    parser.add_argument("--new-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True, help="repository the graph indexes")
    parser.add_argument(
        "--policy",
        default=None,
        help=f"{', '.join(POLICIES)} (default: ${POLICY_ENV}, else {DEFAULT_POLICY})",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help=f"fraction of loss tolerated (default: ${TOLERANCE_ENV}, else {DEFAULT_TOLERANCE})",
    )
    parser.add_argument(
        "--producer-changed",
        action="store_true",
        help="graphify or its config changed, so files may legitimately leave the corpus",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        opts = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on bad usage already; `--help` exits 0 and stays 0.
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR

    try:
        policy = opts.policy or env_policy()
        tolerance = env_tolerance() if opts.tolerance is None else opts.tolerance
        old_manifest = load_json(opts.old_manifest, missing_ok=True)
        # With no old manifest every file on disk reads as changed, which explains every loss.
        # Attribution cannot judge this build, so the legacy count floor does, and the report says
        # so: its policy reads "count" and the note sits in reasons, also on an accept.
        unjudgeable = not old_manifest and policy == POLICY_ATTRIBUTED
        if unjudgeable:
            policy = POLICY_COUNT
        report = classify_loss(
            load_json(opts.old_graph, missing_ok=False),
            load_json(opts.new_graph, missing_ok=False),
            old_manifest,
            load_json(opts.new_manifest, missing_ok=True),
            opts.root,
            policy=policy,
            tolerance=tolerance,
            producer_changed=opts.producer_changed,
        )
        if unjudgeable:
            report.reasons.append(NO_MANIFEST_REASON)
    except (InputError, ValueError) as exc:
        print(f"graphify-shrink-check: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        # An uncaught crash exits 1, which the contract reads as a verdict of refuse with no
        # report. A guard that could not judge says so with the error code instead.
        print(
            f"graphify-shrink-check: internal error: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return EXIT_ERROR

    print(json.dumps(report.to_dict(), indent=2))
    return EXIT_ACCEPT if report.verdict == "accept" else EXIT_REFUSE


def _console_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    _console_main()
