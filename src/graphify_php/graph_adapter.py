"""`NodeIndex` and `EdgeSink` over graphify's in-memory `all_nodes` / `all_edges`.

This module is the only place that knows graphify's node and edge schema. Everything else in
the package talks to the two protocols in `ports`, so a schema move between graphify releases
is repaired here and nowhere else.

The schema this is written against, read from a real build
(`/var/www/knowledge-base/graphify/cem/hub/graph.json`, 6302 nodes / 17886 links, 4012 of them
from `.php` files):

- a class node carries `_callable_class: true` and a `label` that is the SHORT class name
  (`AbstractFrontController`), never the FQN, so an FQN has to be matched down to its last
  segment and then re-checked against the file path;
- a method node hangs off its class by a `method` edge and its label is `.methodName()`;
- a class node hangs off its file node by a `contains` edge;
- `_origin` is `ast` or `semantic` on both nodes and edges.
"""

from __future__ import annotations

import re

from .ports import CallTarget, Confidence

# The marker this package stamps on every edge it adds. graphify's own edges carry `ast` or
# `semantic`, so a reader can select ours, count them, or drop them from a published graph
# without re-running the build.
EDGE_ORIGIN = "graphify-php"

# graphify's own scores for the two confidence levels (extract.py:3534). Matching them keeps
# our edges comparable with graphify's in any consumer that ranks by score.
_CONFIDENCE_SCORE = {Confidence.EXTRACTED: 1.0, Confidence.INFERRED: 0.8}


def _key(label: str) -> str:
    """graphify's own label normalisation (extract.py), reproduced so keys agree.

    `.getCurrentUser()` and `getcurrentuser` collapse to the same key, which is what makes a
    method label from the graph comparable with a method name from PHP source.
    """
    return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()


def _fqn_parts(class_fqn: str) -> list[str]:
    return [p for p in str(class_fqn).replace("/", "\\").split("\\") if p]


def _path_parts(source_file: str) -> list[str]:
    stripped = re.sub(r"\.[^./\\]+$", "", str(source_file or ""))
    return [p for p in stripped.replace("\\", "/").split("/") if p]


def _tail_overlap(a: list[str], b: list[str]) -> int:
    """How many trailing segments the two paths share, compared normalised."""
    count = 0
    for left, right in zip(reversed(a), reversed(b)):
        if _key(left) != _key(right):
            break
        count += 1
    return count


class GraphNodeIndex:
    """Answers "which node is method M of class C" and nothing else.

    Built once per resolver run: the walk is linear in nodes plus edges, and the resolver asks
    this question once per eligible call site, of which the reference application has thousands.
    """

    def __init__(self, all_nodes: list[dict], all_edges: list[dict]) -> None:
        node_by_id: dict[str, dict] = {}
        for node in all_nodes:
            nid = node.get("id")
            if nid:
                node_by_id[nid] = node

        # Only a class a file actually contains is a candidate. graphify also carries nodes
        # that no file contains (synthesised, external, semantic); binding a call to one of
        # those invents a target that has no definition to jump to.
        contained: set[str] = set()
        for edge in all_edges:
            if edge.get("relation") == "contains":
                target = edge.get("target")
                if target:
                    contained.add(target)

        self._classes_by_name: dict[str, list[dict]] = {}
        for node in all_nodes:
            if not node.get("_callable_class") or node.get("id") not in contained:
                continue
            self._classes_by_name.setdefault(_key(node.get("label", "")), []).append(node)

        self._methods: dict[tuple[str, str], str] = {}
        for edge in all_edges:
            if edge.get("relation") != "method":
                continue
            owner, member = edge.get("source"), edge.get("target")
            member_node = node_by_id.get(member)
            if owner and member_node is not None:
                self._methods[(owner, _key(member_node.get("label", "")))] = member

    def method_node(self, class_fqn: str, method: str) -> str | None:
        class_node = self._class_node(class_fqn)
        if class_node is None:
            return None
        return self._methods.get((class_node["id"], _key(method)))

    def _class_node(self, class_fqn: str) -> dict | None:
        parts = _fqn_parts(class_fqn)
        if not parts:
            return None
        namespace, short = parts[:-1], parts[-1]
        candidates = self._classes_by_name.get(_key(short), [])
        if not candidates:
            return None

        # Directories only: the file stem is the class name and matching it against the
        # namespace would score every candidate the same.
        scored = [(_tail_overlap(namespace, _path_parts(c.get("source_file", ""))[:-1]), c)
                  for c in candidates]
        best = max(score for score, _ in scored)
        winners = [c for score, c in scored if score == best]
        if len(winners) != 1:
            # Two project classes share a short name and the namespace did not separate them.
            # Picking either is a coin flip, and a wrong call edge is worse than a missing one.
            return None

        # A namespace of two or more segments must show up in the file path, because a FQN
        # from outside the indexed tree (`Doctrine\ORM\EntityManager`) otherwise binds to
        # whatever project class happens to share its last segment — a unique match, and
        # wrong. A single-segment namespace is exempt: the PSR-4 root (`App\`) maps to the
        # source directory (`src/`) and never appears as a directory itself.
        if len(namespace) >= 2 and best == 0:
            return None
        return winners[0]


class GraphEdgeSink:
    """Appends `calls` edges to graphify's edge list.

    Deduplicates against the pairs already in the graph, the way graphify's own member-call
    resolvers do: the shared cross-file pass may already have connected the same two nodes by
    another route, and a second edge would double that pair's weight in every consumer.
    """

    def __init__(self, all_edges: list[dict]) -> None:
        self._all_edges = all_edges
        self._pairs = {(e.get("source"), e.get("target")) for e in all_edges}
        self.added = 0

    def add_call(self, caller_nid: str, callee_nid: str, target: CallTarget) -> None:
        if (caller_nid, callee_nid) in self._pairs:
            return
        self._pairs.add((caller_nid, callee_nid))
        self._all_edges.append({
            "source": caller_nid,
            "target": callee_nid,
            "relation": "calls",
            "context": "call",
            "confidence": target.confidence.value,
            "confidence_score": _CONFIDENCE_SCORE[target.confidence],
            "source_file": target.site.source_file,
            "source_location": target.site.source_location,
            "weight": 1.0,
            "_origin": EDGE_ORIGIN,
            "_via": target.via,
        })
        self.added += 1
