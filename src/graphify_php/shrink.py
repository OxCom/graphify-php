"""Attributed-loss shrink guard: decide whether a rebuilt graph may replace the previous one.

A plain node/edge count floor cannot tell a real regression from a legitimate shrink. Deleting a
directory of notes, or graphify dropping degree-0 external stubs, shrinks a graph as much as a
resolver silently failing does. This guard attributes every lost node to the file it came from
and asks why that file lost it: the file is gone, the file changed, or the producer changed what
it indexes. Only loss nothing on disk explains counts against the tolerance.

Stdlib only and free of graphify imports, because graphify-mesh runs it out of process through
`graphify-shrink-check` and this package must stay importable without graphify.
"""

from __future__ import annotations

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
) -> LossReport:
    """Compare two builds of the same repository and say whether the new one may publish.

    `producer_changed` is true when graphify or its configuration changed between the builds, so a
    file still on disk may legitimately have left the corpus (a new ignore rule, a dropped file
    type). Without it, such a file losing its nodes is unexplained. It also lets an unchanged file
    lose every node, as when graphify stops indexing data JSON, but only if no old edge left that
    file: a vanished cross-file relation is real knowledge loss whatever the extractor did.

    Edge figures: `lost = explained + unexplained + resolver_lost`, where `resolver_lost` counts
    lost `_resolver` edges whose endpoints both survived and whose files did not change. An edit
    that deleted or rewrote the edge's file, or the file of its calling node, can remove the call,
    so that loss is explained. In an unchanged file, or one that left the corpus with no producer
    change, nothing on disk accounts for a resolver dropping a call between two surviving nodes.
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

    orphans = {i for i in lost_ids if old_degree[i] == 0 and old_nodes[i].get("external")}
    old_manifest = _normalise_manifest(old_manifest, root)
    new_manifest = _normalise_manifest(new_manifest, root)

    def file_of(node: Mapping) -> str:
        return _relative(node.get("source_file"), root)

    # Orphans are left out of the old per-file count too, otherwise a file that only shed its
    # external stubs would look like it lost real nodes under the unchanged-file count rule.
    old_per_file = Counter(file_of(n) for i, n in old_nodes.items() if i not in orphans)
    new_per_file = Counter(file_of(n) for n in new_nodes.values())
    lost_per_file: dict[str, list[str]] = defaultdict(list)
    for i in lost_ids:
        if i not in orphans:
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

    base = sum(1 for i in old_nodes if old_degree[i] >= 1)
    floor = shrink_floor(base, tolerance)
    if base - unexplained < floor:
        reasons.insert(
            0,
            f"unexplained node loss {unexplained} exceeds tolerance {tolerance:g} "
            f"of {base} connected nodes (floor {floor})",
        )

    def edited(rel: str) -> bool:
        cls = class_of(rel)
        return cls in (CLASS_DELETED, CLASS_CHANGED, CLASS_DROPPED_BY_EXTRACTOR) or (
            cls == CLASS_OUT_OF_CORPUS and producer_changed
        )

    def resolver_call_edited(edge: Mapping) -> bool:
        caller = old_nodes.get(edge.get("source")) or {}
        return edited(_relative(edge.get("source_file"), root)) or edited(file_of(caller))

    edge_figures, edge_reasons = _attribute_edges(
        old_edges,
        new_edges,
        new_nodes,
        explained_ids,
        tolerance,
        lambda edge: class_of(_relative(edge.get("source_file"), root)),
        resolver_call_edited,
    )
    reasons.extend(edge_reasons)

    files.sort(key=lambda f: (-f["lost"], f["file"]))
    return LossReport(
        verdict="refuse" if reasons else "accept",
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
        },
        edges=edge_figures,
        reasons=reasons,
        files=files[:FILES_REPORTED],
    )


def _attribute_edges(
    old_edges, new_edges, new_nodes, explained_ids, tolerance, class_of, resolver_call_edited
):
    new_keys = {_edge_key(e) for e in new_edges}
    resolver_old = sum(1 for e in old_edges if e.get("_resolver"))
    lost = explained = unexplained = protected = 0
    for edge in old_edges:
        if _edge_key(edge) in new_keys:
            continue
        lost += 1
        source, target = edge.get("source"), edge.get("target")
        if edge.get("_resolver") and source in new_nodes and target in new_nodes:
            if resolver_call_edited(edge):
                explained += 1
            else:
                protected += 1
        elif (
            source in explained_ids
            or target in explained_ids
            or class_of(edge) in (CLASS_DELETED, CLASS_CHANGED, CLASS_DROPPED_BY_EXTRACTOR)
        ):
            explained += 1
        else:
            unexplained += 1

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
    figures = {
        "old": len(old_edges),
        "new": len(new_edges),
        "lost": lost,
        "explained": explained,
        "unexplained": unexplained,
        "resolver_old": resolver_old,
        "resolver_lost": protected,
    }
    return figures, reasons


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
        },
        edges={
            "old": len(old_edges),
            "new": len(new_edges),
            "lost": lost_edges,
            "explained": 0,
            "unexplained": max(0, len(old_edges) - len(new_edges)),
            "resolver_old": sum(1 for e in old_edges if e.get("_resolver")),
            "resolver_lost": 0,
        },
        reasons=reasons,
        files=[],
    )
