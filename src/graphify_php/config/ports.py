"""The one interface a configuration fact has to implement.

A reader is handed a parsed document and returns facts. It does not know where the file came
from, how it was parsed, what a graph node is, or which other readers exist. That is what makes
a third fact — scheduler definitions, workflow transitions — a new class in this package rather
than a branch in an existing one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .facts import ConfigDocument, FactSet


@runtime_checkable
class ConfigFactReader(Protocol):
    name: str

    def read(self, document: ConfigDocument) -> FactSet:
        """Facts this reader finds in one document. A document with none of its fact yields an
        empty `FactSet`, never an exception: most configuration files hold neither routing nor
        an environment reference, and that is not a condition worth reporting."""
