"""Turning a source-level type name into a fully qualified one.

Its own module because this is where a resolver fabricates edges. A name written `Emails` in
one file and `Emails` in another is two different classes unless the file's `namespace` and
`use` lines say otherwise, and a resolver that matches on the short name links them anyway.
Six upstream issues across four code-graph projects trace their false edges to exactly that
fallback, so there is no fallback here: a name this file cannot see stays unqualified against
the file's own namespace and simply fails to match anything real.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass
class NameResolver:
    """The `namespace` and `use` lines of one file, and nothing else.

    Deliberately holds no corpus-wide index. Without one it is not possible to answer "is
    there a class called `Emails` somewhere?", which is the question whose answer is always a
    guess.

    PHP scopes `use` lines to the namespace block they are written in, so a file with several
    blocks has several import maps. The walker opens each block with `enter_namespace`; while
    it walks, `namespace` and `imports` are the open block's. After the walk they are the last
    block's, and a reader resolving a name under some node asks `at(node)` for that node's block.
    """

    namespace: str = ""
    imports: dict[str, str] = field(default_factory=dict)  # alias -> FQN
    # (start byte, resolver of that block). Empty until a file opens a namespace.
    blocks: list[tuple[int, NameResolver]] = field(default_factory=list, repr=False)

    def resolve(self, name: str) -> str:
        """PHP's own order: absolute, then alias, then relative to the file's namespace."""
        if not name:
            return name
        if name.startswith("\\"):
            return name.lstrip("\\")
        head, _, tail = name.partition("\\")
        alias = self.imports.get(head)
        if alias is not None:
            return f"{alias}\\{tail}" if tail else alias
        if self.namespace:
            return f"{self.namespace}\\{name}"
        return name

    def qualify_declaration(self, short: str) -> str:
        """The FQN of a class declared in this file."""
        if not short:
            return short
        return f"{self.namespace}\\{short}" if self.namespace else short

    def add_import(self, alias: str, fqn: str) -> None:
        self.imports[alias] = fqn.lstrip("\\")

    def enter_namespace(self, namespace: str, start_byte: int) -> None:
        """Open a namespace block at `start_byte`: its own namespace, and no imports yet."""
        if not self.blocks:
            self.blocks.append((0, NameResolver(self.namespace, self.imports)))
        self.namespace = namespace
        self.imports = {}
        self.blocks.append((start_byte, NameResolver(namespace, self.imports)))

    def current(self) -> NameResolver:
        """The open block's resolver; it keeps seeing imports added to that block later."""
        return self.blocks[-1][1] if self.blocks else self

    def at(self, node) -> NameResolver:
        """The resolver of the namespace block that holds `node`."""
        if not self.blocks:
            return self
        index = bisect_right([start for start, _ in self.blocks], node.start_byte) - 1
        return self.blocks[max(index, 0)][1]
