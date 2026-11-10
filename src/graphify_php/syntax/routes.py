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

import functools
import os
import re
from dataclasses import dataclass

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

# `#[IsGranted]`, matched on the short name for the same reason as `ROUTE_ATTRIBUTE`.
GUARD_ATTRIBUTE = "IsGranted"

# Why an `#[IsGranted]` produced no guard. Fixed string, same discipline as the route reasons.
GUARD_NO_ATTRIBUTE_ARG = "guard-no-attribute-argument"

# Calls that check access by naming an attribute, with whether the caller decides what happens
# on refusal (`isGranted` returns a bool) or the call throws itself (`denyAccessUnlessGranted`).
GRANT_CALLS = {"isGranted": True, "denyAccessUnlessGranted": False}

# Project helper names that check access without naming an attribute. Overridable, because the
# convention is the project's own; a list of regexes, matched with `re.search`.
ACCESS_CHECK_PATTERNS_ENV = "GRAPHIFY_PHP_ACCESS_CHECK_PATTERNS"
DEFAULT_ACCESS_CHECK_PATTERNS = (r"^can[A-Z]", r"^assert\w*Access")

# Why an access check produced no edge, or why the configured patterns were not used.
CHECK_NO_ATTRIBUTE_ARG = "access-check-no-attribute-argument"
CHECK_SPREAD_ARGUMENT = "access-check-spread-argument"
ACCESS_CHECK_PATTERNS_INVALID = "access-check-patterns-invalid"


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
        return (
            f"{self.controller_class}::{self.controller_method}#"
            f"{self.path or self.method_path or ''}"
        )


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
    class_attrs = _route_attributes(node)
    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    # In trait code `self` is the using class, not the trait, so a trait's own constants never
    # resolve `self::NAME`: the empty map refuses every such path as `path-not-literal`.
    constants = {} if node.type == "trait_declaration" else _class_constants(body)
    # `getAttributes(...)[0]` in the loader's `getGlobals()`: only the first class-level Route
    # is the prefix, however many are stacked.
    prefix = _Prefix.read(class_attrs[0] if class_attrs else None, constants)

    methods = nodes.children(body, "method_declaration") if body is not None else []

    before = len(found)
    for method in methods:
        # Every Route on a method is a route of its own (the loader's `foreach`), whether the
        # attributes are stacked in separate groups or listed in one.
        for attribute in _route_attributes(method):
            found.append(
                _compose(
                    attribute, prefix, class_fqn, nodes.name_of(method), source_file, constants
                )
            )

    if len(found) == before and class_attrs:
        invoke = next((m for m in methods if nodes.name_of(m) == "__invoke"), None)
        if invoke is not None:
            # `resetGlobals()` in the loader: each class attribute is a route itself here, so
            # it composes with nothing.
            for class_attr in class_attrs:
                found.append(
                    _compose(class_attr, _Prefix(), class_fqn, "__invoke", source_file, constants)
                )


def _class_constants(body) -> dict[str, str | None]:
    """Constants declared in this class body, by name: the string value, or None.

    None marks a name this package must not resolve: a value that is not a plain string
    literal, or a name declared twice. A name absent from the map (inherited, from an
    interface, from another class) is not resolved either.
    """
    found: dict[str, str | None] = {}
    if body is None:
        return found
    for declaration in nodes.children(body, "const_declaration"):
        for element in nodes.children(declaration, "const_element"):
            name_node = nodes.child(element, "name")
            value_node = next(
                (c for c in element.children if c.is_named and c is not name_node), None
            )
            name = nodes.node_text(name_node)
            found[name] = None if name in found else _string_value(value_node)
    return found


@dataclass(frozen=True)
class AccessGuard:
    """One `#[IsGranted]` as declared on a class or a method.

    `label` is the first argument: the string value when it is a plain literal, otherwise the
    argument's source text verbatim (`new Expression(self::READ_ROLES)`), never evaluated. It
    proves the declaration, not what the security voter decides at runtime.
    """

    holder_class: str
    holder_method: str | None
    source_file: str
    line: int
    label: str
    literal: bool
    subject: str = ""
    reason: str = ""

    @property
    def node_key(self) -> str:
        """A literal names one role wherever it is written, so its node is shared.

        Source text is scoped to the declaring class, because `self::` and an imported short
        name mean something different in another class.
        """
        if self.literal:
            return f"guard#{self.label}"
        return f"{self.holder_class}::guard#{self.label}"


def collect_guards(root, names, source_file: str) -> list[AccessGuard]:
    """Every `#[IsGranted]` on a class or method in one file, abstract classes included.

    Symfony does not inherit class attributes, so a guard is reported on its declaring class
    only and never copied onto subclasses.
    """
    found: list[AccessGuard] = []
    _walk_guards(root, names, source_file, found)
    return found


def _walk_guards(node, names, source_file: str, found: list) -> None:
    if node.type in nodes.CLASS_KINDS:
        _read_class_guards(node, names, source_file, found)
        return
    for child in node.children:
        _walk_guards(child, names, source_file, found)


def _read_class_guards(node, names, source_file: str, found: list) -> None:
    class_fqn = names.qualify_declaration(nodes.name_of(node))
    for attribute in _attributes_named(node, GUARD_ATTRIBUTE):
        found.append(_guard(attribute, class_fqn, None, source_file))
    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    methods = nodes.children(body, "method_declaration") if body is not None else []
    for method in methods:
        for attribute in _attributes_named(method, GUARD_ATTRIBUTE):
            found.append(_guard(attribute, class_fqn, nodes.name_of(method), source_file))


def _guard(attribute, class_fqn: str, method_name: str | None, source_file: str) -> AccessGuard:
    positional, named = _arguments(attribute)
    first = named.get("attribute") or (positional[0] if positional else None)
    subject_node = named.get("subject") or (positional[1] if len(positional) > 1 else None)
    subject = nodes.node_text(subject_node) if subject_node is not None else ""
    line = nodes.line_span(attribute)[0]
    if first is None:
        return AccessGuard(
            class_fqn, method_name, source_file, line, "", False, subject, GUARD_NO_ATTRIBUTE_ARG
        )
    value = _string_value(first)
    if value is not None:
        return AccessGuard(class_fqn, method_name, source_file, line, value, True, subject)
    return AccessGuard(
        class_fqn, method_name, source_file, line, nodes.node_text(first), False, subject
    )


@dataclass(frozen=True)
class AccessCheck:
    """One access-check call written directly in a route handler's body.

    `check_call` is the called method's name. For `isGranted` and `denyAccessUnlessGranted`,
    `label`, `literal` and `subject` are read from the arguments exactly as `AccessGuard` reads
    them from `#[IsGranted]`, so both name one guard node. For a project helper matched by
    pattern (`canView`, `assertTeamAccess`), `label` is the method name. A call proves the
    handler asks; it does not prove what the voter or the helper decides.
    """

    holder_class: str
    holder_method: str
    source_file: str
    line: int
    check_call: str
    label: str
    literal: bool
    subject: str = ""
    reason: str = ""

    @property
    def is_grant_call(self) -> bool:
        return self.check_call in GRANT_CALLS

    @property
    def conditional(self) -> bool | None:
        """True when the caller decides on refusal, False when the call throws, None unknown."""
        return GRANT_CALLS.get(self.check_call)

    @property
    def node_key(self) -> str:
        """The `#[IsGranted]` identity for an attribute check, a class-scoped one for a helper.

        A helper is keyed by the calling class: `$this->canView()` in two controllers calls two
        methods that only share a name.
        """
        if not self.is_grant_call:
            return f"{self.holder_class}::check#{self.check_call}"
        if self.literal:
            return f"guard#{self.label}"
        return f"{self.holder_class}::guard#{self.label}"


def access_check_patterns() -> tuple[tuple[re.Pattern, ...], str]:
    """The helper-name patterns in force, and a fixed reason when the override was refused.

    `GRAPHIFY_PHP_ACCESS_CHECK_PATTERNS` is comma-separated, so a pattern cannot contain a
    comma. An override that is empty or fails to compile falls back to the defaults.
    """
    return _compile_patterns(os.environ.get(ACCESS_CHECK_PATTERNS_ENV) or "")


@functools.lru_cache(maxsize=8)
def _compile_patterns(raw: str) -> tuple[tuple[re.Pattern, ...], str]:
    defaults = tuple(re.compile(p) for p in DEFAULT_ACCESS_CHECK_PATTERNS)
    if not raw:
        return defaults, ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return defaults, ACCESS_CHECK_PATTERNS_INVALID
    try:
        return tuple(re.compile(p) for p in parts), ""
    except re.error:
        return defaults, ACCESS_CHECK_PATTERNS_INVALID


def collect_access_checks(root, names, source_file: str, handlers) -> list[AccessCheck]:
    """Access-check calls on `$this` written directly in the body of a route handler.

    `handlers` is the set of `(class FQN, method)` pairs that carry a `#[Route]`, the invokable
    fallback included. Only `$this->name(...)` and `$this?->name(...)` count, and only where the
    handler's own body runs them: a closure, an arrow function, an anonymous class or a nested
    function is not descended into, because nothing proves the handler runs it. A call in a
    method that is not a handler is not read, even when a handler calls that method.
    """
    found: list[AccessCheck] = []
    if not handlers:
        return found
    patterns, _ = access_check_patterns()
    _walk_checks(root, names, source_file, handlers, patterns, found)
    return found


def _walk_checks(node, names, source_file: str, handlers, patterns, found: list) -> None:
    if node.type in nodes.CLASS_KINDS:
        class_fqn = names.qualify_declaration(nodes.name_of(node))
        body = nodes.child(node, "declaration_list", "enum_declaration_list")
        for method in nodes.children(body, "method_declaration"):
            method_name = nodes.name_of(method)
            if (class_fqn, method_name) not in handlers:
                continue
            statements = nodes.child(method, "compound_statement")
            for call in _direct_this_calls(statements):
                check = _check(call, class_fqn, method_name, source_file, patterns)
                if check is not None:
                    found.append(check)
        return
    for child in node.children:
        _walk_checks(child, names, source_file, handlers, patterns, found)


# Bodies that do not run when the enclosing method runs, or run under another `$this`.
_NOT_THE_HANDLER_BODY = (
    *nodes.CLOSURE_KINDS,
    *nodes.CLASS_KINDS,
    "anonymous_class",
    "function_definition",
)


def _direct_this_calls(node) -> list:
    """`$this->m()` / `$this?->m()` calls under `node`, in source order, skipping nested bodies."""
    out: list = []
    if node is None:
        return out
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _NOT_THE_HANDLER_BODY:
            continue
        if current.type == "object_creation_expression" and nodes.child(
            current, "declaration_list"
        ):
            continue
        if current.type in ("member_call_expression", "nullsafe_member_call_expression"):
            receiver = current.child_by_field_name("object")
            name = current.child_by_field_name("name")
            if (
                receiver is not None
                and receiver.type == "variable_name"
                and nodes.node_text(receiver) == "$this"
                and name is not None
                and name.type == "name"
            ):
                out.append(current)
        stack.extend(reversed(current.children))
    return out


def _check(call, class_fqn: str, method_name: str, source_file: str, patterns):
    called = nodes.node_text(call.child_by_field_name("name"))
    line = nodes.line_span(call)[0]
    if called not in GRANT_CALLS:
        if not any(p.search(called) for p in patterns):
            return None
        return AccessCheck(class_fqn, method_name, source_file, line, called, called, False)

    positional, named = _arguments(call)
    first = named.get("attribute") or (positional[0] if positional else None)
    subject_node = named.get("subject") or (positional[1] if len(positional) > 1 else None)
    if first is None or first.type == "variadic_unpacking":
        reason = CHECK_NO_ATTRIBUTE_ARG if first is None else CHECK_SPREAD_ARGUMENT
        return AccessCheck(class_fqn, method_name, source_file, line, called, "", False, "", reason)
    subject = nodes.node_text(subject_node) if subject_node is not None else ""
    value = _string_value(first)
    if value is not None:
        return AccessCheck(class_fqn, method_name, source_file, line, called, value, True, subject)
    return AccessCheck(
        class_fqn, method_name, source_file, line, called, nodes.node_text(first), False, subject
    )


@dataclass
class _Prefix:
    """The class-level attribute, as the three things it contributes."""

    path: str | None = None
    name: str = ""
    methods: tuple[str, ...] = ()
    path_literal: bool = True

    @classmethod
    def read(cls, attribute, constants: dict[str, str | None] | None = None) -> _Prefix:
        if attribute is None:
            return cls()
        positional, named = _arguments(attribute)
        path_node = named.get("path") or (positional[0] if positional else None)
        path, literal = _path_of(path_node, constants)
        return cls(
            path=path,
            name=_string_value(named.get("name")) or "",
            methods=_string_list(named.get("methods")),
            path_literal=literal,
        )


def _compose(
    attribute,
    prefix: _Prefix,
    class_fqn: str,
    method_name: str,
    source_file: str,
    constants: dict[str, str | None] | None = None,
) -> RouteDeclaration:
    positional, named = _arguments(attribute)
    path_node = named.get("path") or (positional[0] if positional else None)
    method_path, literal = _path_of(path_node, constants)

    name = _string_value(named.get("name"))
    # `array_unique(array_merge(...))`: a union, with the class-level entries first.
    methods = tuple(dict.fromkeys(prefix.methods + _string_list(named.get("methods"))))

    effective: str | None = None
    reason = ""
    if path_node is None:
        reason = NO_PATH
    elif not literal:
        reason = (
            LOCALIZED_PATH if method_path is None and _is_array(path_node) else PATH_NOT_LITERAL
        )
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


def _route_attributes(declaration) -> list:
    """Every `#[Route(...)]` on a class or method declaration, in source order."""
    return _attributes_named(declaration, ROUTE_ATTRIBUTE)


def _attributes_named(declaration, short_name: str) -> list:
    """The attributes written with `short_name` on a declaration, in source order.

    Only attribute groups directly on this declaration are read, so an attribute on a nested
    declaration cannot be mistaken for one on its parent.
    """
    attributes = nodes.child(declaration, "attribute_list")
    if attributes is None:
        return []
    found = []
    for group in nodes.children(attributes, "attribute_group"):
        for attribute in nodes.children(group, "attribute"):
            written = nodes.node_text(nodes.child(attribute, "name", "qualified_name"))
            if written.rsplit("\\", 1)[-1] == short_name:
                found.append(attribute)
    return found


def _arguments(attribute) -> tuple[list, dict]:
    positional: list = []
    named: dict = {}
    argument_list = nodes.child(attribute, "arguments")
    for argument in nodes.children(argument_list, "argument"):
        # The `name` FIELD, not the first `name` child: a bare constant (`#[Route(PREFIX)]`)
        # is a `name` node too, and reading it as a label dropped the argument.
        label = None
        value = None
        for i, c in enumerate(argument.children):
            if argument.field_name_for_child(i) == "name":
                label = c
            elif c.is_named and value is None:
                # Named nodes only: `name: "x"` puts a bare `:` token between the label and
                # the value, and taking the first non-label child would take the colon.
                value = c
        if value is None:
            continue
        if label is not None:
            named[nodes.node_text(label)] = value
        else:
            positional.append(value)
    return positional, named


def _path_of(node, constants: dict[str, str | None] | None = None) -> tuple[str | None, bool]:
    """(path, was-a-literal). A non-literal keeps `None` rather than a guessed value."""
    if node is None:
        return None, True
    value = _path_value(node, constants or {})
    if value is None:
        return None, False
    return value, True


def _path_value(node, constants: dict[str, str | None]) -> str | None:
    """A string literal, `self::NAME` for a constant of this class body, or a `.` of those.

    `self::` names the declaring class, so a constant declared in the same body has exactly the
    value written there. `static::` is late-bound to whichever subclass is called and is
    refused, as is any constant this class body does not declare itself.
    """
    if node is None:
        return None
    if node.type in STRING_KINDS:
        return _string_value(node)
    if node.type == "class_constant_access_expression":
        named = [c for c in node.children if c.is_named]
        if (
            len(named) == 2
            and named[0].type == "relative_scope"
            and nodes.node_text(named[0]).lower() == "self"
            and named[1].type == "name"
        ):
            return constants.get(nodes.node_text(named[1]))
        return None
    if node.type == "binary_expression":
        operators = [c for c in node.children if not c.is_named]
        if len(operators) != 1 or nodes.node_text(operators[0]) != ".":
            return None
        left = _path_value(node.child_by_field_name("left"), constants)
        right = _path_value(node.child_by_field_name("right"), constants)
        if left is None or right is None:
            return None
        return left + right
    return None


def _is_array(node) -> bool:
    return node is not None and node.type == "array_creation_expression"


def _string_value(node) -> str | None:
    """The text of a string literal, or None for anything that needs evaluating.

    A concatenation, a constant or a variable all return None. Reading `self::PREFIX . '/x'`
    as `/x` would put a path in the graph that the application never serves.
    """
    if node is None or node.type not in STRING_KINDS:
        return None
    parts = [c for c in node.children if c.is_named]
    if len(parts) == 1 and parts[0].type == "string_content":
        return nodes.node_text(parts[0])
    if parts:
        # An escape sequence or an interpolation: the value is not the source text, and the
        # first `string_content` of `'/it\'s/{id}'` is `/it`, a path nothing serves.
        return None
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
