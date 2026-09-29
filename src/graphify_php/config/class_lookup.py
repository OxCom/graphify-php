"""Finding the graph node for a class FQN written in YAML.

graphify labels a class node with its SHORT name — `AbstractCronMessage`, never
`App\\Cron\\Message\\AbstractCronMessage` — so a routing key cannot be looked up directly. The
short name is matched and then re-checked against the file path, because two classes sharing a
short name is ordinary in a Symfony application and binding to the wrong one puts a false edge
in the graph.

A miss is the expected outcome for a message class that lives in a package outside the indexed
tree. It is counted, never guessed at: an edge from the wrong class is worse than no edge, which
is graphify's own bar for its member-call resolvers.

Kept separate from `graph_adapter` deliberately. That module answers "which node is method M of
class C" for call resolution and is another author's file; this one answers a different question
for a different pass and has no reason to make them move together.
"""

from __future__ import annotations

import re


def _key(label: str) -> str:
    """graphify's own label normalisation, so keys made here agree with labels made there."""
    return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()


def _fqn_parts(fqn: str) -> list[str]:
    return [p for p in str(fqn).replace("/", "\\").split("\\") if p]


def _path_parts(source_file: str) -> list[str]:
    stripped = re.sub(r"\.[^./\\]+$", "", str(source_file or ""))
    return [p for p in stripped.replace("\\", "/").split("/") if p]


def _tail_overlap(a: list[str], b: list[str]) -> int:
    count = 0
    for left, right in zip(reversed(a), reversed(b)):
        if _key(left) != _key(right):
            break
        count += 1
    return count


class GraphClassIndex:
    """Class FQN -> graphify node id, or nothing."""

    def __init__(self, all_nodes: list[dict], all_edges: list[dict]) -> None:
        # Only a class some file contains is a candidate. graphify also carries synthesised and
        # external class nodes; an edge into one of those points at nothing a reader can open.
        contained = {e.get("target") for e in all_edges if e.get("relation") == "contains"}
        self._by_short: dict[str, list[dict]] = {}
        for node in all_nodes:
            if node.get("_callable_class") and node.get("id") in contained:
                self._by_short.setdefault(_key(node.get("label", "")), []).append(node)

    def node_id(self, class_fqn: str) -> str | None:
        parts = _fqn_parts(class_fqn)
        if not parts:
            return None
        candidates = self._by_short.get(_key(parts[-1]), [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0].get("id")

        # Directories only. The file stem repeats the class name, so including it would score
        # every candidate identically and decide nothing.
        namespace = parts[:-1]
        scored = [(_tail_overlap(namespace, _path_parts(c.get("source_file", ""))[:-1]), c)
                  for c in candidates]
        best = max(score for score, _ in scored)
        winners = [c for score, c in scored if score == best]
        if best == 0 or len(winners) != 1:
            return None
        return winners[0].get("id")
