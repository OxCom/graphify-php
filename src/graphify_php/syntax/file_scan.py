"""The walker: one pass over one file, driving whatever collectors are registered.

This module owns scope and nothing else. It knows that `namespace` and `use` bind names, that
a class declaration opens a class scope and a method declaration a method scope, and that a
line number inside the file falls in one of them. It does not know what a type source is, and
adding a fifth one must not change a line here — that is why the collectors are a registry
rather than a chain of `elif`s, and why the four that exist are never named in this file.
"""

from __future__ import annotations

from . import nodes
from .call_sites import index_calls
from .collectors import COLLECTORS, ScanContext, by_node_kind
from .facts import ClassScope, FileFacts, MethodScope
from .names import NameResolver
from .routes import collect_routes


def scan_source(path: str, source: bytes, collectors=None) -> FileFacts:
    """Parse one file and return what it proves about its own receivers."""
    tree = nodes.parse(source)
    names = NameResolver()
    facts = FileFacts(path=path, imports=names.imports)
    registry = by_node_kind(collectors if collectors is not None else COLLECTORS)
    _walk(tree.root_node, facts, names, registry, klass=None, method=None)
    facts.namespace = names.namespace
    # A second pass rather than a hook in `_walk`: locating calls is not scope work, and
    # keeping it out means the walker still has one reason to change.
    facts.calls = index_calls(tree.root_node)
    facts.routes = collect_routes(tree.root_node, names, path)
    return facts


def scan_file(path: str, collectors=None) -> FileFacts:
    with open(path, "rb") as handle:
        return scan_source(path, handle.read(), collectors)


def _walk(node, facts: FileFacts, names: NameResolver, registry, klass, method) -> None:
    kind = node.type

    if _is_anonymous_class(node):
        # Its members belong to a class with no name, so attributing them to the enclosing
        # class would invent properties the enclosing class does not have.
        return

    if kind == "namespace_definition":
        _read_namespace(node, names)
    elif kind == "namespace_use_declaration":
        _read_imports(node, names)
    elif kind in nodes.CLASS_KINDS:
        klass = _open_class(node, facts, names)
        method = None
    elif kind == "method_declaration" and klass is not None:
        method = _open_method(node, klass)
    elif kind == "use_declaration" and klass is not None:
        # A trait `use` inside a class body, not a namespace import: the methods it brings in
        # are declared by this class as far as any caller can tell.
        klass.ancestors.extend(_names_in(node, names))

    for collector in registry.get(kind, ()):
        collector.collect(node, ScanContext(facts=facts, names=names, klass=klass, method=method))

    for child in node.children:
        _walk(child, facts, names, registry, klass, method)


def _is_anonymous_class(node) -> bool:
    """`new class { ... }`, which the grammar hangs under an `object_creation_expression`."""
    if node.type == "anonymous_class":
        return True
    return node.type == "object_creation_expression" and nodes.child(node, "declaration_list") is not None


def _read_namespace(node, names: NameResolver) -> None:
    name_node = nodes.child(node, "namespace_name", "qualified_name", "name")
    if name_node is None:
        return
    # A braced `namespace X { ... }` block is read the same way, which is wrong for the rare
    # file holding two of them. Two namespaces in one file is disallowed by PSR-4 and absent
    # from the reference application, so the simpler reading stands until one turns up.
    names.namespace = nodes.node_text(name_node).strip().strip(";").strip().strip("\\")


def _read_imports(node, names: NameResolver) -> None:
    """`use A\\B\\C;` and `use A\\B\\C as D;`.

    Grouped and function/const imports are not read: a grouped `use A\\{B, C};` nests its
    clauses differently, and a function import binds a name that is never a call receiver.
    """
    for clause in nodes.children(node, "namespace_use_clause"):
        target = nodes.child(clause, "qualified_name", "name")
        if target is None:
            continue
        fqn = nodes.node_text(target).strip().lstrip("\\")
        if not fqn:
            continue
        names.add_import(_alias_of(clause) or fqn.rsplit("\\", 1)[-1], fqn)


def _alias_of(clause) -> str:
    """The name `use ... as X` binds, or "" when the clause has no alias.

    The alias is read as the last `name` under the clause rather than the first, because
    `use Foo as Bar;` puts two bare `name` children side by side and the first one is the
    imported class. Both clause shapes are handled: some grammar builds wrap the alias in a
    `namespace_aliasing_clause`, others leave the `as` token flat among the children.
    """
    aliasing = nodes.child(clause, "namespace_aliasing_clause")
    if aliasing is not None:
        return nodes.node_text(nodes.child(aliasing, "name"))
    if nodes.child(clause, "as") is None:
        return ""
    name_children = nodes.children(clause, "name")
    return nodes.node_text(name_children[-1]) if name_children else ""


def _open_class(node, facts: FileFacts, names: NameResolver) -> ClassScope:
    start, end = nodes.line_span(node)
    scope = ClassScope(fqn=names.qualify_declaration(nodes.name_of(node)), start_line=start, end_line=end)
    base = nodes.child(node, "base_clause")
    if base is not None:
        inherited = _names_in(base, names)
        scope.ancestors.extend(inherited)
        if inherited:
            scope.parent = inherited[0]
    implemented = nodes.child(node, "class_interface_clause")
    if implemented is not None:
        # An interface declares the method too, so it answers "does this member exist" even
        # though it never answers "what runs".
        scope.ancestors.extend(_names_in(implemented, names))
    facts.classes.append(scope)
    return scope


def _names_in(node, names: NameResolver) -> list[str]:
    """Every class name listed in a clause, resolved. `extends A, B` and `use T, U` both list."""
    out = []
    for child in nodes.children(node, "name", "qualified_name"):
        text = nodes.node_text(child).strip()
        if text:
            out.append(names.resolve(text))
    return out


def _open_method(node, klass: ClassScope) -> MethodScope:
    start, end = nodes.line_span(node)
    scope = MethodScope(name=nodes.name_of(node), start_line=start, end_line=end)
    klass.methods.append(scope)
    return scope
