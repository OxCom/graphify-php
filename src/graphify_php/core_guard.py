"""Keep graphify core's own PHP member-call pass off the sites graphify-php decided.

graphify 0.9.103 registers `php_member_calls` itself when `graphify.extract` is imported. It
runs after this package's passes and dedupes only by (caller, target) pair, so at a site
graphify-php proved it still adds its own guess: `$mailer->get()` bound to `Emails::get` beside
the proven `Cache::get`. A pass inside our resolver cannot remove an edge added after it, so
core's registry entry is replaced by a filter that runs core's original on every raw call except
those graphify-php proved, i.e. a source claimed a target for. The filter sits just after our
last pass whatever the import order, because it needs the proof our pass publishes. A site
graphify-php refused is left to core, right or wrong: withholding it there dropped core's right
edges along with its wrong ones.

This module is the only code that touches `resolver_registry._REGISTRY`. Every outcome writes a
`php_core_guard` status record at build time, because `graphify-php-build` deletes the record
file after registration and before the build.

The record also lists the exact core edges the suppression prevented, so the shrink guard can tell
them from a regression. After the real run, core's original runs once more as a dry run over the
proved raw calls only, against copies: what it would append is what was withheld. Each such edge
is listed only when a graphify-php edge from the same caller, line and method name proves the
site. The dry run is safe only because core appends edges and writes nothing else but a parked
note on a caller node (extract.py `_park_unresolved_member_call`, graphify 0.9.103/0.9.104); those
caller nodes are deep-copied. Attribution never fails the build: missing rows only make the shrink
guard stricter.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from . import status
from .graph_adapter import SiteProofs
from .shrink import REASON_CORE_SUPPRESSED

RESOLVER_NAME = "php_core_guard"
CORE_NAME = "php_member_calls"
# Set on every resolve callable graphify-php registers. A marker rather than `__module__`, so
# the package imported under a second module path still recognises its own registrations.
OWNED = "_graphify_php_owned"

# Fixed reasons: a histogram key, never formatted with a value.
REASON_CORE_ABSENT = "core-resolver-absent"
REASON_CORE_AMBIGUOUS = "core-resolver-ambiguous"
REASON_CORE_UNEXPECTED_SHAPE = "core-resolver-unexpected-shape"
REASON_PROVED_SET_MISSING = "proved-set-missing"
REASON_PROVED_NOT_FOUND = "proved-not-found"
REASON_REGISTRY_UNEXPECTED = "registry-unexpected-shape"
# Why a withheld core edge is not listed for the shrink guard. Never fails the build.
REASON_DRY_RUN_FAILED = "dry-run-failed"
REASON_DRY_RUN_TOUCHED_BUILD = "dry-run-touched-build"
REASON_NO_PROOF_EDGE = "no-proof-edge"

_MARK = "_graphify_php_core_guard"
_ORIGINAL = "_graphify_php_core_original"


@dataclass
class CoreGuardStatus(status.ResolverStatus):
    # Core's candidate sites (PHP member raw calls) handed in, and how many were withheld.
    core_sites_total: int = 0
    suppressed_sites: int = 0
    # Proved raw calls not found by identity in core's input: non-zero means a pass between
    # ours and core's copied the raw calls, and core saw sites it should not have.
    proved_not_found: int = 0
    # Edges core's dry run would have appended at proved sites (the denominator), and the ones
    # listed in `suppressed_edge_rows` because a graphify-php edge proves the site. The rest are
    # counted in `suppressed_unlisted` (reason -> count); `dry-run-*` reasons count dry runs.
    suppressed_edges: int = 0
    suppressed_edges_listed: int = 0
    suppressed_unlisted: dict = field(default_factory=dict)
    # {source, target, relation, reason, proof: {source, target}}: the shrink sidecar's rows.
    suppressed_edge_rows: list = field(default_factory=list)
    dry_run_seconds: float = 0.0


# One build at a time per process: (per_file, {id(raw): raw}). The raw objects are held so their
# ids cannot be reused by another dict before core's pass reads them.
_proved: tuple[object, dict[int, dict]] | None = None


def prove(per_file: object, raws: Iterable[dict]) -> None:
    """Publish the raw calls graphify-php emitted an edge for in the build over `per_file`."""
    global _proved
    _proved = (per_file, {id(raw): raw for raw in raws})


def _take(per_file: object) -> dict[int, dict] | None:
    global _proved
    slot, _proved = _proved, None
    if slot is None or slot[0] is not per_file:
        return None
    return slot[1]


def _is_core_site(raw: dict) -> bool:
    # The same gate core's resolver opens with (extract.py `_resolve_php_member_calls`).
    return bool(raw.get("is_member_call")) and str(raw.get("source_file", "")).endswith(".php")


def _without(
    per_file: Sequence[dict], proved: dict[int, dict], record: CoreGuardStatus
) -> list[dict]:
    """A shallow copy of `per_file` whose `raw_calls` omit every proved raw call."""
    # By id: one raw dict listed in two files must not count as two proved calls found.
    found: set[int] = set()
    kept_files: list[dict] = []
    for result in per_file:
        raws = result.get("raw_calls") or []
        kept = []
        for raw in raws:
            hit = proved.get(id(raw)) is raw
            if hit:
                found.add(id(raw))
            if _is_core_site(raw):
                record.core_sites_total += 1
                record.suppressed_sites += hit
            if not hit:
                kept.append(raw)
        kept_files.append(result if len(kept) == len(raws) else {**result, "raw_calls": kept})
    record.proved_not_found = len(proved) - len(found)
    return kept_files


def _dry_run(
    original: Callable,
    per_file: Sequence[dict],
    proved: dict[int, dict],
    all_nodes: list,
    all_edges: list,
    record: CoreGuardStatus,
) -> None:
    """List the edges core would have appended at proved sites. Touches nothing of the build."""
    started = time.perf_counter()
    try:
        only: list[dict] = []
        for result in per_file:
            raws = [raw for raw in result.get("raw_calls") or [] if proved.get(id(raw)) is raw]
            if raws:
                only.append({**result, "raw_calls": raws})
        if not only:
            return
        callers = {raw.get("caller_nid") for result in only for raw in result["raw_calls"]}
        # Core parks a note on a caller node it cannot bind; the copy takes it instead.
        nodes = [copy.deepcopy(n) if n.get("id") in callers else n for n in all_nodes]
        edges = list(all_edges)
        n_nodes, n_edges = len(all_nodes), len(all_edges)
        original(only, nodes, edges)
        if (len(all_nodes), len(all_edges)) != (n_nodes, n_edges) or edges[:n_edges] != all_edges:
            record.suppressed_unlisted[REASON_DRY_RUN_TOUCHED_BUILD] = 1
            return
        withheld = edges[n_edges:]
        proofs = SiteProofs(nodes, all_edges)
        rows = []
        for edge in withheld:
            if not isinstance(edge, dict):
                continue
            proved = proofs.proof_for(edge)
            if proved is None:
                record.suppressed_unlisted[REASON_NO_PROOF_EDGE] = (
                    record.suppressed_unlisted.get(REASON_NO_PROOF_EDGE, 0) + 1
                )
                continue
            source, target, relation, proof = proved
            rows.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation,
                    "reason": REASON_CORE_SUPPRESSED,
                    "proof": {"source": source, "target": proof},
                }
            )
        record.suppressed_edges = len(withheld)
        record.suppressed_edges_listed = len(rows)
        record.suppressed_edge_rows = rows
    except Exception:
        # Attribution is an aid to the shrink guard, never a reason to fail the build.
        record.suppressed_edges = record.suppressed_edges_listed = 0
        record.suppressed_edge_rows = []
        record.suppressed_unlisted = {REASON_DRY_RUN_FAILED: 1}
    finally:
        record.dry_run_seconds = round(time.perf_counter() - started, 6)


def _recorded(run: Callable[[CoreGuardStatus, list, list, list], None]) -> Callable:
    def resolve(per_file, all_nodes, all_edges) -> None:
        record = CoreGuardStatus(name=RESOLVER_NAME)
        try:
            run(record, per_file, all_nodes, all_edges)
        except Exception as exc:
            record.state = "failed"
            record.reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.finished_at = time.time()
            status.write([record])

    setattr(resolve, _MARK, True)
    return resolve


def _filter(original: Callable) -> Callable:
    def run(record, per_file, all_nodes, all_edges) -> None:
        proved = _take(per_file)
        if proved is None:
            # Our pass did not run first, or failed: core unfiltered would contradict it, and
            # core withheld everywhere loses its right edges. Neither may publish.
            record.state, record.reason = "failed", REASON_PROVED_SET_MISSING
            return
        original(_without(per_file, proved, record), all_nodes, all_edges)
        if record.proved_not_found:
            # A pass between ours and core's copied raw calls: core ran on proved sites.
            record.state, record.reason = "failed", REASON_PROVED_NOT_FOUND
            return
        # After the real run, so a pair core already added elsewhere is not counted as withheld.
        _dry_run(original, per_file, proved, all_nodes, all_edges, record)
        record.state = "completed"

    resolve = _recorded(run)
    setattr(resolve, _ORIGINAL, original)
    resolve.__name__ = CORE_NAME
    return resolve


def _reporting(state: str, reason: str) -> Callable:
    def run(record, per_file, all_nodes, all_edges) -> None:
        _take(per_file)  # nothing filters here, so the slot must not keep `per_file` alive
        record.state, record.reason = state, reason

    return _recorded(run)


def original_of(resolve: Callable) -> Callable:
    """Core's own resolve behind a filter installed here, or `resolve` itself."""
    return getattr(resolve, _ORIGINAL, resolve)


def install(resolver_registry) -> None:
    """Put a filter in place of core's `php_member_calls`, just after our last pass. Idempotent.

    Core is any `php_member_calls` entry graphify-php does not own, wherever its function lives:
    matching on `__module__` would let a graphify refactor run core unfiltered.
    """
    php = frozenset({".php"})
    entries = getattr(resolver_registry, "_REGISTRY", None)
    if not isinstance(entries, list):
        # Core cannot be withheld, so its wrong edges would publish: refuse the build.
        resolver_registry.register(
            resolver_registry.LanguageResolver(
                RESOLVER_NAME, php, _reporting("failed", REASON_REGISTRY_UNEXPECTED)
            )
        )
        return
    if any(getattr(getattr(e, "resolve", None), _MARK, False) for e in entries):
        return

    matches = [
        i
        for i, e in enumerate(entries)
        if getattr(e, "name", None) == CORE_NAME
        and not getattr(getattr(e, "resolve", None), OWNED, False)
    ]
    if not matches:
        entries.append(
            resolver_registry.LanguageResolver(
                RESOLVER_NAME, php, _reporting("skipped", REASON_CORE_ABSENT)
            )
        )
        return

    entry = entries[matches[0]]
    if len(matches) > 1:
        guard = resolver_registry.LanguageResolver(
            CORE_NAME, php, _reporting("skipped", REASON_CORE_AMBIGUOUS)
        )
    elif not (
        isinstance(entry, resolver_registry.LanguageResolver)
        and callable(entry.resolve)
        and ".php" in entry.suffixes
    ):
        # The suffixes are ours: core's may be what is unexpected.
        guard = resolver_registry.LanguageResolver(
            CORE_NAME, php, _reporting("skipped", REASON_CORE_UNEXPECTED_SHAPE)
        )
    else:
        guard = resolver_registry.LanguageResolver(
            entry.name, entry.suffixes, _filter(entry.resolve)
        )
    # Every core entry goes, never run unfiltered. The guard runs after our last pass, so the
    # proof is published before it is read: graphify imported first registers core before us.
    for i in reversed(matches):
        del entries[i]
    owned = [i for i, e in enumerate(entries) if getattr(getattr(e, "resolve", None), OWNED, False)]
    entries.insert(owned[-1] + 1 if owned else matches[0], guard)
