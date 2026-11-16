"""Matching an attribute's resolved class against a provider's list of FQCNs.

Strict: the class must be listed, or be declared in the repository and extend a listed class
through repository declarations only (Symfony's loader reads attributes with
`IS_INSTANCEOF`, so a subclass of `Route` is a route). A chain that leaves the repository
through an unlisted class is refused with `ATTRIBUTE_PARENT_UNKNOWN`: whether that vendor class
extends a listed one is not known here. Class names compare case-insensitively, as PHP compares
them; the listed spelling is what a match returns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..syntax.attributes import fold

ATTRIBUTE_PARENT_UNKNOWN = "attribute-parent-unknown"


class AttributeMatcher:
    """`match(fqcn) -> (listed FQCN or None, reason)` for one list of FQCNs."""

    def __init__(self, fqcns: Iterable[str], parents: Mapping[str, tuple[str | None, ...]]):
        self._listed = {fold(f): f for f in fqcns}
        self._shorts = frozenset(k.rsplit("\\", 1)[-1] for k in self._listed)
        self._parents = parents
        self._cache: dict[str, tuple[str | None, str]] = {}

    def match(self, fqcn: str) -> tuple[str | None, str]:
        key = fold(fqcn)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._walk(key)
        return hit

    def short_name_listed(self, fqcn: str) -> bool:
        """Whether the class's short name equals a listed FQCN's short name."""
        return fold(fqcn).rsplit("\\", 1)[-1] in self._shorts

    def _walk(self, key: str) -> tuple[str | None, str]:
        if key in self._listed:
            return self._listed[key], ""
        seen = {key}
        current = key
        while True:
            declared = self._parents.get(current)
            if declared is None:
                # The start is not a repository class: an unlisted attribute, no reason. Any
                # later step left the repository through an unlisted class.
                return None, "" if current == key else ATTRIBUTE_PARENT_UNKNOWN
            distinct = {fold(p) if p else None for p in declared}
            if len(distinct) != 1:
                # Two declarations of one class with different parents: which one loads is
                # not known here.
                return None, ATTRIBUTE_PARENT_UNKNOWN
            parent = distinct.pop()
            if parent is None:
                return None, ""
            if parent in self._listed:
                return self._listed[parent], ""
            if parent in seen:
                return None, ""
            seen.add(parent)
            current = parent
