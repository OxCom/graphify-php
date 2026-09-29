"""Turning a source-level type name into a fully qualified one.

Its own module because this is where a resolver fabricates edges. A name written `Emails` in
one file and `Emails` in another is two different classes unless the file's `namespace` and
`use` lines say otherwise, and a resolver that matches on the short name links them anyway.
Six upstream issues across four code-graph projects trace their false edges to exactly that
fallback, so there is no fallback here: a name this file cannot see stays unqualified against
the file's own namespace and simply fails to match anything real.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NameResolver:
    """The `namespace` and `use` lines of one file, and nothing else.

    Deliberately holds no corpus-wide index. Without one it is not possible to answer "is
    there a class called `Emails` somewhere?", which is the question whose answer is always a
    guess.
    """

    namespace: str = ""
    imports: dict[str, str] = field(default_factory=dict)   # alias -> FQN

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
