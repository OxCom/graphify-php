"""Turning facts into graphify nodes and edges, the only module that knows graphify's schema.

Two jobs, both of which every reader would otherwise have to repeat: give a `NodeRef` an id, and
keep the graph free of duplicates. Readers emit the same transport node once per routing entry
on purpose — it is simpler to state the fact than to remember whether it was already stated —
and deduplication here is what makes that safe.

The schema written against, read from a real build: a node carries `id`, `label`, `file_type`,
`source_file`, `source_location` and `_origin`; an edge carries `source`, `target`, `relation`,
`confidence`, `weight` and `_origin`.

`_origin` is not the place for authorship. graphify overwrites it with `"ast"` on every edge
after the resolvers run (extract.py:8285-8289); it means the eviction tier, not the author. The
marker lives in a key of this package's own, which survives.
"""

from __future__ import annotations

from .class_lookup import GraphClassIndex
from .facts import REF_CONFIG, REF_PHP_CLASS, ConfigEdge, ConfigNode, FactSet, NodeRef

EDGE_MARKER = "_resolver"
EDGE_ORIGIN = "graphify-php"

REASON_CLASS_NOT_IN_GRAPH = "message_class_not_in_graph"

# A configuration node describes the running application, not prose about it, so it takes the
# same `file_type` as the code nodes it sits beside. A consumer filtering the graph down to the
# code view would otherwise drop exactly the facts this package exists to add.
FILE_TYPE = "code"

# Configuration facts are read straight out of a declaration, not derived from one. That is
# graphify's definition of EXTRACTED, and using its vocabulary keeps these edges rankable
# alongside its own.
CONFIDENCE = "EXTRACTED"


class GraphWriter:
    """Appends configuration nodes and edges to graphify's lists, once each."""

    def __init__(self, all_nodes: list[dict], all_edges: list[dict],
                 classes: GraphClassIndex | None = None) -> None:
        self._nodes = all_nodes
        self._edges = all_edges
        self._classes = classes if classes is not None else GraphClassIndex(all_nodes, all_edges)
        self._node_ids = {n.get("id") for n in all_nodes}
        self._pairs = {(e.get("source"), e.get("target"), e.get("relation")) for e in all_edges}
        self.nodes_added = 0
        self.edges_added = 0
        self.unresolved: dict[str, int] = {}

    def write(self, facts: FactSet) -> None:
        for node in facts.nodes:
            self._add_node(node)
        for edge in facts.edges:
            self._add_edge(edge)

    def _add_node(self, node: ConfigNode) -> None:
        if node.key in self._node_ids:
            return
        self._node_ids.add(node.key)
        self._nodes.append({
            "id": node.key,
            "label": node.label,
            "file_type": FILE_TYPE,
            "source_file": node.source_file,
            "source_location": node.source_location or "L1",
            "_origin": "ast",
            EDGE_MARKER: EDGE_ORIGIN,
            "_config_kind": node.kind,
        })
        self.nodes_added += 1

    def _add_edge(self, edge: ConfigEdge) -> None:
        source = self._resolve(edge.source)
        target = self._resolve(edge.target)
        if source is None or target is None or source == target:
            return
        key = (source, target, edge.relation)
        if key in self._pairs:
            return
        self._pairs.add(key)
        self._edges.append({
            "source": source,
            "target": target,
            "relation": edge.relation,
            "confidence": CONFIDENCE,
            "source_file": edge.source_file,
            "source_location": edge.source_location or "L1",
            "weight": 1.0,
            EDGE_MARKER: EDGE_ORIGIN,
        })
        self.edges_added += 1

    def _resolve(self, ref: NodeRef) -> str | None:
        if ref.kind == REF_CONFIG:
            # A config endpoint is only usable once its node exists. Facts list their nodes
            # before their edges, so an unknown key here means a reader referenced a node it
            # never emitted, which is a bug in that reader rather than a missing class.
            return ref.value if ref.value in self._node_ids else None
        if ref.kind == REF_PHP_CLASS:
            found = self._classes.node_id(ref.value)
            if found is None:
                # The transport node is already written by the time this runs, so the
                # application's transport list stays complete. What is lost is the one edge,
                # and the count is what says how much of the routing table points outside the
                # indexed tree.
                self.unresolved[REASON_CLASS_NOT_IN_GRAPH] = (
                    self.unresolved.get(REASON_CLASS_NOT_IN_GRAPH, 0) + 1)
            return found
        return None
