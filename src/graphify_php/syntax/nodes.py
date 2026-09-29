"""Tree-sitter primitives shared by the collectors.

Kept separate from the collectors so that a collector is about one type source and nothing
else: the moment `type_name` lived next to the walker, every new source meant editing the
walker too.

Nothing here knows what a type source is, and nothing here knows about `ports`. The whole
module is "ask the grammar a question", so it changes when tree-sitter-php changes and for no
other reason.
"""

from __future__ import annotations

try:  # both ship with graphify, so this is reuse rather than a new dependency
    import tree_sitter as ts
    import tree_sitter_php as tsp
except ImportError:  # pragma: no cover - exercised only on a broken install
    ts = None
    tsp = None

# Type positions in the grammar. A declaration carries exactly one of these as a child, so the
# collectors name the set once here instead of repeating it at every declaration site.
TYPE_KINDS = (
    "named_type",
    "union_type",
    "optional_type",
    "primitive_type",
    "intersection_type",
    "disjunctive_normal_form_type",   # `(A&B)|C`, a disjunction whose branches are intersections
)

CLASS_KINDS = (
    "class_declaration",
    "trait_declaration",
    "interface_declaration",
    "enum_declaration",
)

_PARSER = None


def tree_sitter_available() -> tuple[bool, str]:
    if ts is None or tsp is None:
        return False, "tree_sitter and tree_sitter_php are not importable"
    return True, ""


def parser():
    """The one parser instance for this process.

    Reused rather than rebuilt per file: constructing the Language and Parser dominates the
    cost of parsing a single small file, and a build walks every PHP file in the repository.
    """
    global _PARSER
    if _PARSER is None:
        ok, why = tree_sitter_available()
        if not ok:
            raise RuntimeError(why)
        _PARSER = ts.Parser(ts.Language(tsp.language_php()))
    return _PARSER


def parse(source: bytes):
    return parser().parse(source)


def node_text(node) -> str:
    """Source text of a node.

    py-tree-sitter keeps the bytes it parsed on the node, so nothing downstream has to carry
    the buffer alongside every node it handles.
    """
    if node is None:
        return ""
    raw = node.text
    return raw.decode("utf-8", "replace") if raw is not None else ""


def child(node, *kinds):
    """First direct child of any of `kinds`, or None.

    Direct children only. A recursive search would reach into a nested declaration and read a
    type belonging to another scope, which is the mistake this whole package exists to avoid.
    """
    if node is None:
        return None
    for candidate in node.children:
        if candidate.type in kinds:
            return candidate
    return None


def children(node, *kinds):
    if node is None:
        return []
    return [c for c in node.children if c.type in kinds]


def name_of(node) -> str:
    """The declared identifier of a class or method declaration."""
    return node_text(child(node, "name"))


def variable_of(node) -> str:
    """The bare name of a `variable_name` child, with the sigil removed."""
    return node_text(child(node, "variable_name")).lstrip("$")


def line_span(node) -> tuple[int, int]:
    """1-based inclusive line span, matching how every other tool reports a location."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def type_node(node):
    """The type annotation of a declaration, or None when it is untyped."""
    return child(node, *TYPE_KINDS)


def is_union(node) -> bool:
    """Is this type position an alternation — the object is ONE OF several classes?

    `A|B` and `(A&B)|C` both are: there are two candidate targets and nothing in the type says
    which. An intersection is deliberately not here. `A&B` is one object that is both things
    at once, so a method declared in only one constituent is an unambiguous target, and
    `intersection_members` handles it instead.
    """
    if node is None:
        return False
    if node.type in ("union_type", "disjunctive_normal_form_type"):
        return True
    if node.type == "optional_type":
        return any(is_union(c) for c in node.children)
    return False


def intersection_members(node) -> list[str]:
    """The class names of an `A&B` type, or [] when this is not an intersection.

    Only class names are returned: `A&int` is not expressible in PHP, so a member that reads
    as a keyword is a parse this code does not understand and is dropped rather than guessed.
    """
    if node is None:
        return []
    if node.type == "optional_type":
        for child in node.children:
            found = intersection_members(child)
            if found:
                return found
        return []
    if node.type != "intersection_type":
        return []
    names = []
    for child in node.children:
        name = type_name(child)
        if name:
            names.append(name)
    return names


def type_name(node) -> str | None:
    """Return a single class name from a type node, or None when it is not one class.

    Nullable `?Foo` yields `Foo`: null is not an object target, so the call still reaches
    `Foo::method` when it happens at all. A union `Foo|Bar` yields None — both alternatives
    are real targets and this version cannot express two, so it records nothing rather than
    picking one. `array`, `string`, `int` and friends yield None: they own no methods.

    `self`, `static` and `parent` yield None as well. They name a declaration, not the runtime
    class, and treating them as a class is how late static binding turns into a wrong edge.
    """
    if node is None:
        return None
    if node.type in ("union_type", "disjunctive_normal_form_type", "intersection_type", "primitive_type"):
        return None
    if node.type in ("optional_type", "nullable_type"):
        for candidate in node.children:
            found = type_name(candidate)
            if found:
                return found
        return None
    if node.type in ("named_type", "qualified_name", "name"):
        raw = node_text(node).strip().lstrip("?").strip()
        if not raw:
            return None
        # PHP keyword types and relative scopes are all lower case; user classes conventionally
        # are not. The check is on the first character so that an unknown future keyword is
        # refused rather than fabricated into a class.
        if raw[0].islower():
            return None
        return raw
    for candidate in node.children:
        found = type_name(candidate)
        if found:
            return found
    return None
