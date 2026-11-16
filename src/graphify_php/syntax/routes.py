"""The route pass's records, and the PHP value readers every framework provider shares.

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

Which attributes, files or calls declare a route or a guard is a framework provider's business
(`frameworks/`). Every provider emits the three records below, so the publishing side has one
shape to handle. The composition rules of Symfony's `#[Route]` live in `frameworks/symfony.py`.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import nodes
from .attributes import fold as _fold
from .attributes import resolve_class_name

if TYPE_CHECKING:
    # `access_control` imports this module at runtime.
    from .access_control import PlaceholderFacts

STRING_KINDS = ("string", "encapsed_string")

# Why an effective route could not be composed. Fixed strings, same discipline as the call
# refusals: these are a work list, not a histogram of prose.
PATH_NOT_LITERAL = "path-not-literal"
PREFIX_NOT_LITERAL = "prefix-not-literal"
LOCALIZED_PATH = "localized-path"
NO_PATH = "no-path"

# Why an `#[IsGranted]` produced no guard. Fixed string, same discipline as the route reasons.
GUARD_NO_ATTRIBUTE_ARG = "guard-no-attribute-argument"

# Project helper names that check access without naming an attribute. Overridable, because the
# convention is the project's own; a list of regexes, matched with `re.search`.
ACCESS_CHECK_PATTERNS_ENV = "GRAPHIFY_PHP_ACCESS_CHECK_PATTERNS"
DEFAULT_ACCESS_CHECK_PATTERNS = (r"^can[A-Z]", r"^assert\w*Access")

# Why an access check produced no edge, or why the configured patterns were not used.
CHECK_NO_ATTRIBUTE_ARG = "access-check-no-attribute-argument"
CHECK_SPREAD_ARGUMENT = "access-check-spread-argument"
ACCESS_CHECK_PATTERNS_INVALID = "access-check-patterns-invalid"

# What a guard does. Only `authorization` makes a handler guarded in the coverage counters.
# `public` lets every request through; `unknown` is a guard whose decision cannot be read.
GUARD_KIND_AUTHORIZATION = "authorization"
GUARD_KIND_AUTHENTICATION = "authentication"
GUARD_KIND_PUBLIC = "public"
GUARD_KIND_MIDDLEWARE = "middleware"
GUARD_KIND_UNKNOWN = "unknown"

# `RouteDeclaration.handler_kind`. The route pass also writes "unresolved" on a node whose
# named handler is not in the graph. A provider declares `HANDLER_KIND_UNRESOLVED` for a route
# whose path it proved but whose handler it could not: no lookup, no `handled_by` edge.
HANDLER_KIND_METHOD = "method"
HANDLER_KIND_CLOSURE = "closure"
HANDLER_KIND_UNRESOLVED = "unresolved"

# Why a route's `route_template` is "" or was kept unchecked. Fixed strings.
TEMPLATE_OPTIONAL_LITERAL = "route-template-optional-literal"
TEMPLATE_MULTI_SEGMENT = "route-template-multi-segment"
TEMPLATE_REQUIREMENT_UNREAD = "route-template-requirement-unread"
# A `{` or `}` that is not a placeholder: the template would be ambiguous to a reader.
TEMPLATE_UNPARSED = "route-template-unparsed"

# A normalized placeholder: `{name}`, or `{name?}` when the segment may be absent.
_TEMPLATE_PLACEHOLDER = re.compile(r"\{(\w+)(\?)?\}")

# Providers whose guard node keys predate `guard_kind` and must stay byte-identical. ""
# covers a record built without a provider.
FROZEN_KEY_PROVIDERS = frozenset({"", "symfony"})

# A grant-call receiver: `$this->m()` / `$this?->m()`, or `Fqcn::m()` as `STATIC_RECEIVER` +
# the FQCN, resolved through the file's imports and compared case-insensitively.
THIS_RECEIVER = "this"
STATIC_RECEIVER = "static:"


@dataclass(frozen=True)
class RouteDeclaration:
    """One route as declared, with the controller method it names.

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
    # The provider that declared it, written as `provider` on its node and edge.
    provider: str = ""
    # `_via` on the `handled_by` edge: "attribute", or "route_file" for a routes file.
    via: str = "attribute"
    # The file declaring the handler class when it is not `source_file` (a routes file).
    handler_file: str = ""
    # `route_template` as the provider normalized `path` (see `brace_template`). None: the
    # provider does not normalize, published as "". `template_reason` is a `TEMPLATE_*`
    # reason: with "" it says why, with a template it says what was not checked.
    template: str | None = None
    template_reason: str = ""
    # `access_control.PlaceholderFacts` for the `access_control` matcher; None when the provider
    # does not read them. Never published on a node, and not part of equality.
    placeholder_facts: PlaceholderFacts | None = field(default=None, compare=False, repr=False)
    # `HANDLER_KIND_CLOSURE` when the action is a closure: no handler node exists by design,
    # `controller_class` and `controller_method` are "", and `handler_file` is the repo-relative
    # routes file declaring it.
    handler_kind: str = HANDLER_KIND_METHOD

    @property
    def resolved(self) -> bool:
        return self.path is not None

    @property
    def is_closure(self) -> bool:
        return self.handler_kind == HANDLER_KIND_CLOSURE

    @property
    def has_no_handler(self) -> bool:
        """A closure, or a handler the provider declared unresolved: the pass looks nothing up."""
        return self.handler_kind in (HANDLER_KIND_CLOSURE, HANDLER_KIND_UNRESOLVED)

    @property
    def node_key(self) -> str:
        """Identity of this declaration, not of the path.

        The declaring method is in the key on purpose. Two repositories that both declare
        `/health` declare two different things — each is served by its own controller — and
        collapsing them onto one node would claim a relationship between two codebases that
        share nothing but a string. A closure has no method, so its declaring file and line
        stand in: `<provider>:closure:<file>:<line>#<path>`. A declared-unresolved handler has
        none either: `<provider>:unresolved:<file>:<line>#<path>`.
        """
        if self.has_no_handler:
            return (
                f"{self.provider}:{self.handler_kind}:{self.handler_file}:{self.line}#"
                f"{self.path or self.method_path or ''}"
            )
        return (
            f"{self.controller_class}::{self.controller_method}#"
            f"{self.path or self.method_path or ''}"
        )


@dataclass(frozen=True)
class AccessGuard:
    """One guard declared on a class or a method (`#[IsGranted]`, controller middleware).

    `label` is the guard's argument: the string value when it is a plain literal, otherwise the
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
    provider: str = ""
    # `_via` on the `guarded_by` edge.
    via: str = "attribute"
    # `guard_attribute` on the edge and the node: the attribute's canonical short name, or the
    # provider's name for a non-attribute guard ("middleware", "filter").
    guard_attribute: str = "IsGranted"
    # One of the `GUARD_KIND_*` values. Only `authorization` makes a handler guarded.
    guard_kind: str = "authorization"

    @property
    def node_key(self) -> str:
        """A literal names one role wherever it is written, so its node is shared.

        Source text is scoped to the declaring class, because `self::` and an imported short
        name mean something different in another class. Outside `FROZEN_KEY_PROVIDERS` the
        key is `guard#<provider>:<guard_kind>:<label>` (or `<Holder>::guard#...` for source
        text), so a role and a middleware alias with one text do not merge.
        """
        label = _kind_scoped(self.provider, self.guard_kind, self.label)
        if self.literal:
            return f"guard#{label}"
        return f"{self.holder_class}::guard#{label}"


@dataclass(frozen=True)
class GrantCall:
    """A call that asks the framework's access layer to decide, and where its arguments sit.

    `conditional`: True when the caller decides on refusal (the call returns a bool), False when
    the call throws, None unknown. `label` and `subject` are `(position, name)`: the named
    argument wins, then the positional one; None disables that half.
    """

    conditional: bool | None = None
    label: tuple[int | None, str | None] = (0, "attribute")
    subject: tuple[int | None, str | None] = (1, "subject")


@dataclass(frozen=True)
class AccessCheck:
    """One access-check call written directly in a route handler's body.

    `check_call` is the called method's name (`Gate::authorize` for a static call). For a
    provider's grant call, `label`, `literal` and `subject` are read from the arguments exactly
    as `AccessGuard` reads them from `#[IsGranted]`, so both name one guard node. For a project
    helper matched by pattern (`canView`, `assertTeamAccess`), `label` is the method name. A
    call proves the handler asks; it does not prove what the voter or the helper decides.
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
    provider: str = ""
    via: str = "call"
    guard_kind: str = "authorization"
    grant: bool = False
    grant_conditional: bool | None = None

    @property
    def is_grant_call(self) -> bool:
        return self.grant

    @property
    def conditional(self) -> bool | None:
        """True when the caller decides on refusal, False when the call throws, None unknown."""
        return self.grant_conditional if self.grant else None

    @property
    def node_key(self) -> str:
        """The `#[IsGranted]` identity for a grant call, a class-scoped one for a helper.

        A helper is keyed by the calling class: `$this->canView()` in two controllers calls two
        methods that only share a name.
        """
        if not self.is_grant_call:
            return f"{self.holder_class}::check#{self.check_call}"
        label = _kind_scoped(self.provider, self.guard_kind, self.label)
        if self.literal:
            return f"guard#{label}"
        return f"{self.holder_class}::guard#{label}"


def _kind_scoped(provider: str, guard_kind: str, label: str) -> str:
    if provider in FROZEN_KEY_PROVIDERS:
        return label
    return f"{provider}:{guard_kind}:{label}"


def brace_template(
    path: str, optional: Collection[str] = (), multi_segment: Collection[str] = ()
) -> tuple[str, str]:
    """`(route_template, reason)` for a path already written with `{name}` placeholders.

    `{name?}` is kept as written; a name in `optional` is written `{name?}` too. A name in
    `multi_segment` (its pattern can match `/`) gives `("", TEMPLATE_MULTI_SEGMENT)`, and a
    brace that is not a placeholder gives `("", TEMPLATE_UNPARSED)`. A provider with another
    placeholder syntax rewrites it to this one first.
    """
    out: list[str] = []
    last = 0
    for match in _TEMPLATE_PLACEHOLDER.finditer(path):
        literal = path[last : match.start()]
        if "{" in literal or "}" in literal:
            return "", TEMPLATE_UNPARSED
        name = match.group(1)
        if name in multi_segment:
            return "", TEMPLATE_MULTI_SEGMENT
        out.append(literal)
        out.append(f"{{{name}?}}" if match.group(2) or name in optional else f"{{{name}}}")
        last = match.end()
    tail = path[last:]
    if "{" in tail or "}" in tail:
        return "", TEMPLATE_UNPARSED
    out.append(tail)
    return "/" + "".join(out).lstrip("/"), ""


# What a requirement regex may contain and still never match `/`: word characters and a few
# punctuation marks, the escapes `\d \w \s \h \v`, an escaped non-`/` mark, quantifiers,
# groups and anchors. Anything else (`.`, `\S`, `[^...]`, `\x2f`, `\p{...}`) counts as can-match.
_SAFE_REQUIREMENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-~%,;:=@'\""
)
_SAFE_REQUIREMENT_SYNTAX = frozenset("+*?{}()|^$")
_SAFE_REQUIREMENT_ESCAPES = frozenset("dwshv")
_SAFE_REQUIREMENT_ESCAPED_MARKS = frozenset(".-_~%,;:=@+*?{}()[]|^$#&!<>")


def requirement_may_match_slash(regex: str) -> bool:
    """False only when `regex` provably never matches a `/`; True when unsure.

    A whitelist, not a parser: every construct it does not know makes the answer True.
    """
    i = 0
    in_class = False
    while i < len(regex):
        char = regex[i]
        if char == "\\":
            if i + 1 >= len(regex):
                return True
            escaped = regex[i + 1]
            if escaped not in _SAFE_REQUIREMENT_ESCAPES and (
                escaped not in _SAFE_REQUIREMENT_ESCAPED_MARKS
            ):
                return True
            i += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            elif char == "-":
                # A range is safe only inside one of `0-9`, `a-z`, `A-Z`.
                low, high = regex[i - 1 : i], regex[i + 1 : i + 2]
                if high != "]" and not _same_alnum_run(low, high):
                    return True
            elif char not in _SAFE_REQUIREMENT_CHARS and char != ".":
                return True
            i += 1
            continue
        if char == "[":
            if regex[i + 1 : i + 2] == "^":
                end = _class_end(regex, i + 2)
                # A negated class that excludes `/` never matches it, whatever else it holds.
                if end < 0 or "/" not in regex[i + 2 : end]:
                    return True
                i = end + 1
                continue
            if regex[i + 1 : i + 2] in ("]", ""):
                return True
            in_class = True
        elif char == "(" and regex[i + 1 : i + 2] == "?":
            if regex[i + 2 : i + 3] != ":":
                return True
            i += 3
            continue
        elif char not in _SAFE_REQUIREMENT_CHARS and char not in _SAFE_REQUIREMENT_SYNTAX:
            return True
        i += 1
    return in_class


def _class_end(regex: str, start: int) -> int:
    """Index of the `]` closing a class whose body starts at `start`, or -1."""
    i = start + 1 if regex[start : start + 1] == "]" else start
    while i < len(regex):
        if regex[i] == "\\":
            i += 2
            continue
        if regex[i] == "]":
            return i
        i += 1
    return -1


def _same_alnum_run(low: str, high: str) -> bool:
    return any(
        low.isascii() and high.isascii() and start <= low <= high <= end
        for start, end in (("0", "9"), ("a", "z"), ("A", "Z"))
    )


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


def collect_access_checks(
    root,
    names,
    source_file: str,
    handlers,
    grants: Mapping[tuple[str, str], GrantCall],
    provider: str = "",
) -> list[AccessCheck]:
    """Access-check calls written directly in the body of a route handler.

    `handlers` is the set of `(class FQN, method)` pairs the calling provider declared routes
    for. `grants` is that provider's call specs, keyed `(receiver kind, method name)`:
    `("this", "isGranted")` matches `$this->isGranted(...)` and `$this?->isGranted(...)`;
    `("static:Illuminate\\Support\\Facades\\Gate", "authorize")` matches a static call whose
    class resolves to that FQCN through the file's imports, case-insensitively. Project helpers
    matched by `access_check_patterns` are `$this` calls only. A closure, an arrow function, an
    anonymous class or a nested function is not descended into, because nothing proves the
    handler runs it. A call in a method that is not a handler is not read, even when a handler
    calls it.
    """
    found: list[AccessCheck] = []
    if not handlers:
        return found
    patterns, _ = access_check_patterns()
    member: dict = {}
    static: dict = {}
    for (receiver, method), call in grants.items():
        if receiver == THIS_RECEIVER:
            member[method] = call
        elif receiver.startswith(STATIC_RECEIVER):
            static[(_fold(receiver[len(STATIC_RECEIVER) :]), method)] = call
    table = _Grants(member, static, patterns, provider)
    _walk_checks(root, names, source_file, handlers, table, found)
    return found


@dataclass(frozen=True)
class _Grants:
    member: dict
    static: dict
    patterns: tuple
    provider: str


def _walk_checks(node, names, source_file: str, handlers, table: _Grants, found: list) -> None:
    if node.type in nodes.CLASS_KINDS:
        names = names.at(node)
        class_fqn = names.qualify_declaration(nodes.name_of(node))
        body = nodes.child(node, "declaration_list", "enum_declaration_list")
        for method in nodes.children(body, "method_declaration"):
            method_name = nodes.name_of(method)
            if (class_fqn, method_name) not in handlers:
                continue
            statements = nodes.child(method, "compound_statement")
            for call in _direct_calls(statements, bool(table.static)):
                check = _check(call, class_fqn, method_name, source_file, table, names)
                if check is not None:
                    found.append(check)
        return
    for child in node.children:
        _walk_checks(child, names, source_file, handlers, table, found)


# Bodies that do not run when the enclosing method runs, or run under another `$this`.
_NOT_THE_HANDLER_BODY = (
    *nodes.CLOSURE_KINDS,
    *nodes.CLASS_KINDS,
    "anonymous_class",
    "function_definition",
)


def _direct_calls(node, with_static: bool) -> list:
    """`$this->m()` / `$this?->m()` calls under `node`, and `X::m()` calls when `with_static`.

    In source order, skipping nested bodies.
    """
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
        elif with_static and current.type == "scoped_call_expression":
            scope = current.child_by_field_name("scope")
            name = current.child_by_field_name("name")
            if (
                scope is not None
                and scope.type in ("name", "qualified_name")
                and name is not None
                and name.type == "name"
            ):
                out.append(current)
        stack.extend(reversed(current.children))
    return out


def _check(call, class_fqn: str, method_name: str, source_file: str, table: _Grants, names):
    called = nodes.node_text(call.child_by_field_name("name"))
    line = nodes.line_span(call)[0]
    if call.type == "scoped_call_expression":
        written = nodes.node_text(call.child_by_field_name("scope")).strip()
        grant = table.static.get((_fold(resolve_class_name(written, names)), called))
        if grant is None:
            # The helper patterns name `$this` methods only: a static call is never one.
            return None
        called = f"{written.lstrip(chr(92)).rsplit(chr(92), 1)[-1]}::{called}"
    else:
        grant = table.member.get(called)
    if grant is None:
        if not any(p.search(called) for p in table.patterns):
            return None
        # A project helper's decision is not read, so its kind is unknown.
        return AccessCheck(
            class_fqn, method_name, source_file, line, called, called, False,
            provider=table.provider, guard_kind=GUARD_KIND_UNKNOWN,
        )  # fmt: skip

    def made(label: str, literal: bool, subject: str = "", reason: str = "") -> AccessCheck:
        return AccessCheck(
            class_fqn, method_name, source_file, line, called, label, literal, subject, reason,
            provider=table.provider, grant=True, grant_conditional=grant.conditional,
        )  # fmt: skip

    positional, named = arguments(call)
    first = pick(grant.label, positional, named)
    subject_node = pick(grant.subject, positional, named)
    if first is None or first.type == "variadic_unpacking":
        return made(
            "", False, "", CHECK_NO_ATTRIBUTE_ARG if first is None else CHECK_SPREAD_ARGUMENT
        )
    subject = nodes.node_text(subject_node) if subject_node is not None else ""
    value = string_value(first)
    if value is not None:
        return made(value, True, subject)
    return made(nodes.node_text(first), False, subject)


def pick(spec: tuple[int | None, str | None], positional: list, named: dict):
    """The argument at `(position, name)`: the named one first, then the positional one."""
    position, name = spec
    if name is not None and name in named:
        return named[name]
    if position is not None and 0 <= position < len(positional):
        return positional[position]
    return None


def class_constants(body) -> dict[str, str | None]:
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
            found[name] = None if name in found else string_value(value_node)
    return found


def arguments(call_or_attribute) -> tuple[list, dict]:
    """The `(positional, named)` argument nodes of a call or an attribute."""
    positional: list = []
    named: dict = {}
    argument_list = nodes.child(call_or_attribute, "arguments")
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


def path_of(node, constants: dict[str, str | None] | None = None) -> tuple[str | None, bool]:
    """(path, was-a-literal). A non-literal keeps `None` rather than a guessed value."""
    if node is None:
        return None, True
    value = path_value(node, constants or {})
    if value is None:
        return None, False
    return value, True


def path_value(node, constants: dict[str, str | None]) -> str | None:
    """A string literal, `self::NAME` for a constant of this class body, or a `.` of those.

    `self::` names the declaring class, so a constant declared in the same body has exactly the
    value written there. `static::` is late-bound to whichever subclass is called and is
    refused, as is any constant this class body does not declare itself.
    """
    if node is None:
        return None
    if node.type in STRING_KINDS:
        return string_value(node)
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
        left = path_value(node.child_by_field_name("left"), constants)
        right = path_value(node.child_by_field_name("right"), constants)
        if left is None or right is None:
            return None
        return left + right
    return None


def class_constant_name(node, names) -> str | None:
    """The FQCN of `Foo::class`, resolved through the file's imports, or None for anything else.

    `self::class`, `static::class` and `parent::class` are refused: which class they name
    depends on where the code runs.
    """
    if node is None or node.type != "class_constant_access_expression":
        return None
    named = [c for c in node.children if c.is_named]
    if (
        len(named) != 2
        or named[0].type not in ("name", "qualified_name")
        or nodes.node_text(named[1]).lower() != "class"
    ):
        return None
    return resolve_class_name(nodes.node_text(named[0]).strip(), names.at(node))


def is_array(node) -> bool:
    return node is not None and node.type == "array_creation_expression"


def string_value(node) -> str | None:
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


def literal(node) -> str | None:
    """A string literal's value; single-quoted `\\\\` and `\\'` escapes decoded."""
    value = string_value(node)
    if value is not None or node is None or node.type != "string":
        return value
    parts = [c for c in node.children if c.is_named]
    if not parts or any(p.type not in ("string_content", "escape_sequence") for p in parts):
        return None
    text = ""
    for part in parts:
        raw = nodes.node_text(part)
        if part.type == "escape_sequence":
            if raw not in ("\\\\", "\\'"):
                return None
            raw = raw[1]
        text += raw
    return text


def array_items(node) -> list[tuple[object, object]] | None:
    """`[(key node or None, value node)]` of an array literal; None when it has a spread."""
    if not is_array(node):
        return None
    items: list[tuple[object, object]] = []
    for element in nodes.children(node, "array_element_initializer"):
        named = [c for c in element.children if c.is_named]
        if not named or named[0].type == "variadic_unpacking":
            return None
        if len(named) == 2:
            items.append((named[0], named[1]))
        elif len(named) == 1:
            items.append((None, named[0]))
        else:
            return None
    return items


def strings(node) -> tuple[str, ...] | None:
    """A string, or a list literal of strings only. None when any part is not a literal."""
    single = literal(node)
    if single is not None:
        return (single,)
    items = array_items(node)
    if items is None or any(key is not None for key, _ in items):
        return None
    values = tuple(literal(value) for _, value in items)
    if any(v is None for v in values):
        return None
    return values  # type: ignore[return-value]


def string_list(node) -> tuple[str, ...]:
    """`methods: 'GET'` and `methods: ['GET', 'POST']` are both written in this codebase."""
    if node is None:
        return ()
    single = string_value(node)
    if single is not None:
        return (single,)
    if not is_array(node):
        return ()
    out = []
    for element in node.children:
        value = string_value(element)
        if value is None:
            for inner in element.children:
                value = string_value(inner)
                if value is not None:
                    break
        if value is not None:
            out.append(value)
    return tuple(out)
