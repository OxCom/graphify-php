"""Route declarations read from PHP attributes.

Why this is here at all. The call graph answers questions whose subject is a symbol. It cannot
answer one whose subject is a literal: "which repository owns `/api/v1/storage/upload/small`"
has no symbol in it, no node label contains a route path, and the ranked search therefore
returns a frontend caller and an unrelated class above the controller that actually serves it.
A route declaration puts the literal into the graph, attached to the method that serves it.

What a route declaration proves, and what it does not. It proves that this codebase declares
that path and names this controller method to serve it. It does NOT prove that a deployed
ingress, load balancer or reverse proxy routes that path here, nor that any environment loads
the route at all — Symfony filters by `env`, a firewall may block it, and another service may
answer the same path first. A reader will assume the stronger claim, so it is worth saying
plainly: this is a declaration in source, not an observation of production traffic.

The composition rules are read from Symfony's own loader rather than guessed, in the copy at
`vendor/symfony/routing/Loader/AttributeClassLoader.php`:

- path — `$paths[] = $prefix.$path`, plain string concatenation of the class-level path onto
  the method-level one.
- name — `$name = $globals['name'].$name`, the class-level name used as a prefix.
- methods — `array_unique(array_merge($globals['methods'], $attr->methods))`, a UNION. A
  method-level `methods:` does not replace the class-level one, which is the rule most likely
  to be guessed wrong.
- an abstract class is skipped entirely (`load()` throws on it).
- a class with `__invoke` and no method-level route takes its route from the class attribute,
  and `resetGlobals()` means no prefix is applied in that case — the class attribute IS the
  route rather than a prefix for one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import nodes

# Matched on the short name, because that is what the attribute is written as. A `use` that
# renames it is not followed: an alias for `Route` is vanishingly rare next to the cost of
# resolving every attribute name in the file, and a miss here loses a node rather than
# inventing one.
ROUTE_ATTRIBUTE = "Route"

STRING_KINDS = ("string", "encapsed_string")

# Why an effective route could not be composed. Fixed strings, same discipline as the call
# refusals: these are a work list, not a histogram of prose.
PATH_NOT_LITERAL = "path-not-literal"
PREFIX_NOT_LITERAL = "prefix-not-literal"
LOCALIZED_PATH = "localized-path"
NO_PATH = "no-path"


@dataclass(frozen=True)
class RouteDeclaration:
    """One `#[Route]` as declared, with the controller method it sits on.

    `path` is the template verbatim — `/api/v1/storage/upload/small/{id}` — because the
    placeholder is what makes it searchable and matchable. It is None when composition needed
    something this package cannot evaluate; `class_path` and `method_path` still carry
    whichever halves were literal, so the declaration is a usable partial rather than a hole.
    """

    controller_class: str
    controller_method: str
    source_file: str
    line: int
    path: str | None = None
    name: str | None = None
    methods: tuple[str, ...] = ()
    class_path: str | None = None
    method_path: str | None = None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.path is not None

    @property
    def node_key(self) -> str:
        """Identity of this declaration, not of the path.

        The declaring method is in the key on purpose. Two repositories that both declare
        `/health` declare two different things — each is served by its own controller — and
        collapsing them onto one node would claim a relationship between two codebases that
        share nothing but a string.
        """
        return f"{self.controller_class}::{self.controller_method}#{self.path or self.method_path or ''}"


def collect_routes(root, names, source_file: str) -> list[RouteDeclaration]:
    """Every route declared in one file.

    A separate pass over the tree rather than a hook in the walker: routes are not type facts,
    and the walker's one reason to change is scope.
    """
    found: list[RouteDeclaration] = []
    _walk(root, names, source_file, found)
    return found


def _walk(node, names, source_file: str, found: list) -> None:
    if node.type in nodes.CLASS_KINDS:
        _read_class(node, names, source_file, found)
        return
    for child in node.children:
        _walk(child, names, source_file, found)


def _read_class(node, names, source_file: str, found: list) -> None:
    if nodes.child(node, "abstract_modifier") is not None:
        # Symfony's loader throws rather than reading attributes from an abstract class, so a
        # route on one is a prefix for subclasses, never a route in its own right.
        return
    class_fqn = names.qualify_declaration(nodes.name_of(node))
    class_attr = _route_attribute(node)
    prefix = _Prefix.read(class_attr)

    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    methods = nodes.children(body, "method_declaration") if body is not None else []

    before = len(found)
    for method in methods:
        attribute = _route_attribute(method)
        if attribute is None:
            continue
        found.append(_compose(attribute, prefix, class_fqn, nodes.name_of(method), source_file))

    if len(found) == before and class_attr is not None:
        invoke = next((m for m in methods if nodes.name_of(m) == "__invoke"), None)
        if invoke is not None:
            # `resetGlobals()` in the loader: the class attribute is the route itself here, so
            # it composes with nothing.
            found.append(_compose(class_attr, _Prefix(), class_fqn, "__invoke", source_file))


@dataclass
class _Prefix:
    """The class-level attribute, as the three things it contributes."""

    path: str | None = None
    name: str = ""
    methods: tuple[str, ...] = ()
    path_literal: bool = True

    @classmethod
    def read(cls, attribute) -> "_Prefix":
        if attribute is None:
            return cls()
        positional, named = _arguments(attribute)
        path_node = named.get("path") or (positional[0] if positional else None)
        path, literal = _path_of(path_node)
        return cls(
            path=path,
            name=_string_value(named.get("name")) or "",
            methods=_string_list(named.get("methods")),
            path_literal=literal,
        )


def _compose(attribute, prefix: _Prefix, class_fqn: str, method_name: str, source_file: str) -> RouteDeclaration:
    positional, named = _arguments(attribute)
    path_node = named.get("path") or (positional[0] if positional else None)
    method_path, literal = _path_of(path_node)

    name = _string_value(named.get("name"))
    # `array_unique(array_merge(...))`: a union, with the class-level entries first.
    methods = tuple(dict.fromkeys(prefix.methods + _string_list(named.get("methods"))))

    effective: str | None = None
    reason = ""
    if path_node is None:
        reason = NO_PATH
    elif not literal:
        reason = LOCALIZED_PATH if method_path is None and _is_array(path_node) else PATH_NOT_LITERAL
    elif not prefix.path_literal:
        reason = PREFIX_NOT_LITERAL
    else:
        effective = (prefix.path or "") + method_path

    return RouteDeclaration(
        controller_class=class_fqn,
        controller_method=method_name,
        source_file=source_file,
        line=nodes.line_span(attribute)[0],
        path=effective,
        name=(prefix.name + name) if name is not None else (prefix.name or None),
        methods=methods,
        class_path=prefix.path,
        method_path=method_path,
        reason=reason,
    )


def _route_attribute(declaration):
    """The `#[Route(...)]` on a class or method declaration, or None.

    Only attribute groups directly on this declaration are read, so an attribute on a nested
    declaration cannot be mistaken for one on its parent.
    """
    attributes = nodes.child(declaration, "attribute_list")
    if attributes is None:
        return None
    for group in nodes.children(attributes, "attribute_group"):
        for attribute in nodes.children(group, "attribute"):
            written = nodes.node_text(nodes.child(attribute, "name", "qualified_name"))
            if written.rsplit("\\", 1)[-1] == ROUTE_ATTRIBUTE:
                return attribute
    return None


def _arguments(attribute) -> tuple[list, dict]:
    positional: list = []
    named: dict = {}
    argument_list = nodes.child(attribute, "arguments")
    for argument in nodes.children(argument_list, "argument"):
        label = nodes.child(argument, "name")
        # Named nodes only: `name: "x"` puts a bare `:` token between the label and the value,
        # and taking the first non-label child would take the colon.
        value = next((c for c in argument.children if c.is_named and c is not label), None)
        if value is None:
            continue
        if label is not None:
            named[nodes.node_text(label)] = value
        else:
            positional.append(value)
    return positional, named


def _path_of(node) -> tuple[str | None, bool]:
    """(path, was-a-literal). A non-literal keeps `None` rather than a guessed value."""
    if node is None:
        return None, True
    value = _string_value(node)
    if value is None:
        return None, False
    return value, True


def _is_array(node) -> bool:
    return node is not None and node.type == "array_creation_expression"


def _string_value(node) -> str | None:
    """The text of a string literal, or None for anything that needs evaluating.

    A concatenation, a constant or a variable all return None. Reading `self::PREFIX . '/x'`
    as `/x` would put a path in the graph that the application never serves.
    """
    if node is None or node.type not in STRING_KINDS:
        return None
    content = nodes.child(node, "string_content")
    if content is not None:
        return nodes.node_text(content)
    text = nodes.node_text(node)
    return text[1:-1] if len(text) >= 2 and text[0] in "\"'" else text


def _string_list(node) -> tuple[str, ...]:
    """`methods: 'GET'` and `methods: ['GET', 'POST']` are both written in this codebase."""
    if node is None:
        return ()
    single = _string_value(node)
    if single is not None:
        return (single,)
    if not _is_array(node):
        return ()
    out = []
    for element in node.children:
        value = _string_value(element)
        if value is None:
            for inner in element.children:
                value = _string_value(inner)
                if value is not None:
                    break
        if value is not None:
            out.append(value)
    return tuple(out)
