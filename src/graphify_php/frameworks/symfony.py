"""Symfony: `#[Route]`, `#[IsGranted]`, the controller grant calls and `security.access_control`.

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

The loader reads attributes with `IS_INSTANCEOF`, so a repository subclass of `Route` or
`IsGranted` counts (`matching.AttributeMatcher`). Node keys and every published field are
frozen: they predate providers, and the graph built from them is diffed against a golden file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from ..syntax import access_control, nodes
from ..syntax.attributes import attribute_fqcn, declared_attributes
from ..syntax.routes import (
    GUARD_KIND_AUTHENTICATION,
    GUARD_KIND_AUTHORIZATION,
    GUARD_KIND_PUBLIC,
    GUARD_KIND_UNKNOWN,
    GUARD_NO_ATTRIBUTE_ARG,
    LOCALIZED_PATH,
    NO_PATH,
    PATH_NOT_LITERAL,
    PREFIX_NOT_LITERAL,
    TEMPLATE_REQUIREMENT_UNREAD,
    THIS_RECEIVER,
    AccessGuard,
    GrantCall,
    RouteDeclaration,
    arguments,
    brace_template,
    class_constants,
    collect_access_checks,
    is_array,
    path_of,
    path_value,
    requirement_may_match_slash,
    string_list,
    string_value,
)
from .matching import ATTRIBUTE_PARENT_UNKNOWN, AttributeMatcher
from .provider import (
    AUTHORIZATION_ACCESS_CONTROL_BLOCKED,
    AUTHORIZATION_ACCESS_CONTROL_UNDECIDED,
    ProviderContext,
    ProviderResult,
    RouteGuard,
)

NAME = "symfony"

ROUTE_FQCNS = (
    "Symfony\\Component\\Routing\\Attribute\\Route",
    "Symfony\\Component\\Routing\\Annotation\\Route",
)
IS_GRANTED_FQCNS = (
    "Symfony\\Component\\Security\\Http\\Attribute\\IsGranted",
    "Sensio\\Bundle\\FrameworkExtraBundle\\Configuration\\IsGranted",
)
# `guard_attribute` of an `#[IsGranted]` guard: the canonical short name, whatever the spelling.
IS_GRANTED = "IsGranted"

# Calls that check access by naming an attribute, with whether the caller decides what happens
# on refusal (`isGranted` returns a bool) or the call throws itself (`denyAccessUnlessGranted`).
GRANT_CALLS = {
    (THIS_RECEIVER, "isGranted"): GrantCall(conditional=True),
    (THIS_RECEIVER, "denyAccessUnlessGranted"): GrantCall(conditional=False),
}

# `guard_attribute` and `_via` of an `access_control` guard, as published before providers.
ACCESS_CONTROL = "access_control"

# `AuthenticatedVoter`'s attributes. A request passes on ANY attribute of a rule (the default
# `affirmative` strategy), so one public or authentication-only attribute decides the kind.
PUBLIC_ROLES = frozenset({"PUBLIC_ACCESS", "IS_AUTHENTICATED_ANONYMOUSLY"})
AUTHENTICATION_ROLES = frozenset(
    {
        "IS_AUTHENTICATED_FULLY",
        "IS_AUTHENTICATED_REMEMBERED",
        "IS_AUTHENTICATED",
        "IS_REMEMBERED",
        "IS_IMPERSONATOR",
    }
)
# Weakest first: the kind of "A or B" is the weaker of the two, and unknown loses only to public.
_KIND_ORDER = (
    GUARD_KIND_PUBLIC,
    GUARD_KIND_UNKNOWN,
    GUARD_KIND_AUTHENTICATION,
    GUARD_KIND_AUTHORIZATION,
)
_IS_GRANTED_CALL = re.compile(r"""is_granted\(\s*(['"])([^'"]*)\1\s*\)""")
_OPERATOR = re.compile(r"\|\||&&|\b(?:and|or)\b", re.IGNORECASE)

# Symfony's inline placeholder: `{!name:mapped.prop<requirement>?default}`, every part optional
# but the name (`Route::extractInlineDefaultsAndRequirements`).
_SYMFONY_PLACEHOLDER = re.compile(r"\{(!?)(\w+)(?::\w+(?:\.\w+)?)?(<.*?>)?(\?[^}]*)?\}")
# `RouteCompiler::SEPARATORS`: one of these before a placeholder belongs to the placeholder.
_SEPARATORS = "/,;.:-_~+*=@|"


def role_kind(role: str) -> str:
    """The guard kind of one role or attribute name."""
    if role in PUBLIC_ROLES:
        return GUARD_KIND_PUBLIC
    if role in AUTHENTICATION_ROLES:
        return GUARD_KIND_AUTHENTICATION
    return GUARD_KIND_AUTHORIZATION


def weakest_kind(kinds: Iterable[str]) -> str:
    """The kind of a check that passes when any one of `kinds` passes; public for none."""
    found = set(kinds)
    return next((k for k in _KIND_ORDER if k in found), GUARD_KIND_PUBLIC)


def expression_kind(expression: str) -> str:
    """authorization when `allow_if` provably requires a role; unknown otherwise.

    Proved when there is no top-level `or`/`||` and one top-level `and`/`&&` conjunct is exactly
    a positive `is_granted('X')`, X not an `AuthenticatedVoter` attribute.
    """
    text = _unwrap(expression)
    outside = _depth_zero(text)
    if outside is None:
        return GUARD_KIND_UNKNOWN
    parts: list[str] = []
    start = 0
    for i in outside:
        if i < start:
            continue
        match = _OPERATOR.match(text, i)
        if match is None:
            continue
        if match.group(0).lower() in ("or", "||"):
            return GUARD_KIND_UNKNOWN
        parts.append(text[start:i])
        start = match.end()
    parts.append(text[start:])
    for part in parts:
        call = _IS_GRANTED_CALL.fullmatch(_unwrap(part))
        if call and role_kind(call.group(2)) == GUARD_KIND_AUTHORIZATION:
            return GUARD_KIND_AUTHORIZATION
    return GUARD_KIND_UNKNOWN


def _depth_zero(text: str) -> list[int] | None:
    """Indices of `text` outside quotes and parens; None when a quote or a paren is unbalanced."""
    found: list[int] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        else:
            if depth == 0:
                found.append(i)
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return None
        i += 1
    return None if quote or depth else found


def _unwrap(text: str) -> str:
    """`text` stripped, without the paren pairs that enclose all of it."""
    text = text.strip()
    while text.startswith("(") and _depth_zero(text) == [0]:
        text = text[1:-1].strip()
    return text


def access_rule_kind(roles: Iterable[str], allow_if: str | None) -> str:
    """The guard kind of one `access_control` rule.

    No attributes at all lets the request through (`AccessListener` returns without deciding).
    `roles` and `allow_if` together are attributes of one decision, so either one passing is
    enough and the weaker kind wins.
    """
    kinds = [role_kind(r) for r in roles]
    if allow_if is not None:
        kinds.append(expression_kind(allow_if))
    return weakest_kind(kinds)


def label_kind(label: str, literal: bool) -> str:
    """The kind of an `#[IsGranted]` or a grant call: a literal is a role, anything else unknown."""
    return role_kind(label) if literal and label else GUARD_KIND_UNKNOWN


class SymfonyProvider:
    name = NAME
    packages = (
        "symfony/framework-bundle",
        "symfony/security-bundle",
        "symfony/routing",
        "symfony/http-kernel",
    )

    def collect(self, ctx: ProviderContext) -> ProviderResult:
        out = ProviderResult()
        parents = ctx.class_parents()
        route_match = AttributeMatcher(ROUTE_FQCNS, parents)
        guard_match = AttributeMatcher(IS_GRANTED_FQCNS, parents)
        for path in ctx.files:
            facts = ctx.facts(path)
            if facts is None or not _wanted(facts.attributes, route_match, guard_match, out):
                continue
            parsed = ctx.parse(path)
            if parsed is None:
                continue
            routes = collect_routes(parsed.root, parsed.names, path, route_match)
            guards = collect_guards(parsed.root, parsed.names, path, guard_match)
            handlers = {(r.controller_class, r.controller_method) for r in routes}
            checks = collect_access_checks(
                parsed.root, parsed.names, path, handlers, GRANT_CALLS, NAME
            )
            for bucket, records in ((out.routes, routes), (out.guards, guards)):
                for record in records:
                    bucket.append(record)
                    if record.reason:
                        out.count(record.reason)
            for route in routes:
                if route.template_reason:
                    out.count(route.template_reason)
            for check in checks:
                if check.is_grant_call:
                    check = replace(check, guard_kind=label_kind(check.label, check.literal))
                out.checks.append(check)
                if check.reason:
                    out.count(check.reason)
        _access_control(ctx.repo_root, out)
        return out


PROVIDER = SymfonyProvider()


def scan(path: str, source: bytes) -> ProviderResult:
    """The provider run over one in-memory file: what that file alone declares."""
    return PROVIDER.collect(ProviderContext.from_sources({path: source}))


def _wanted(attributes, route_match, guard_match, out: ProviderResult) -> bool:
    """Count this file's attributes; True when any of them is a Route or an IsGranted."""
    wanted = False
    for attribute in attributes:
        route, route_why = route_match.match(attribute.fqcn)
        guard, guard_why = guard_match.match(attribute.fqcn)
        if route or guard:
            out.attributes_mapped += 1
            wanted = True
        elif route_why or guard_why:
            out.count(ATTRIBUTE_PARENT_UNKNOWN)
        elif route_match.short_name_listed(attribute.fqcn) or guard_match.short_name_listed(
            attribute.fqcn
        ):
            out.attributes_short_name_only += 1
    return wanted


def _access_control(repo_root: str, out: ProviderResult) -> None:
    """Edge each route to the `access_control` rule proved to apply to all its URLs.

    One declaration per route node, the first, as the pass publishes them. A repository with
    no `config/packages/security.yaml` gets no guards and no reasons. A route this step cannot
    decide is recorded in `out.route_undecided`: its authorization state is then undecided.
    """
    routes: dict[str, RouteDeclaration] = {}
    for declaration in out.routes:
        if declaration.path or declaration.method_path:
            routes.setdefault(declaration.node_key, declaration)
    rules, why = access_control.load_rules(repo_root)
    if why:
        out.count_pass(why)
    if rules is None:
        if why:
            # `security.yaml` exists but was not read: any route may carry a rule.
            for key in routes:
                out.route_undecided[key] = AUTHORIZATION_ACCESS_CONTROL_BLOCKED
        return
    blocker = access_control.config_blocker(repo_root)
    if blocker:
        # The rules or the route paths read here may not be the ones served: no edge at all,
        # one count per route, as for an undecided route.
        out.count_pass(blocker, len(routes))
        for key in routes:
            out.route_undecided[key] = AUTHORIZATION_ACCESS_CONTROL_BLOCKED
        return
    facts_blocked = access_control.segment_facts_blocked(repo_root)
    if facts_blocked and routes:
        out.count_pass(access_control.REASON_SEGMENT_FACTS_UNREAD)
    for key, declaration in routes.items():
        if declaration.path is None:
            out.count_pass(access_control.REASON_NO_PATH)
            # An unread prefix or path: which rule matches is not known.
            out.route_undecided[key] = AUTHORIZATION_ACCESS_CONTROL_BLOCKED
            continue
        rule, reason = access_control.first_rule(
            declaration.path, rules, None if facts_blocked else declaration.placeholder_facts
        )
        if rule is None:
            out.count_pass(reason)
            if reason == access_control.REASON_UNDECIDED:
                out.undecided += 1
                out.route_undecided[key] = AUTHORIZATION_ACCESS_CONTROL_UNDECIDED
            continue
        out.route_guards.append(
            RouteGuard(
                route_key=declaration.node_key,
                node_key=rule.node_key,
                label=rule.label,
                guard_attribute=ACCESS_CONTROL,
                via=ACCESS_CONTROL,
                source_file=access_control.SECURITY_YAML,
                provider=NAME,
                guard_kind=access_rule_kind(rule.roles, rule.allow_if),
                extra_node_fields=(
                    ("access_control_path", rule.path if isinstance(rule.path, str) else ""),
                    ("access_control_index", rule.index),
                ),
            )
        )


def _matching(declaration, names, matcher: AttributeMatcher) -> list:
    """The attributes on a declaration that `matcher` maps, in source order."""
    return [
        a for a in declared_attributes(declaration) if matcher.match(attribute_fqcn(a, names))[0]
    ]


def collect_routes(root, names, source_file: str, matcher: AttributeMatcher) -> list:
    """Every route declared in one file.

    A separate pass over the tree rather than a hook in the walker: routes are not type facts,
    and the walker's one reason to change is scope.
    """
    found: list[RouteDeclaration] = []
    _walk(root, names, source_file, found, matcher)
    return found


def _walk(node, names, source_file: str, found: list, matcher) -> None:
    if node.type in nodes.CLASS_KINDS:
        _read_class(node, names, source_file, found, matcher)
        return
    for child in node.children:
        _walk(child, names, source_file, found, matcher)


def _read_class(node, names, source_file: str, found: list, matcher) -> None:
    if nodes.child(node, "abstract_modifier") is not None:
        # Symfony's loader throws rather than reading attributes from an abstract class, so a
        # route on one is a prefix for subclasses, never a route in its own right.
        return
    names = names.at(node)
    class_fqn = names.qualify_declaration(nodes.name_of(node))
    class_attrs = _matching(node, names, matcher)
    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    # In trait code `self` is the using class, not the trait, so a trait's own constants never
    # resolve `self::NAME`: the empty map refuses every such path as `path-not-literal`.
    constants = {} if node.type == "trait_declaration" else class_constants(body)
    arrays = {} if node.type == "trait_declaration" else _array_constants(body)
    # `getAttributes(...)[0]` in the loader's `getGlobals()`: only the first class-level Route
    # is the prefix, however many are stacked.
    prefix = _Prefix.read(class_attrs[0] if class_attrs else None, constants, arrays)

    methods = nodes.children(body, "method_declaration") if body is not None else []

    before = len(found)
    for method in methods:
        # Every Route on a method is a route of its own (the loader's `foreach`), whether the
        # attributes are stacked in separate groups or listed in one.
        for attribute in _matching(method, names, matcher):
            found.append(
                _compose(
                    attribute,
                    prefix,
                    class_fqn,
                    nodes.name_of(method),
                    source_file,
                    constants,
                    method,
                    arrays,
                )
            )

    if len(found) == before and class_attrs:
        invoke = next((m for m in methods if nodes.name_of(m) == "__invoke"), None)
        if invoke is not None:
            # `resetGlobals()` in the loader: each class attribute is a route itself here, so
            # it composes with nothing.
            for class_attr in class_attrs:
                found.append(
                    _compose(
                        class_attr,
                        _Prefix(),
                        class_fqn,
                        "__invoke",
                        source_file,
                        constants,
                        invoke,
                        arrays,
                    )
                )


def collect_guards(root, names, source_file: str, matcher: AttributeMatcher) -> list:
    """Every `#[IsGranted]` on a class or method in one file, abstract classes included.

    Symfony does not inherit class attributes, so a guard is reported on its declaring class
    only and never copied onto subclasses.
    """
    found: list[AccessGuard] = []
    _walk_guards(root, names, source_file, found, matcher)
    return found


def _walk_guards(node, names, source_file: str, found: list, matcher) -> None:
    if node.type in nodes.CLASS_KINDS:
        _read_class_guards(node, names, source_file, found, matcher)
        return
    for child in node.children:
        _walk_guards(child, names, source_file, found, matcher)


def _read_class_guards(node, names, source_file: str, found: list, matcher) -> None:
    names = names.at(node)
    class_fqn = names.qualify_declaration(nodes.name_of(node))
    for attribute in _matching(node, names, matcher):
        found.append(_guard(attribute, class_fqn, None, source_file))
    body = nodes.child(node, "declaration_list", "enum_declaration_list")
    methods = nodes.children(body, "method_declaration") if body is not None else []
    for method in methods:
        for attribute in _matching(method, names, matcher):
            found.append(_guard(attribute, class_fqn, nodes.name_of(method), source_file))


def _guard(attribute, class_fqn: str, method_name: str | None, source_file: str) -> AccessGuard:
    positional, named = arguments(attribute)
    first = named.get("attribute") or (positional[0] if positional else None)
    subject_node = named.get("subject") or (positional[1] if len(positional) > 1 else None)
    subject = nodes.node_text(subject_node) if subject_node is not None else ""
    line = nodes.line_span(attribute)[0]

    def made(label: str, literal: bool, reason: str = "") -> AccessGuard:
        return AccessGuard(
            class_fqn, method_name, source_file, line, label, literal, subject, reason,
            provider=NAME, guard_attribute=IS_GRANTED, guard_kind=label_kind(label, literal),
        )  # fmt: skip

    if first is None:
        return made("", False, GUARD_NO_ATTRIBUTE_ARG)
    value = string_value(first)
    if value is not None:
        return made(value, True)
    return made(nodes.node_text(first), False)


@dataclass
class _Prefix:
    """The class-level attribute, as the things it contributes."""

    path: str | None = None
    name: str = ""
    methods: tuple[str, ...] = ()
    path_literal: bool = True
    # `requirements:` and `defaults:` keys, which the loader merges under the method's own.
    requirements: tuple[tuple[str, str | None], ...] = ()
    requirements_unread: bool = False
    defaults: tuple[str, ...] = ()
    defaults_unread: bool = False

    @classmethod
    def read(
        cls,
        attribute,
        constants: dict[str, str | None] | None = None,
        arrays: dict[str, Any] | None = None,
    ) -> _Prefix:
        if attribute is None:
            return cls()
        positional, named = arguments(attribute)
        path_node = named.get("path") or (positional[0] if positional else None)
        path, literal = path_of(path_node, constants)
        requirements, unread = _requirements(named.get("requirements"), constants, arrays)
        return cls(
            path=path,
            name=string_value(named.get("name")) or "",
            methods=string_list(named.get("methods")),
            path_literal=literal,
            requirements=requirements,
            requirements_unread=unread,
            defaults=_array_keys(named.get("defaults")),
            defaults_unread=_array_entries(named.get("defaults"))[1],
        )


def _array_entries(node) -> tuple[list[tuple[str, object]], bool]:
    """`'key' => value` entries of an array literal, and True when one could not be read.

    No array at all is `([], False)`; a value that is not an array literal, or an entry without
    a literal key, is unread.
    """
    if node is None:
        return [], False
    if not is_array(node):
        return [], True
    entries: list[tuple[str, object]] = []
    unread = False
    for element in node.children:
        if element.type != "array_element_initializer":
            continue
        parts = [c for c in element.children if c.is_named]
        key = string_value(parts[0]) if len(parts) == 2 else None
        if key is None:
            unread = True
            continue
        entries.append((key, parts[1]))
    return entries, unread


def _requirements(
    node,
    constants: dict[str, str | None] | None = None,
    arrays: dict[str, Any] | None = None,
) -> tuple[tuple[tuple[str, str | None], ...], bool]:
    """`requirements:` as `(name, regex)` pairs, None for a regex that is not a literal.

    A regex reads like a route path: a literal, `self::NAME` for a string constant of this
    class body, or a `.` of those (`path_value`). Anything else stays None. The whole array may
    be `self::NAME` for an array constant of this class body, and an inline array may spread
    one (`_constant_array`); an unreadable one is unread as a whole.
    """
    constants = constants or {}
    arrays = arrays or {}
    name = _self_name(node)
    if name is not None:
        whole = _constant_array(name, constants, arrays, frozenset())
        return ((), True) if whole is None else (tuple(whole.items()), False)
    if not is_array(node):
        return (), _array_entries(node)[1]
    # PHP array semantics: the last write wins and the key keeps its first position.
    pairs: dict[str, str | None] = {}
    unread = False
    for element in node.children:
        if element.type != "array_element_initializer":
            continue
        parts = [c for c in element.children if c.is_named]
        if len(parts) == 1 and parts[0].type == "variadic_unpacking":
            spread = _spread(parts[0], constants, arrays, frozenset())
            if spread is None:
                unread = True
            else:
                pairs.update(spread)
            continue
        key = string_value(parts[0]) if len(parts) == 2 else None
        if key is None:
            unread = True
            continue
        pairs[key] = path_value(parts[1], constants)
    return tuple(pairs.items()), unread


def _self_name(node) -> str | None:
    """NAME of `self::NAME`; None for `static::`, `parent::`, another class or anything else."""
    if node is None or node.type != "class_constant_access_expression":
        return None
    named = [c for c in node.children if c.is_named]
    if (
        len(named) == 2
        and named[0].type == "relative_scope"
        and nodes.node_text(named[0]).lower() == "self"
        and named[1].type == "name"
    ):
        return nodes.node_text(named[1])
    return None


def _array_constants(body) -> dict[str, Any]:
    """The value node of each constant declared in this class body; None when declared twice.

    A name absent from the map (inherited, from an interface, from another class) is not read.
    """
    found: dict[str, Any] = {}
    if body is None:
        return found
    for declaration in nodes.children(body, "const_declaration"):
        for element in nodes.children(declaration, "const_element"):
            name_node = nodes.child(element, "name")
            value_node = next(
                (c for c in element.children if c.is_named and c is not name_node), None
            )
            name = nodes.node_text(name_node)
            found[name] = None if name in found else value_node
    return found


def _constant_array(
    name: str,
    constants: dict[str, str | None],
    arrays: dict[str, Any],
    seen: frozenset[str],
) -> dict[str, str] | None:
    """The `requirements` read from array constant `name`, or None when any part is unread.

    Every element is a literal string key with a `path_value` regex, or a spread of another
    array constant of this body read by the same rules. A cycle, a non-string key or one
    unreadable value makes the whole array unread, never just that key.
    """
    value = arrays.get(name)
    if name in seen or value is None or not is_array(value):
        return None
    out: dict[str, str] = {}
    for element in value.children:
        if element.type != "array_element_initializer":
            continue
        parts = [c for c in element.children if c.is_named]
        if len(parts) == 1 and parts[0].type == "variadic_unpacking":
            spread = _spread(parts[0], constants, arrays, seen | {name})
            if spread is None:
                return None
            out.update(spread)
            continue
        key = string_value(parts[0]) if len(parts) == 2 else None
        regex = path_value(parts[1], constants) if key is not None else None
        if key is None or regex is None:
            return None
        out[key] = regex
    return out


def _spread(
    node,
    constants: dict[str, str | None],
    arrays: dict[str, Any],
    seen: frozenset[str],
) -> dict[str, str] | None:
    """`...self::NAME` as the entries of that array constant; None for any other spread."""
    inner = [c for c in node.children if c.is_named]
    name = _self_name(inner[0]) if len(inner) == 1 else None
    return None if name is None else _constant_array(name, constants, arrays, seen)


def _array_keys(node) -> tuple[str, ...]:
    return tuple(key for key, _ in _array_entries(node)[0])


_PARAMETER_KINDS = ("simple_parameter", "property_promotion_parameter", "variadic_parameter")


def _parameter_defaults(method) -> tuple[tuple[str, ...], bool]:
    """Names of the controller parameters with a default value, and True when unread.

    `AttributeClassLoader::addRoute` makes such a default the route default of the
    placeholder with the same name, which can make that placeholder optional.
    """
    params = nodes.child(method, "formal_parameters") if method is not None else None
    if params is None:
        return (), True
    names: list[str] = []
    for param in params.children:
        if not param.is_named or param.type == "comment":
            continue
        if param.type not in _PARAMETER_KINDS:
            return tuple(names), True
        if param.child_by_field_name("default_value") is not None:
            names.append(nodes.variable_of(param))
    return tuple(names), False


def symfony_template(
    path: str,
    requirements: dict[str, str | None] | None = None,
    requirements_unread: bool = False,
    defaults: Iterable[str] = (),
) -> tuple[str, str]:
    """`(route_template, reason)` for a composed Symfony path.

    Inline requirements and defaults are dropped: `{id<\\d+>}` is `{id}`, `{page<\\d+>?1}` is
    `{page?}`. A placeholder is optional only in the trailing run `RouteCompiler` makes
    optional: it has a default, is not `{!name}`, and only a separator follows it. A requirement
    that may match `/` gives `""` and `TEMPLATE_MULTI_SEGMENT`. An unread requirement keeps the
    template and gives `TEMPLATE_REQUIREMENT_UNREAD`.
    """
    requirements = requirements or {}
    with_default = set(defaults)
    matches = list(_SYMFONY_PLACEHOLDER.finditer(path))
    multi: set[str] = set()
    unread = requirements_unread
    for match in matches:
        name = match.group(2)
        inline = match.group(3)
        if inline is not None and requirement_may_match_slash(inline[1:-1]):
            multi.add(name)
        if match.group(4) is not None:
            with_default.add(name)
        if name in requirements:
            regex = requirements[name]
            if regex is None:
                unread = True
            elif requirement_may_match_slash(regex):
                multi.add(name)
    optional: set[str] = set()
    if matches and matches[-1].end() == len(path):
        for k in range(len(matches) - 1, -1, -1):
            match = matches[k]
            if match.group(1) or match.group(2) not in with_default:
                break
            optional.add(match.group(2))
            gap = path[matches[k - 1].end() if k else 0 : match.start()]
            if gap and not (len(gap) == 1 and gap in _SEPARATORS):
                break
    plain = _SYMFONY_PLACEHOLDER.sub(lambda m: "{" + m.group(2) + "}", path)
    template, reason = brace_template(plain, optional, multi)
    if not reason and unread:
        reason = TEMPLATE_REQUIREMENT_UNREAD
    return template, reason


def _compose(
    attribute,
    prefix: _Prefix,
    class_fqn: str,
    method_name: str,
    source_file: str,
    constants: dict[str, str | None] | None = None,
    method=None,
    arrays: dict[str, Any] | None = None,
) -> RouteDeclaration:
    positional, named = arguments(attribute)
    path_node = named.get("path") or (positional[0] if positional else None)
    method_path, literal = path_of(path_node, constants)

    name = string_value(named.get("name"))
    # `array_unique(array_merge(...))`: a union, with the class-level entries first.
    methods = tuple(dict.fromkeys(prefix.methods + string_list(named.get("methods"))))

    effective: str | None = None
    reason = ""
    if path_node is None:
        reason = NO_PATH
    elif not literal:
        reason = LOCALIZED_PATH if method_path is None and is_array(path_node) else PATH_NOT_LITERAL
    elif not prefix.path_literal:
        reason = PREFIX_NOT_LITERAL
    elif method_path is not None:
        # Always taken: a literal, present path argument has a value.
        effective = (prefix.path or "") + method_path

    template: str | None = None
    template_reason = ""
    facts: access_control.PlaceholderFacts | None = None
    if effective is not None:
        # `array_replace($globals['requirements'], $requirements)`: the method's entries win.
        requirements, requirements_unread = _requirements(
            named.get("requirements"), constants, arrays
        )
        template, template_reason = symfony_template(
            effective,
            dict(prefix.requirements + requirements),
            prefix.requirements_unread or requirements_unread,
            prefix.defaults + _array_keys(named.get("defaults")),
        )
        defaults, defaults_unread = _array_entries(named.get("defaults"))
        parameter_defaults, parameters_unread = _parameter_defaults(method)
        facts = access_control.PlaceholderFacts(
            requirements=prefix.requirements + requirements,
            defaults=prefix.defaults + tuple(key for key, _ in defaults) + parameter_defaults,
            unread=(
                prefix.requirements_unread
                or requirements_unread
                or prefix.defaults_unread
                or defaults_unread
                or parameters_unread
            ),
        )

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
        provider=NAME,
        template=template,
        template_reason=template_reason,
        placeholder_facts=facts,
    )
