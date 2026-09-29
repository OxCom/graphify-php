"""The node standing for a configuration file, and the key every reader addresses it by.

The file node belongs to no single reader: routing edges and environment edges both start at it,
and whichever reader emitted it would silently own a node the other depends on. It is emitted by
the pass instead, and only for a document that produced at least one fact — a Symfony `config/`
holds dozens of files about caching, routing and security that this package indexes nothing
from, and a node per file would bury the two facts it does index.

The key is the path relative to the repository root. Build paths are absolute while a build runs
and relativized only at publish time, so keying on the absolute path would make the same file
produce a different node on another machine.
"""

from __future__ import annotations

from .facts import ConfigNode


def file_key(path: str) -> str:
    return f"cfg::file::{path}"


class DocumentNodeFactory:
    """Builds the node for one configuration file."""

    def node(self, path: str) -> ConfigNode:
        return ConfigNode(key=file_key(path), label=path.rsplit("/", 1)[-1],
                          kind="config_file", source_file=path)
