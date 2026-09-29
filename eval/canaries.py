"""Planted fake secrets, and the check that they never reach an artifact.

The package's rule is that identifiers may be indexed and values never. A rule with no test is
a preference: `FALLBACK_DSN`, `MAILER_API_KEY` and `mailer_password` are identifiers and are
expected to appear in a graph, while the strings they are bound to must not appear anywhere —
graph output, the status record, logs, diagnostics, error text. This module makes the second
half checkable by planting values that cannot occur by accident and then looking for them.

Every planted value carries the marker `CANARY`. None of them opens anything; writing a real
credential to make this test realistic would create the exact exposure the test exists to
prevent, and the check works identically on a fake.

The scan takes artifact text, not artifact types. A leak check that knew the graph schema
would stop working the day a new artifact is added, which is the day it is most needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

MARKER = "CANARY"


@dataclass(frozen=True)
class Canary:
    name: str
    kind: str              # where a real secret of this shape would live
    value: str
    planted_in: str        # path relative to FIXTURE_ROOT
    identifier: str        # the name beside it, which IS allowed in an artifact


CANARIES: tuple[Canary, ...] = (
    Canary(
        name="env_default",
        kind="fallback value for a missing environment variable",
        value="CANARY-env-default-53bd8ac017",
        planted_in="secrets.php",
        identifier="MAILER_API_KEY",
    ),
    Canary(
        name="dsn",
        kind="connection string with an inline password",
        value="smtp://canary-user:CANARY-dsn-pw-4a91f3@mail.invalid:587/?tls=1",
        planted_in="secrets.php",
        identifier="FALLBACK_DSN",
    ),
    Canary(
        name="token_literal",
        kind="API token as a class constant",
        value="cnry_live_CANARY0000token0000literal0000deadbeef",
        planted_in="secrets.php",
        identifier="API_TOKEN",
    ),
    Canary(
        name="attribute_argument",
        kind="secret passed as an attribute argument",
        value="CANARY-attr-arg-7c1f0d2b9e",
        planted_in="secrets.php",
        identifier="Sensitive",
    ),
    Canary(
        name="yaml_value",
        kind="parameter value in a service definition",
        value="CANARY-yaml-value-9f24e6b8d0",
        planted_in="services.yaml",
        identifier="mailer_password",
    ),
)


class CanaryLeak(AssertionError):
    """Raised when a planted value is found in an artifact."""


@dataclass(frozen=True)
class Leak:
    canary: str
    artifact: str
    line: int
    excerpt: str           # the canary value replaced, so the report cannot leak it further


def canary_values() -> tuple[str, ...]:
    return tuple(canary.value for canary in CANARIES)


def verify_planted(root: Path | None = None) -> list[str]:
    """Confirm each canary is actually in its fixture.

    A leak check whose canaries are no longer in the corpus passes for the wrong reason: it
    finds nothing because nothing was ever indexed. This is the half of the test that catches
    a fixture edit silently disarming it.
    """
    base = root or FIXTURE_ROOT
    problems: list[str] = []
    for canary in CANARIES:
        path = base / canary.planted_in
        if not path.exists():
            problems.append(f"{canary.name}: fixture missing at {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if canary.value not in text:
            problems.append(f"{canary.name}: value not present in {canary.planted_in}")
        if canary.identifier not in text:
            problems.append(f"{canary.name}: identifier {canary.identifier} not present in {canary.planted_in}")
        if MARKER not in canary.value:
            problems.append(f"{canary.name}: value carries no {MARKER} marker, so it may be a real secret")
    return problems


def scan_text(artifact: str, text: str) -> list[Leak]:
    found: list[Leak] = []
    for canary in CANARIES:
        if canary.value not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if canary.value in line:
                found.append(
                    Leak(
                        canary=canary.name,
                        artifact=artifact,
                        line=number,
                        excerpt=line.strip().replace(canary.value, f"<{canary.name} redacted>")[:200],
                    )
                )
    return found


def scan_artifacts(artifacts: Mapping[str, str]) -> list[Leak]:
    """Scan a label -> text mapping. The caller decides what counts as an artifact."""
    leaks: list[Leak] = []
    for artifact, text in artifacts.items():
        leaks.extend(scan_text(artifact, text))
    return leaks


def collect_artifacts(paths: Iterable[Path]) -> dict[str, str]:
    """Read files and directory trees into the mapping `scan_artifacts` takes.

    Binary content is decoded with replacement rather than skipped: a canary that survives
    into a compressed or binary artifact is still a leak, and a scan that skipped the file
    would report a clean run.
    """
    artifacts: dict[str, str] = {}
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    artifacts[str(child)] = child.read_text(encoding="utf-8", errors="replace")
        elif path.is_file():
            artifacts[str(path)] = path.read_text(encoding="utf-8", errors="replace")
    return artifacts


def assert_no_leaks(artifacts: Mapping[str, str]) -> None:
    leaks = scan_artifacts(artifacts)
    if leaks:
        detail = "\n".join(f"  {leak.canary} in {leak.artifact}:{leak.line}: {leak.excerpt}" for leak in leaks)
        raise CanaryLeak(f"{len(leaks)} planted value(s) reached an artifact:\n{detail}")
