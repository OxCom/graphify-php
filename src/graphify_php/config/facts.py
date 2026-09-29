"""What a reader returns, and what the writer turns into graph nodes and edges.

The whole package is shaped around this one dataclass set so that a third configuration fact
— a scheduler definition, a workflow transition — is a new reader class and nothing else. A
reader never touches graphify's node schema and the writer never learns what a transport is,
so neither of them grows a branch per fact kind.

An endpoint is a `NodeRef` rather than a node id because a fact knows what it means, not what
the graph calls it. `config` means "a node this package emits, keyed by `value`"; `php_class`
means "the class graphify already indexed under this FQN, if it did". Resolution is the
writer's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A node this package emits, addressed by the key the reader chose.
REF_CONFIG = "config"
# A class node graphify already made. May not exist: a message class outside the indexed tree
# is the normal case, not an error.
REF_PHP_CLASS = "php_class"


@dataclass(frozen=True)
class NodeRef:
    kind: str
    value: str

    @staticmethod
    def config(key: str) -> "NodeRef":
        return NodeRef(REF_CONFIG, key)

    @staticmethod
    def php_class(fqn: str) -> "NodeRef":
        return NodeRef(REF_PHP_CLASS, fqn)


@dataclass(frozen=True)
class ConfigNode:
    """A node to add to the graph if it is not already there.

    `key` is stable across builds and across files: two `messenger.yaml` files routing to
    `cron` describe one transport, and emitting it twice would split a consumer's view of who
    sends to it.
    """

    key: str
    label: str
    kind: str                 # config_file | transport | env_var
    source_file: str = ""
    source_location: str = ""


@dataclass(frozen=True)
class ConfigEdge:
    source: NodeRef
    target: NodeRef
    relation: str
    source_file: str = ""
    source_location: str = ""


@dataclass
class FactSet:
    """One reader's answer for one document.

    `unresolved` carries the reader's own reason vocabulary. The resolver prefixes it with the
    reader name, so two readers may use the same word without merging into one meaningless
    count.
    """

    nodes: list[ConfigNode] = field(default_factory=list)
    edges: list[ConfigEdge] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)

    def unresolvable(self, reason: str, count: int = 1) -> None:
        self.unresolved[reason] = self.unresolved.get(reason, 0) + count

    def extend(self, other: "FactSet") -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        for reason, count in other.unresolved.items():
            self.unresolvable(reason, count)


@dataclass(frozen=True)
class ConfigDocument:
    """One parsed YAML document, with the path it came from.

    `path` is relative to the repository root, because it becomes a node label and an absolute
    build path would make the same file produce a different node on another machine — the
    graph is published and compared across machines.
    """

    path: str
    data: dict
