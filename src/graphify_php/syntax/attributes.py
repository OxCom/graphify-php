"""PHP attributes as facts, with no framework in sight.

Every `#[...]` on a class, method, property, parameter, class constant or function, with its
name resolved to a fully-qualified class name through the file's `namespace` and `use` lines.
What an attribute MEANS is a framework provider's business (`frameworks/`); this module only
says which class it names. A name the file does not import resolves against the file's own
namespace and so matches no provider's FQCN: there is no short-name fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import nodes

TARGET_CLASS = "class"
TARGET_METHOD = "method"
TARGET_PROPERTY = "property"
TARGET_PARAMETER = "parameter"
TARGET_CLASS_CONSTANT = "class_constant"
TARGET_FUNCTION = "function"

_PARAMETER_KINDS = ("simple_parameter", "property_promotion_parameter", "variadic_parameter")


@dataclass(frozen=True)
class AttributeArg:
    """One argument as written: the source text, and the value when it is a plain string."""

    text: str
    value: str | None


@dataclass(frozen=True)
class AttributeFact:
    """One attribute. `member` is the method, property, constant or function name, or ""."""

    target: str
    class_fqn: str
    member: str
    short: str
    fqcn: str
    positional: tuple[AttributeArg, ...]
    named: tuple[tuple[str, AttributeArg], ...]
    line: int


def attribute_fqcn(attribute, names) -> str:
    """The FQCN an `attribute` node names, resolved the way PHP resolves a class name."""
    written = nodes.node_text(nodes.child(attribute, "name", "qualified_name")).strip()
    return resolve_class_name(written, names.at(attribute))


def resolve_class_name(written: str, names) -> str:
    """`NameResolver.resolve`, with the alias matched case-insensitively, as PHP matches it.

    `#[isGranted]` under `use ...\\IsGranted;` names that class. The result keeps the import's
    spelling; compare it with `fold`. Member-call resolution keeps its own, exact lookup.
    """
    if not written or written.startswith("\\"):
        return names.resolve(written)
    head, _, tail = written.partition("\\")
    folded = head.casefold()
    for alias, fqn in names.imports.items():
        if alias.casefold() == folded:
            return f"{fqn}\\{tail}" if tail else fqn
    return names.resolve(written)


def fold(fqcn: str) -> str:
    """The comparison key of a class name: PHP class names are case-insensitive."""
    return fqcn.strip().lstrip("\\").casefold()


def declared_attributes(declaration) -> list:
    """The attribute nodes written directly on a declaration, in source order.

    Only attribute groups directly on this declaration are read, so an attribute on a nested
    declaration cannot be mistaken for one on its parent.
    """
    attributes = nodes.child(declaration, "attribute_list")
    if attributes is None:
        return []
    found = []
    for group in nodes.children(attributes, "attribute_group"):
        found.extend(nodes.children(group, "attribute"))
    return found


def collect_attributes(root, names, path: str) -> list[AttributeFact]:
    """Every attribute in one file, in source order. `path` is accepted for symmetry only."""
    found: list[AttributeFact] = []
    _walk(root, names, found)
    return found


def _walk(node, names, found: list) -> None:
    if node.type in nodes.CLASS_KINDS:
        _read_class(node, names.at(node), found)
        return
    if node.type == "function_definition":
        member = nodes.name_of(node)
        scoped = names.at(node)
        _add(node, TARGET_FUNCTION, "", member, scoped, found)
        _parameters(node, "", member, scoped, found)
        return
    for child in node.children:
        _walk(child, names, found)


def _read_class(node, names, found: list) -> None:
    class_fqn = names.qualify_declaration(nodes.name_of(node))
    _add(node, TARGET_CLASS, class_fqn, "", names, found)
    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    if body is None:
        return
    for member in body.children:
        if member.type == "method_declaration":
            name = nodes.name_of(member)
            _add(member, TARGET_METHOD, class_fqn, name, names, found)
            _parameters(member, class_fqn, name, names, found)
        elif member.type == "property_declaration":
            element = nodes.child(member, "property_element")
            variable = nodes.child(element, "variable_name") if element is not None else None
            name = nodes.node_text(variable).lstrip("$") if variable is not None else ""
            _add(member, TARGET_PROPERTY, class_fqn, name, names, found)
        elif member.type == "const_declaration":
            element = nodes.child(member, "const_element")
            name = nodes.node_text(nodes.child(element, "name")) if element is not None else ""
            _add(member, TARGET_CLASS_CONSTANT, class_fqn, name, names, found)


def _parameters(declaration, class_fqn: str, method: str, names, found: list) -> None:
    parameters = nodes.child(declaration, "formal_parameters")
    for parameter in nodes.children(parameters, *_PARAMETER_KINDS):
        variable = nodes.child(parameter, "variable_name")
        name = nodes.node_text(variable).lstrip("$") if variable is not None else ""
        member = f"{method}(${name})"
        _add(parameter, TARGET_PARAMETER, class_fqn, member, names, found)


def _add(declaration, target: str, class_fqn: str, member: str, names, found: list) -> None:
    for attribute in declared_attributes(declaration):
        fqcn = attribute_fqcn(attribute, names)
        positional, named = _arguments(attribute)
        found.append(
            AttributeFact(
                target=target,
                class_fqn=class_fqn,
                member=member,
                short=fqcn.rsplit("\\", 1)[-1],
                fqcn=fqcn,
                positional=positional,
                named=named,
                line=nodes.line_span(attribute)[0],
            )
        )


def _arguments(attribute) -> tuple[tuple[AttributeArg, ...], tuple[tuple[str, AttributeArg], ...]]:
    positional: list[AttributeArg] = []
    named: list[tuple[str, AttributeArg]] = []
    for argument in nodes.children(nodes.child(attribute, "arguments"), "argument"):
        label = None
        value = None
        for i, c in enumerate(argument.children):
            if argument.field_name_for_child(i) == "name":
                label = c
            elif c.is_named and value is None:
                value = c
        if value is None:
            continue
        arg = AttributeArg(nodes.node_text(value), _plain_string(value))
        if label is not None:
            named.append((nodes.node_text(label), arg))
        else:
            positional.append(arg)
    return tuple(positional), tuple(named)


def _plain_string(node) -> str | None:
    if node.type not in ("string", "encapsed_string"):
        return None
    parts = [c for c in node.children if c.is_named]
    if len(parts) == 1 and parts[0].type == "string_content":
        return nodes.node_text(parts[0])
    if parts:
        return None
    text = nodes.node_text(node)
    return text[1:-1] if len(text) >= 2 and text[0] in "\"'" else text
