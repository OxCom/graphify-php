"""Configuration facts Symfony declares in YAML and nowhere in PHP.

Two of them today: which transport carries which message, and which environment variable a
configuration file reads. Both are invisible to a graph built from PHP source — the first exists
only as a routing key, the second only as a container placeholder — and both are the kind of
question a text search answers with every file and no direction.

The pieces are separate because a third fact is coming: `ConfigFileFinder` decides what to read,
`SafeDocumentParser` turns one file into a mapping or a named refusal, a reader turns one mapping
into facts, and `GraphWriter` turns facts into graph nodes and edges. A new fact is a new reader
registered in `default_readers`; nothing else changes.
"""

from __future__ import annotations

from typing import Sequence

from .discovery import ConfigFileFinder
from .document import DocumentNodeFactory, file_key
from .emit import GraphWriter
from .environment import EnvReferenceReader
from .facts import ConfigDocument, ConfigEdge, ConfigNode, FactSet, NodeRef
from .messaging import MessageRoutingReader
from .ports import ConfigFactReader
from .safe_yaml import SafeDocumentParser

__all__ = [
    "ConfigDocument", "ConfigEdge", "ConfigFactReader", "ConfigFileFinder", "ConfigNode",
    "DocumentNodeFactory", "file_key",
    "EnvReferenceReader", "FactSet", "GraphWriter", "MessageRoutingReader", "NodeRef",
    "SafeDocumentParser", "default_readers",
]


def default_readers() -> Sequence[ConfigFactReader]:
    """The readers a build runs, and the one place they are named."""
    return (MessageRoutingReader(), EnvReferenceReader())
