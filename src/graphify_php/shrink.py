"""Attributed-loss shrink guard: decide whether a rebuilt graph may replace the previous one.

A plain node/edge count floor cannot tell a real regression from a legitimate shrink. Deleting a
directory of notes, or graphify dropping degree-0 external stubs, shrinks a graph as much as a
resolver silently failing does. This guard attributes every lost node to the file it came from
and asks why that file lost it: the file is gone, the file changed, or the producer changed what
it indexes. Only loss nothing on disk explains counts against the tolerance.

Stdlib only and free of graphify imports, because graphify-mesh runs it out of process through
`graphify-shrink-check` and this package must stay importable without graphify.

Attribution sidecar
-------------------
Some loss is caused by graphify-php itself and leaves no trace on disk: core edges withheld at a
site graphify-php proved, engine edges a proof contradicts, external stubs switched off. The build
records that loss in `.graphify-php-attribution.json`, next to the status record
(`attribution_path(status.status_path())`). This module owns its schema; the writer imports the
constants below. Schema 1:

    {"schema": 1, "producer": "graphify-php", "external_stubs": "on" | "off",
     "removed_edges": [{"source": str, "target": str, "relation": str,
                        "reason": "core-suppressed-at-proved-site" | "engine-contradicts-proof",
                        "proof": {"source": str, "target": str}}],
     "counts": {"<reason>": int >= 0}}

The guard trusts it only as far as the new graph confirms it. A lost edge is attributed when it
carries no `_resolver` marker, its (source, target, relation) is listed, and the cited proof edge
is in the new graph with the same source and relation and a `_resolver` marker. Each reason is
capped at its count. `external_stubs: "off"` attributes lost stub nodes and their edges only when
the new graph holds no stub at all. A malformed document is ignored whole, and the verdict is
the one without a sidecar; the reason is noted in `reasons` after the verdict is taken.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

POLICY_ATTRIBUTED = "attributed"
POLICY_CONNECTED = "connected"
POLICY_COUNT = "count"
# Computes the count figures for the report and never refuses. Accepted here, not only in the
# build wrapper, so `GRAPHIFY_SHRINK_POLICY=off` means the same thing to every caller.
POLICY_OFF = "off"
POLICIES = (POLICY_ATTRIBUTED, POLICY_CONNECTED, POLICY_COUNT, POLICY_OFF)

DEFAULT_POLICY = POLICY_ATTRIBUTED
DEFAULT_TOLERANCE = 0.10

NONE_BUCKET = "<none>"
FILES_REPORTED = 50

CLASS_DELETED = "deleted"
CLASS_CHANGED = "changed"
CLASS_OUT_OF_CORPUS = "out_of_corpus"
CLASS_UNCHANGED = "unchanged"
# An unchanged file the new producer no longer indexes at all; see classify_loss.
CLASS_DROPPED_BY_EXTRACTOR = "dropped_by_extractor"
CLASS_NONE = "none"

# --- attribution sidecar, schema 1 (see the module docstring) ---
ATTRIBUTION_FILENAME = ".graphify-php-attribution.json"
ATTRIBUTION_SCHEMA = 1
ATTRIBUTION_PRODUCER = "graphify-php"
STUBS_ON = "on"
STUBS_OFF = "off"
REASON_CORE_SUPPRESSED = "core-suppressed-at-proved-site"
REASON_ENGINE_CONTRADICTS = "engine-contradicts-proof"
# The reasons a writer may put in `removed_edges` and `counts`.
REMOVAL_REASONS = (REASON_CORE_SUPPRESSED, REASON_ENGINE_CONTRADICTS)
# The guard's own reason for stub loss under `external_stubs: "off"`; never written by a build.
REASON_STUBS_DISABLED = "stubs-disabled"


@dataclass
class LossReport:
    verdict: str
    policy: str
    tolerance: float
    nodes: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def shrink_floor(old_count: int, tolerance: float) -> int:
    """Smallest surviving count accepted for `old_count`.

    Same arithmetic as graphify-mesh's `_shrink_floor` (sync_project.py), so both sides refuse at
    the same count: rounding up means a tolerance is never widened by truncation, and a tiny base
    gets exact-match semantics.
    """
    if old_count <= 0 or tolerance <= 0.0:
        return old_count
    return math.ceil(old_count * (1.0 - tolerance))


def _below_floor(base: int, loss: int, tolerance: float) -> bool:
    return base - loss < shrink_floor(base, tolerance)


def _nodes(graph: Mapping) -> dict[str, dict]:
    # Node-link JSON normally carries dicts, but a bare id is still a node; it just has no
    # attributes to attribute it by, so it lands in the "<none>" bucket.
    out: dict[str, dict] = {}
    for node in graph.get("nodes") or []:
        if isinstance(node, dict):
            if node.get("id") is not None:
                out[node["id"]] = node
        elif isinstance(node, str | int):
            out[node] = {}
    return out


def _edges(graph: Mapping) -> list[dict]:
    # networkx writes `links` by default and `edges` when asked to; graphify has shipped both.
    edges = graph.get("links") if "links" in graph else graph.get("edges")
    return [e for e in edges or [] if isinstance(e, dict)]


def _edge_key(edge: Mapping) -> tuple:
    return edge.get("source"), edge.get("target"), edge.get("relation")


def _degree(edges: Iterable[Mapping], ids: Iterable[str]) -> Counter:
    degree: Counter = Counter({i: 0 for i in ids})
    for edge in edges:
        degree[edge.get("source")] += 1
        degree[edge.get("target")] += 1
    return degree


def _relative(path: str | None, root: Path) -> str:
    """Repo-relative POSIX form, so graph paths and manifest keys compare equal.

    `root` is already resolved. graphify records whichever spelling of the root it was given, so an
    absolute path through a symlink only matches once its directory is resolved too. The file
    itself is not followed: a symlinked file belongs to where it sits in the repository.
    """
    if not path:
        return NONE_BUCKET
    p = Path(path)
    if p.is_absolute():
        for candidate in (p, p.parent.resolve() / p.name):
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                continue
        # Outside the repository: keep it absolute, and it is still checked on disk as-is.
        return p.as_posix()
    return PurePosixPath(p.as_posix()).as_posix()


def _normalise_manifest(manifest: Mapping, root: Path) -> dict[str, object]:
    return {_relative(k, root): v for k, v in (manifest or {}).items()}


def _file_hash(entry: object) -> object:
    if not isinstance(entry, dict):
        return None
    return entry.get("ast_hash") or entry.get("semantic_hash")


def _classify_file(rel: str, root: Path, old_manifest: Mapping, new_manifest: Mapping) -> str:
    if rel == NONE_BUCKET:
        return CLASS_NONE
    if not (root / rel).exists():
        return CLASS_DELETED
    if rel not in new_manifest:
        return CLASS_OUT_OF_CORPUS
    if rel not in old_manifest:
        return CLASS_CHANGED
    if _file_hash(old_manifest[rel]) != _file_hash(new_manifest[rel]):
        return CLASS_CHANGED
    return CLASS_UNCHANGED


class AttributionError(ValueError):
    """The sidecar does not match schema 1; it is ignored whole."""


@dataclass(frozen=True)
class _Attribution:
    external_stubs: str
    # (source, target, relation) of a lost edge -> [(reason, (proof source, proof target))]
    removed: dict[tuple, list[tuple[str, tuple[str, str]]]]
    counts: dict[str, int]


def attribution_path(status_path: Path | str) -> Path:
    """Where the build writes its attribution sidecar: next to the status record."""
    return Path(status_path).parent / ATTRIBUTION_FILENAME


def read_attribution(path: Path | str) -> tuple[object | None, str | None]:
    """The sidecar's JSON document, or None plus the reason it is unusable.

    Only reads; `classify_loss` validates the shape, so every caller gets the same checks.
    """
    path = Path(path)
    if not path.exists():
        return None, f"attribution sidecar {path} missing; no loss attributed"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"attribution sidecar {path} unreadable ({exc}); no loss attributed"


def _str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttributionError(f"{what} is not a non-empty string")
    return value


def _parse_attribution(data: object) -> _Attribution:
    if not isinstance(data, Mapping):
        raise AttributionError(f"expected a JSON object, got {type(data).__name__}")
    if data.get("schema") != ATTRIBUTION_SCHEMA or isinstance(data.get("schema"), bool):
        raise AttributionError(f"schema {data.get('schema')!r}, expected {ATTRIBUTION_SCHEMA}")
    if data.get("producer") != ATTRIBUTION_PRODUCER:
        raise AttributionError(f"producer {data.get('producer')!r}")
    stubs = data.get("external_stubs")
    if stubs not in (STUBS_ON, STUBS_OFF):
        raise AttributionError(f"external_stubs {stubs!r}")
    counts = data.get("counts")
    if not isinstance(counts, Mapping):
        raise AttributionError("counts is not an object")
    for reason, n in counts.items():
        if reason not in REMOVAL_REASONS:
            raise AttributionError(f"counts names unknown reason {reason!r}")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise AttributionError(f"counts[{reason!r}] is not a non-negative integer")
    entries = data.get("removed_edges")
    if not isinstance(entries, list):
        raise AttributionError("removed_edges is not a list")
    removed: dict[tuple, list[tuple[str, tuple[str, str]]]] = defaultdict(list)
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise AttributionError(f"removed_edges[{i}] is not an object")
        reason = entry.get("reason")
        if reason not in REMOVAL_REASONS:
            raise AttributionError(f"removed_edges[{i}] has unknown reason {reason!r}")
        proof = entry.get("proof")
        if not isinstance(proof, Mapping):
            raise AttributionError(f"removed_edges[{i}].proof is not an object")
        key = tuple(_str(entry.get(k), f"removed_edges[{i}].{k}") for k in ("source", "target"))
        key += (_str(entry.get("relation"), f"removed_edges[{i}].relation"),)
        cited = (
            _str(proof.get("source"), f"removed_edges[{i}].proof.source"),
            _str(proof.get("target"), f"removed_edges[{i}].proof.target"),
        )
        removed[key].append((reason, cited))
    return _Attribution(stubs, dict(removed), {r: int(n) for r, n in counts.items()})


def _is_stub(node: Mapping) -> bool:
    """A body-less external class node graphify-php minted (graph_adapter.GraphStubSink).

    `_resolver` alone also marks the config resolver's file nodes; `external` tells them apart.
    """
    return bool(node.get("_resolver")) and bool(node.get("external"))


def classify_loss(
    old_graph: dict,
    new_graph: dict,
    old_manifest: dict,
    new_manifest: dict,
    root: Path,
    *,
    policy: str = DEFAULT_POLICY,
    tolerance: float = DEFAULT_TOLERANCE,
    producer_changed: bool = False,
    attribution: object | None = None,
) -> LossReport:
    """Compare two builds of the same repository and say whether the new one may publish.

    `producer_changed` is true when graphify or its configuration changed between the builds, so a
    file still on disk may legitimately have left the corpus (a new ignore rule, a dropped file
    type). Without it, such a file losing its nodes is unexplained. It also lets an unchanged file
    lose every node, as when graphify stops indexing data JSON, but only if no old edge left that
    file: a vanished cross-file relation is real knowledge loss whatever the extractor did.

    Edge figures: `lost = explained + unexplained + resolver_lost + attributed_total`, where
    `resolver_lost` counts lost `_resolver` edges whose source survived, whose target survived or
    was a graphify-php stub that vanished, and whose files did not change. An edit that deleted or
    rewrote the edge's file, or the file of its calling node, can remove the call, so that loss is
    explained. In an unchanged file, or one that left the corpus with no producer change, nothing
    on disk accounts for a resolver dropping a call. `attributed` counts, per reason, loss the
    `attribution` sidecar (module docstring) recorded and the new graph confirms.

    Node figures: `lost = orphan + explained + unexplained + attributed_total`. A lost stub node
    belongs to no file; it is explained when every lost edge touching it was explained or
    attributed, and attributed under `stubs-disabled` when stubs were switched off.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown shrink policy {policy!r}; expected one of {', '.join(POLICIES)}")
    if not 0.0 <= tolerance < 1.0:
        raise ValueError(f"shrink tolerance {tolerance!r} outside [0, 1)")
    # Resolved once, so a relative or symlinked root and the absolute paths graphify recorded
    # name the same files.
    root = Path(root).resolve()

    old_nodes, new_nodes = _nodes(old_graph), _nodes(new_graph)
    old_edges, new_edges = _edges(old_graph), _edges(new_graph)
    old_degree = _degree(old_edges, old_nodes)
    lost_ids = [i for i in old_nodes if i not in new_nodes]

    if policy != POLICY_ATTRIBUTED:
        return _counted(policy, tolerance, old_nodes, new_nodes, old_edges, new_edges, lost_ids)

    # Notes on the sidecar go into `reasons` only after the verdict is taken: attribution can
    # only remove loss from the unexplained count, never refuse a build by itself.
    notes: list[str] = []
    parsed: _Attribution | None = None
    if attribution is not None:
        try:
            parsed = _parse_attribution(attribution)
        except AttributionError as exc:
            notes.append(f"attribution ignored: {exc}")

    orphans = {i for i in lost_ids if old_degree[i] == 0 and old_nodes[i].get("external")}
    stub_ids = {i for i, n in old_nodes.items() if _is_stub(n)}
    lost_stubs = {i for i in lost_ids if i in stub_ids and i not in orphans}
    new_stubs = sum(1 for n in new_nodes.values() if _is_stub(n))
    stubs_disabled = False
    if parsed is not None and parsed.external_stubs == STUBS_OFF:
        if new_stubs == 0:
            stubs_disabled = True
        else:
            notes.append(
                f"external_stubs is off but the new graph holds {new_stubs} stub nodes; "
                "stub loss not attributed"
            )
    old_manifest = _normalise_manifest(old_manifest, root)
    new_manifest = _normalise_manifest(new_manifest, root)

    def file_of(node: Mapping) -> str:
        return _relative(node.get("source_file"), root)

    # Orphans are left out of the old per-file count too, otherwise a file that only shed its
    # external stubs would look like it lost real nodes under the unchanged-file count rule.
    # graphify-php's stubs belong to no file; they are judged by their edges further down.
    old_per_file = Counter(
        file_of(n) for i, n in old_nodes.items() if i not in orphans and i not in stub_ids
    )
    new_per_file = Counter(file_of(n) for n in new_nodes.values() if not _is_stub(n))
    lost_per_file: dict[str, list[str]] = defaultdict(list)
    for i in lost_ids:
        if i not in orphans and i not in lost_stubs:
            lost_per_file[file_of(old_nodes[i])].append(i)

    classes: dict[str, str] = {}

    def class_of(rel: str) -> str:
        if rel not in classes:
            classes[rel] = _classify_file(rel, root, old_manifest, new_manifest)
        return classes[rel]

    def self_contained(rel: str, ids: list[str]) -> bool:
        lost = set(ids)
        for edge in old_edges:
            source, target = edge.get("source"), edge.get("target")
            if source in lost or target in lost:
                other = target if source in lost else source
                if file_of(old_nodes.get(other) or {}) != rel:
                    return False
        return True

    explained = unexplained = 0
    explained_ids: set[str] = set(orphans)
    reasons: list[str] = []
    files: list[dict] = []
    for rel, ids in lost_per_file.items():
        cls = class_of(rel)
        old_n, new_n = old_per_file[rel], new_per_file[rel]
        # The new producer stopped indexing this file outright: explained if nothing linked out.
        if (
            cls == CLASS_UNCHANGED
            and producer_changed
            and old_n >= 1
            and new_n == 0
            and self_contained(rel, ids)
        ):
            cls = classes[rel] = CLASS_DROPPED_BY_EXTRACTOR
        if cls in (CLASS_DELETED, CLASS_CHANGED, CLASS_DROPPED_BY_EXTRACTOR) or (
            cls == CLASS_OUT_OF_CORPUS and producer_changed
        ):
            file_loss, file_unexplained = len(ids), 0
        elif cls == CLASS_UNCHANGED:
            # graphify renames ids between versions (`types_lodash` -> `ref_types_lodash`), so
            # for a file whose bytes did not change only a drop in its node count is real loss.
            file_loss = file_unexplained = max(0, old_n - new_n)
        else:
            file_loss = file_unexplained = len(ids)
        unexplained += file_unexplained
        explained += len(ids) - file_unexplained
        if file_unexplained == 0:
            explained_ids.update(ids)
        files.append({"file": rel, "class": cls, "old": old_n, "new": new_n, "lost": file_loss})

        if cls == CLASS_UNCHANGED and old_n >= 1 and new_n == 0:
            reasons.append(f"unchanged file {rel} lost all {old_n} of its nodes")
        elif cls == CLASS_CHANGED and old_n >= 1 and new_n == 0:
            reasons.append(f"changed file {rel} still exists but lost all {old_n} of its nodes")
        elif cls == CLASS_OUT_OF_CORPUS and not producer_changed:
            reasons.append(
                f"{rel} is on disk but left the corpus ({len(ids)} nodes lost) "
                "with no producer change to explain it"
            )

    def edited(rel: str) -> bool:
        cls = class_of(rel)
        return cls in (CLASS_DELETED, CLASS_CHANGED, CLASS_DROPPED_BY_EXTRACTOR) or (
            cls == CLASS_OUT_OF_CORPUS and producer_changed
        )

    def resolver_call_edited(edge: Mapping) -> bool:
        caller = old_nodes.get(edge.get("source")) or {}
        return edited(_relative(edge.get("source_file"), root)) or edited(file_of(caller))

    edge_figures, edge_reasons, stubs_unexplained, edge_notes = _attribute_edges(
        old_edges,
        new_edges,
        new_nodes,
        explained_ids,
        tolerance,
        lambda edge: class_of(_relative(edge.get("source_file"), root)),
        resolver_call_edited,
        lost_stubs,
        stubs_disabled,
        parsed,
    )
    notes.extend(edge_notes)

    node_attributed: Counter = Counter()
    for i in lost_stubs:
        if stubs_disabled:
            node_attributed[REASON_STUBS_DISABLED] += 1
        elif i in stubs_unexplained:
            unexplained += 1
        else:
            explained += 1

    base = sum(1 for i in old_nodes if old_degree[i] >= 1)
    floor = shrink_floor(base, tolerance)
    if base - unexplained < floor:
        reasons.insert(
            0,
            f"unexplained node loss {unexplained} exceeds tolerance {tolerance:g} "
            f"of {base} connected nodes (floor {floor})",
        )
    reasons.extend(edge_reasons)
    verdict = "refuse" if reasons else "accept"
    reasons.extend(notes)

    files.sort(key=lambda f: (-f["lost"], f["file"]))
    return LossReport(
        verdict=verdict,
        policy=policy,
        tolerance=tolerance,
        nodes={
            "old": len(old_nodes),
            "new": len(new_nodes),
            "lost": len(lost_ids),
            "orphan": len(orphans),
            "explained": explained,
            "unexplained": unexplained,
            "base": base,
            "floor": floor,
            "attributed": dict(sorted(node_attributed.items())),
            "attributed_total": sum(node_attributed.values()),
        },
        edges=edge_figures,
        reasons=reasons,
        files=files[:FILES_REPORTED],
    )


def _proved(entries, edge, new_resolver_keys) -> str | None:
    """The reason of the first listed entry whose cited proof the new graph holds."""
    source, relation = edge.get("source"), edge.get("relation")
    for reason, (proof_source, proof_target) in entries:
        if proof_source == source and (proof_source, proof_target, relation) in new_resolver_keys:
            return reason
    return None


def _attribute_edges(
    old_edges,
    new_edges,
    new_nodes,
    explained_ids,
    tolerance,
    class_of,
    resolver_call_edited,
    lost_stubs,
    stubs_disabled,
    attribution,
):
    new_keys = {_edge_key(e) for e in new_edges}
    new_resolver_keys = {_edge_key(e) for e in new_edges if e.get("_resolver")}
    resolver_old = sum(1 for e in old_edges if e.get("_resolver"))
    removed = attribution.removed if attribution is not None else {}
    lost = explained = unexplained = protected = 0
    attributed: Counter = Counter()
    over_cap: Counter = Counter()
    unproved = listed_resolver = 0
    # A lost stub is explained only when every lost edge touching it is explained or attributed.
    stubs_unexplained: set = set()
    for edge in old_edges:
        key = _edge_key(edge)
        if key in new_keys:
            continue
        lost += 1
        source, target = edge.get("source"), edge.get("target")
        ours = bool(edge.get("_resolver"))
        to_lost_stub = target in lost_stubs
        if ours and key in removed:
            listed_resolver += 1
        if ours and to_lost_stub and stubs_disabled:
            attributed[REASON_STUBS_DISABLED] += 1
            continue
        if ours and source in new_nodes and (target in new_nodes or to_lost_stub):
            if resolver_call_edited(edge):
                explained += 1
            else:
                protected += 1
                stubs_unexplained.add(target)
            continue
        if not ours and key in removed:
            reason = _proved(removed[key], edge, new_resolver_keys)
            if reason is None:
                unproved += 1
            elif attributed[reason] < attribution.counts.get(reason, 0):
                attributed[reason] += 1
                continue
            else:
                over_cap[reason] += 1
        if (
            source in explained_ids
            or target in explained_ids
            or class_of(edge) in (CLASS_DELETED, CLASS_CHANGED, CLASS_DROPPED_BY_EXTRACTOR)
        ):
            explained += 1
        else:
            unexplained += 1
            stubs_unexplained.update(x for x in (source, target) if x in lost_stubs)

    reasons = []
    if _below_floor(resolver_old, protected, tolerance):
        reasons.append(
            f"{protected} of {resolver_old} resolver edges lost between surviving nodes "
            f"(tolerance {tolerance:g})"
        )
    if _below_floor(len(old_edges), unexplained, tolerance):
        reasons.append(
            f"unexplained edge loss {unexplained} exceeds tolerance {tolerance:g} "
            f"of {len(old_edges)} edges"
        )
    notes = []
    for reason, n in sorted(over_cap.items()):
        notes.append(
            f"attribution for {reason} capped at its recorded count "
            f"{attribution.counts.get(reason, 0)}; {n} more listed edges not attributed"
        )
    if unproved:
        notes.append(
            f"{unproved} edges listed in the attribution sidecar have no proof in the new graph"
        )
    if listed_resolver:
        notes.append(
            f"{listed_resolver} lost graphify-php edges listed in the attribution sidecar; "
            "ignored, the sidecar never explains the resolver's own edges"
        )
    figures = {
        "old": len(old_edges),
        "new": len(new_edges),
        "lost": lost,
        "explained": explained,
        "unexplained": unexplained,
        "resolver_old": resolver_old,
        "resolver_lost": protected,
        "attributed": dict(sorted(attributed.items())),
        "attributed_total": sum(attributed.values()),
    }
    return figures, reasons, stubs_unexplained, notes


def _counted(policy, tolerance, old_nodes, new_nodes, old_edges, new_edges, lost_ids):
    """The `connected`, `count` and `off` policies: floors on totals, no attribution."""
    if policy == POLICY_CONNECTED:
        old_degree = _degree(old_edges, old_nodes)
        new_degree = _degree(new_edges, new_nodes)
        base = sum(1 for i in old_nodes if old_degree[i] >= 1)
        survived = sum(1 for i in new_nodes if new_degree[i] >= 1)
        what = "connected nodes"
    else:
        base, survived = len(old_nodes), len(new_nodes)
        what = "nodes"
    floor = shrink_floor(base, tolerance)
    edge_floor = shrink_floor(len(old_edges), tolerance)

    reasons = []
    if policy != POLICY_OFF:
        if survived < floor:
            reasons.append(f"{what} {base} -> {survived}, below floor {floor}")
        if len(new_edges) < edge_floor:
            reasons.append(f"edges {len(old_edges)} -> {len(new_edges)}, below floor {edge_floor}")

    new_keys = {_edge_key(e) for e in new_edges}
    lost_edges = sum(1 for e in old_edges if _edge_key(e) not in new_keys)
    return LossReport(
        verdict="refuse" if reasons else "accept",
        policy=policy,
        tolerance=tolerance,
        nodes={
            "old": len(old_nodes),
            "new": len(new_nodes),
            "lost": len(lost_ids),
            "orphan": 0,
            "explained": 0,
            "unexplained": max(0, base - survived),
            "base": base,
            "floor": floor,
            "attributed": {},
            "attributed_total": 0,
        },
        edges={
            "old": len(old_edges),
            "new": len(new_edges),
            "lost": lost_edges,
            "explained": 0,
            "unexplained": max(0, len(old_edges) - len(new_edges)),
            "resolver_old": sum(1 for e in old_edges if e.get("_resolver")),
            "resolver_lost": 0,
            "attributed": {},
            "attributed_total": 0,
        },
        reasons=reasons,
        files=[],
    )
