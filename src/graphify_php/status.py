"""Build-scoped status record for every resolver this package registers.

Why this exists before any resolution code. graphify runs registered resolvers like this:

    try:
        resolver.resolve(per_file, all_nodes, all_edges)
    except Exception as exc:
        _LOG.warning("%s resolution failed, skipping: %s", resolver.name, exc)

A resolver that raises is skipped, the build succeeds, and the graph is published without its
edges. A resolver that is never registered — because the seam moved in an upgrade — behaves
identically. Both failures look exactly like a codebase that happens to have few calls, which
is the shape of the defect this package exists to fix, so the failure would be invisible
precisely where it matters.

The log line is not a usable signal: it goes to graphify's logger, not to whatever runs the
build. So each resolver writes its own record here, and `graphify-php-build` refuses to publish
a graph whose expected resolvers did not complete.

The record separates three outcomes on purpose:

- `completed` — ran, and the counters say what it did.
- `skipped` — ran and decided there was nothing to do, with a reason. A repository with no
  PHPStan in `vendor/` is a normal skip, not a fault.
- `failed` — raised. The build must not publish.

Counters are per resolver and deliberately include the denominator (`eligible`), because
"resolved 400 calls" means nothing without "out of how many", and a regression that halves
coverage keeps producing a large, healthy-looking number.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The build writes here; the wrapper reads it. An environment variable rather than a fixed
# path because one machine indexes ~20 repositories and their builds must not share a file.
STATUS_PATH_ENV = "GRAPHIFY_PHP_STATUS"
DEFAULT_STATUS_PATH = "graphify-out/.graphify-php-status.json"


@dataclass
class ResolverStatus:
    name: str
    state: str = "started"          # started | completed | skipped | failed
    reason: str = ""                # why it skipped, or the exception text
    files_seen: int = 0
    eligible_sites: int = 0         # member calls this resolver could have resolved
    resolved_sites: int = 0         # ... and did
    edges_added: int = 0
    unresolved: dict = field(default_factory=dict)   # reason -> count
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def unresolvable(self, reason: str, count: int = 1) -> None:
        """Record a call site left alone, and why.

        Reasons are a fixed vocabulary in the resolvers, never free text: they are the work
        list for the next version, and a histogram of unique strings is not a work list.
        """
        self.unresolved[reason] = self.unresolved.get(reason, 0) + count


def status_path() -> Path:
    return Path(os.environ.get(STATUS_PATH_ENV) or DEFAULT_STATUS_PATH)


def write(records: list[ResolverStatus]) -> Path:
    """Write the build record, merging with anything already there.

    Merging rather than overwriting because resolvers run one after another and each writes as
    it finishes; a crash after the second of three still leaves the first two on disk, which is
    what makes the failure diagnosable.
    """
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if path.exists():
        try:
            existing = {r["name"]: r for r in json.loads(path.read_text()).get("resolvers", [])}
        except (ValueError, OSError):
            existing = {}
    for record in records:
        existing[record.name] = asdict(record)
    payload = {"schema": 1, "written_at": time.time(), "resolvers": list(existing.values())}
    path.write_text(json.dumps(payload, indent=2))
    return path


def read() -> dict:
    path = status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
