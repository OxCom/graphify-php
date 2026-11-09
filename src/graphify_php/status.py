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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path

# The sidecar schema (shrink.py) owns the stub vocabulary; the record states it in the same words.
from .shrink import STUBS_OFF as STUBS_OFF
from .shrink import STUBS_ON as STUBS_ON

# The build writes here; the wrapper reads it. An environment variable rather than a fixed
# path because one machine indexes ~20 repositories and their builds must not share a file.
STATUS_PATH_ENV = "GRAPHIFY_PHP_STATUS"
DEFAULT_STATUS_PATH = "graphify-out/.graphify-php-status.json"

# Distribution names, not import names: graphify is published on PyPI as `graphifyy`.
PRODUCER_NAME = "graphify-php"
GRAPHIFY_DISTRIBUTION = "graphifyy"


def installed_version(distribution: str) -> str:
    """Installed version of `distribution`, or "unknown" when it carries no metadata."""
    try:
        return _package_version(distribution)
    except (PackageNotFoundError, ValueError):
        return "unknown"


@dataclass
class ResolverStatus:
    name: str
    state: str = "started"  # started | completed | skipped | failed
    reason: str = ""  # why it skipped, or the exception text
    files_seen: int = 0
    eligible_sites: int = 0  # member calls this resolver could have resolved
    resolved_sites: int = 0  # ... and did
    edges_added: int = 0
    # Target records bound to an external stub node instead of being dropped as
    # `target_not_in_graph`, and the stub nodes and edges that produced. Zero with the flag off.
    external_stubbed: int = 0
    stub_nodes_added: int = 0
    stub_edges_added: int = 0
    # Type-engine records on a scoped site's line that no raw call carried (graphify
    # core bound the member call by bare name), edged from that site's caller. Not a resolved
    # site: they are counted here and never in `resolved_sites`.
    unsent_calls_added: int = 0
    # graphify's engine binds `$x->m()` by bare name to a node in the caller's own file. Of
    # those edges (`engine_sites_total`, the denominator), how many a type proof contradicted
    # on the edge's line, how many were dropped, how many of the drops rested on an interface
    # target, and how many proved edges replaced them. Every kept edge is counted once in
    # `engine_kept` (reason -> count), so dropped + kept sums to the total.
    engine_sites_total: int = 0
    engine_contradicted: int = 0
    engine_dropped: int = 0
    engine_dropped_interface: int = 0
    engine_proved_added: int = 0
    engine_kept: dict = field(default_factory=dict)
    # One row per dropped edge: {source, target, relation, reason, proof: {source, target}},
    # `proof.target` None when the proved body has no node. Uncapped: one row per dropped edge,
    # about 345 on the reference application.
    engine_dropped_edges: list = field(default_factory=list)
    # reason -> count. The unit differs by reason: source reasons count sites, `probe:` reasons
    # count candidate names, `scoped_method_*` count scoped groups, and the graph-lookup reasons
    # (`target_not_in_graph`, `self_call`) count target records, so a two-arm site can appear twice.
    unresolved: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def unresolvable(self, reason: str, count: int = 1) -> None:
        """Record a call site left alone, and why.

        Reasons are a fixed vocabulary in the resolvers, never free text: they are the work
        list for the next version, and a histogram of unique strings is not a work list.
        """
        self.unresolved[reason] = self.unresolved.get(reason, 0) + count


@dataclass
class MemberCallStatus(ResolverStatus):
    """The member-call record (`php_member_calls`), plus the nullsafe pass's counters.

    `nullsafe_sites_total` is every `?->` call a parser found, refused ones included;
    `nullsafe_calls_added` is how many of them got a target into the graph (an edge, a stub
    edge, or a pair the graph already held). Neither is counted in `eligible_sites`.
    """

    nullsafe_sites_total: int = 0
    nullsafe_calls_added: int = 0
    # Whether this pass minted external stubs (`STUBS_ON`/`STUBS_OFF`), decided in the build's
    # process. "" until decided. The attribution sidecar reads it from here and nowhere else.
    external_stubs: str = ""
    # graphify's cross-file pass case-folds `new Type(...)` onto a method `type()`. Of the
    # graphify `calls` edges to a method node whose line creates that name (`new_site_edges_total`,
    # the denominator; an unreadable line counts too), how many were dropped. Every kept one is
    # counted once in `new_site_kept` (reason -> count), so dropped + kept sums to the total.
    new_site_edges_total: int = 0
    new_site_edges_dropped: int = 0
    new_site_kept: dict = field(default_factory=dict)
    # One row per dropped edge: {source, target, source_location}. Audit only: no proof edge
    # stands behind a drop, so it is not in the attribution sidecar and the shrink guard sees
    # it as unexplained loss once; the edge does not come back.
    new_site_dropped_edges: list = field(default_factory=list)


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
        # This runs inside the resolver's `finally`. A file that is not a record — a list,
        # a row without a name — must not raise here and replace the resolver's own exception.
        try:
            previous = json.loads(path.read_text())
            rows = previous.get("resolvers", []) if isinstance(previous, dict) else []
            existing = {
                r["name"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)
            }
        except (ValueError, OSError, TypeError, AttributeError, KeyError):
            existing = {}
    for record in records:
        existing[record.name] = asdict(record)
    payload = {
        "schema": 1,
        "written_at": time.time(),
        # Which build of this package wrote the record, so a reader can tell an upgrade apart.
        "producer": {"name": PRODUCER_NAME, "version": installed_version(PRODUCER_NAME)},
        "resolvers": list(existing.values()),
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def read() -> dict:
    path = status_path()
    if not path.exists():
        return {}
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return record if isinstance(record, dict) else {}
