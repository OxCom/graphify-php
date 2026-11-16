"""The walker: one pass over one file, driving whatever collectors are registered.

This module owns scope and nothing else. It knows that `namespace` and `use` bind names, that
a class declaration opens a class scope and a method declaration a method scope, and that a
line number inside the file falls in one of them. It does not know what a type source is, and
adding a fifth one must not change a line here — that is why the collectors are a registry
rather than a chain of `elif`s, and why the four that exist are never named in this file.
"""

from __future__ import annotations

from . import nodes
from .attributes import collect_attributes
from .call_sites import index_calls
from .collectors import COLLECTORS, ScanContext, by_node_kind
from .facts import ClassScope, FileFacts, MethodScope
from .names import NameResolver


def scan_source(path: str, source: bytes, collectors=None) -> FileFacts:
    """Parse one file and return what it proves about its own receivers."""
    return parse_source(path, source, collectors)[2]


def parse_source(path: str, source: bytes, collectors=None):
    """`scan_source`, also returning the tree's root and the file's names: `(root, names, facts)`.

    For a reader that walks the tree itself (a route provider). The root keeps the whole tree
    alive, so a caller holding many should hold `FileFacts` instead.
    """
    tree = nodes.parse(source)
    names = NameResolver()
    facts = FileFacts(path=path)
    registry = by_node_kind(collectors if collectors is not None else COLLECTORS)
    _walk(tree.root_node, facts, names, registry, klass=None, method=None)
    # The last block's: the whole file's in a single-namespace file. A reader that needs the
    # block of one class asks `ClassScope.names` instead.
    facts.namespace = names.namespace
    facts.imports = names.imports
    # A second pass rather than a hook in `_walk`: locating calls is not scope work, and
    # keeping it out means the walker still has one reason to change.
    facts.calls = index_calls(tree.root_node)
    facts.attributes = collect_attributes(tree.root_node, names, path)
    return tree.root_node, names, facts


def scan_file(path: str, collectors=None) -> FileFacts:
    with open(path, "rb") as handle:
        return scan_source(path, handle.read(), collectors)


def _walk(node, facts: FileFacts, names: NameResolver, registry, klass, method) -> None:
    kind = node.type

    if _is_anonymous_class(node):
        # Its members belong to a class with no name, so attributing them to the enclosing
        # class would invent properties the enclosing class does not have. The span is kept
        # so a call site inside it is refused rather than read against the enclosing class.
        facts.anonymous_classes.append(nodes.line_span(node))
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

    if kind == "namespace_definition" and nodes.child(node, "compound_statement") is not None:
        # Code after a braced block is global code, and the block's imports end with it.
        names.enter_namespace("", node.end_byte)


def _is_anonymous_class(node) -> bool:
    """`new class { ... }`, which the grammar hangs under an `object_creation_expression`."""
    if node.type == "anonymous_class":
        return True
    return (
        node.type == "object_creation_expression"
        and nodes.child(node, "declaration_list") is not None
    )


def _read_namespace(node, names: NameResolver) -> None:
    """Open the block a `namespace` statement starts, braced or not, named or global.

    Each block starts with no imports: PHP scopes a `use` line to the block it is written in,
    so an import in one block must not resolve a name in the next.
    """
    name_node = nodes.child(node, "namespace_name", "qualified_name", "name")
    namespace = ""
    if name_node is not None:
        namespace = nodes.node_text(name_node).strip().strip(";").strip().strip("\\")
    names.enter_namespace(namespace, node.start_byte)


def _read_imports(node, names: NameResolver) -> None:
    """`use A\\B\\C;`, `use A\\B\\C as D;` and the grouped `use A\\{B, C\\E as D};`.

    A grouped clause is read as the group prefix joined to the clause's own name. A
    `function` or `const` import inside a group, or a whole grouped `use function`, is
    skipped: it binds a name that is never a class. An ungrouped one is read as before.
    """
    group = nodes.child(node, "namespace_use_group")
    if group is not None:
        if nodes.child(node, "function", "const") is not None:
            return
        prefix_node = nodes.child(node, "namespace_name", "qualified_name", "name")
        prefix = nodes.node_text(prefix_node).strip().strip("\\") if prefix_node else ""
        for clause in nodes.children(group, "namespace_use_clause"):
            if nodes.child(clause, "function", "const") is not None:
                continue
            target = nodes.child(clause, "qualified_name", "name")
            if target is None:
                continue
            tail = nodes.node_text(target).strip().strip("\\")
            if not tail:
                continue
            fqn = f"{prefix}\\{tail}" if prefix else tail
            names.add_import(_alias_of(clause) or fqn.rsplit("\\", 1)[-1], fqn)
        return
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
    scope = ClassScope(
        fqn=names.qualify_declaration(nodes.name_of(node)),
        start_line=start,
        end_line=end,
        names=names.current(),
    )
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
