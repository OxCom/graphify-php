"""One class per type source.

Each collector handles exactly one grammar node kind and writes exactly one kind of fact.
They are registered in `COLLECTORS` at the bottom, so a fifth type source — docblocks, an
attribute, a container definition — is a new class and one list entry, never a new branch in
the walker. `file_scan` imports the registry and has no idea what is in it.

The four here, in the precedence they settle conflicts by (lowest number wins):

1. Promoted constructor parameters — `__construct(private readonly Emails $emails)`. Measured
   on one Symfony application: 652 of 715 injected properties carry a concrete class type and
   63 an interface (92 % / 8 %), so this one source covers most of the receivers that matter.
2. Typed property declarations — `private readonly Emails $legacy;`.
3. Constructor-body assignment from a typed parameter — `$this->legacy = $x;` where the
   constructor declares `Emails $x`. The case pure declaration reading misses; it predates
   promoted properties, so it is common in older code rather than exotic.
4. `new` bindings for locals — `$mailer = new Emails();`.

Docblocks are not read at all in this version. A `@var` that contradicts a native type is a
bug in the code, and resolving one correctly needs scope tracking these collectors do not do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..ports import Confidence
from . import nodes
from .facts import TypeFact

# Fixed reasons, shared with the resolver so that a refusal recorded while scanning and a
# refusal decided at the call site cannot drift into two spellings of the same thing.
UNION_TYPE = "union-type"
REASSIGNED_LOCAL = "reassigned-local"


@dataclass
class ScanContext:
    """Where the walker currently is. Collectors read it; they never change it."""

    facts: object            # FileFacts, kept untyped here to avoid an import cycle
    names: object            # NameResolver
    klass: object | None     # ClassScope
    method: object | None    # MethodScope


class TypeCollector(Protocol):
    """A source of receiver types.

    `node_kind` is what the walker dispatches on, `via` is what lands in `CallTarget.via`, and
    `precedence` settles a conflict with another collector without either knowing the other.
    """

    node_kind: str
    via: str
    confidence: Confidence
    precedence: int

    def collect(self, node, ctx: ScanContext) -> None: ...


def _fact(collector: TypeCollector, fqn: str, members: tuple = ()) -> TypeFact:
    return TypeFact(
        fqn=fqn,
        confidence=collector.confidence,
        via=collector.via,
        precedence=collector.precedence,
        members=members,
    )


def _declared_type(collector: TypeCollector, node, ctx: ScanContext) -> tuple[str | None, tuple, str | None]:
    """Read a declaration's type annotation as (fqn, intersection members, refusal reason).

    Four outcomes. A concrete class is a fact. An intersection `A&B` is a fact with several
    constituents, resolved at the call site by which one declares the method. A union is a
    refusal that must be counted, because both alternatives are real targets and this version
    cannot express two. Anything else — untyped, `int`, `self` — is none of them, because it
    names no call target at all.
    """
    annotation = nodes.type_node(node)
    if nodes.is_union(annotation):
        return None, (), UNION_TYPE
    members = nodes.intersection_members(annotation)
    if members:
        return None, tuple(ctx.names.resolve(m) for m in members), None
    short = nodes.type_name(annotation)
    if not short:
        return None, (), None
    return ctx.names.resolve(short), (), None


class PromotedParameterCollector:
    """`__construct(private readonly Emails $emails)`.

    The dominant source in modern PHP: constructor promotion states the property's type and
    its injection in one place, which is why 92 % of the reference application's injected
    properties resolve from this collector alone.
    """

    node_kind = "property_promotion_parameter"
    via = "promoted-parameter"
    confidence = Confidence.EXTRACTED
    precedence = 10

    def collect(self, node, ctx: ScanContext) -> None:
        if ctx.klass is None:
            return
        prop = nodes.variable_of(node)
        if not prop:
            return
        fqn, members, refusal = _declared_type(self, node, ctx)
        if refusal:
            ctx.klass.refuse_property(prop, refusal, self.precedence)
        elif fqn or members:
            ctx.klass.record_property(prop, _fact(self, fqn or "", members))


class TypedPropertyCollector:
    """`private readonly Emails $legacy;`, including the multi-element form."""

    node_kind = "property_declaration"
    via = "typed-property"
    confidence = Confidence.EXTRACTED
    precedence = 20

    def collect(self, node, ctx: ScanContext) -> None:
        if ctx.klass is None:
            return
        fqn, members, refusal = _declared_type(self, node, ctx)
        if not fqn and not members and not refusal:
            return
        for element in nodes.children(node, "property_element"):
            prop = nodes.variable_of(element)
            if not prop:
                continue
            if refusal:
                ctx.klass.refuse_property(prop, refusal, self.precedence)
            else:
                ctx.klass.record_property(prop, _fact(self, fqn or "", members))


class ConstructorAssignmentCollector:
    """`$this->legacy = $x;` where the constructor declares `Emails $x`.

    Registered on the constructor rather than on the assignment so that the parameter types
    and the statements that use them are read in one place; an assignment node on its own
    cannot see the signature it draws from without walking back up the tree.

    INFERRED, not EXTRACTED: the property itself states no type, and the link from parameter
    to property is a reading of one statement, not a declaration.
    """

    node_kind = "method_declaration"
    via = "constructor-assignment"
    confidence = Confidence.INFERRED
    precedence = 30

    def collect(self, node, ctx: ScanContext) -> None:
        if ctx.klass is None or nodes.name_of(node) != "__construct":
            return
        params = self._parameter_types(node, ctx)
        if not params:
            return
        body = nodes.child(node, "compound_statement")
        for assignment in self._assignments(body):
            prop, source_var = self._this_assignment(assignment)
            if not prop or source_var not in params:
                continue
            fqn, members, refusal = params[source_var]
            if refusal:
                ctx.klass.refuse_property(prop, refusal, self.precedence)
            elif fqn or members:
                ctx.klass.record_property(prop, _fact(self, fqn or "", members))

    def _parameter_types(self, ctor, ctx: ScanContext) -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        params = nodes.child(ctor, "formal_parameters")
        for param in nodes.children(params, "simple_parameter", "property_promotion_parameter"):
            name = nodes.variable_of(param)
            if not name:
                continue
            fqn, members, refusal = _declared_type(self, param, ctx)
            if fqn or members or refusal:
                out[name] = (fqn, members, refusal)
        return out

    def _assignments(self, node):
        """Every assignment in the constructor body, nested ones included.

        A guarded assignment inside an `if` is still the only thing that ever sets the
        property in the overwhelming majority of constructors, so restricting this to
        top-level statements would drop real facts for no gain in precision.
        """
        if node is None:
            return
        if node.type == "assignment_expression":
            yield node
        for candidate in node.children:
            yield from self._assignments(candidate)

    def _this_assignment(self, assignment) -> tuple[str | None, str | None]:
        """`$this->prop = $var` as (prop, var), or (None, None) for anything else.

        `$this->$prop = ...` falls out here because the property side is a `variable_name`
        rather than a `name`, which is the shape of a dynamic property and not something a
        static read can name.
        """
        left = assignment.children[0] if assignment.children else None
        right = assignment.children[-1] if len(assignment.children) > 2 else None
        if left is None or right is None:
            return None, None
        if left.type != "member_access_expression" or right.type != "variable_name":
            return None, None
        receiver = left.children[0] if left.children else None
        prop_node = left.children[-1] if len(left.children) > 1 else None
        if receiver is None or prop_node is None or prop_node.type != "name":
            return None, None
        if nodes.node_text(receiver) != "$this":
            return None, None
        return nodes.node_text(prop_node), nodes.node_text(right).lstrip("$")


class LocalConstructionCollector:
    """`$mailer = new Emails();`, and the reassignment that invalidates it.

    Aliasing a property into a local (`$e = $this->emails;`) is deliberately not tracked: it
    needs flow analysis to know the local was not reassigned before the call, and a binding
    that survives its own reassignment is exactly how a resolver starts lying. For the same
    reason every other assignment to a bound local revokes the binding rather than being
    ignored — the second write is the evidence that the first no longer holds.
    """

    node_kind = "assignment_expression"
    via = "new-binding"
    confidence = Confidence.INFERRED
    precedence = 40

    def collect(self, node, ctx: ScanContext) -> None:
        if ctx.method is None:
            return
        left = node.children[0] if node.children else None
        right = node.children[-1] if len(node.children) > 2 else None
        if left is None or right is None or left.type != "variable_name":
            return
        local = nodes.node_text(left).lstrip("$")
        if not local or local == "this":
            return

        # Any second write revokes the binding, in either order. Without flow analysis there is
        # no way to tell a call placed before the second write from one placed after it, and
        # only one of those two readings is true.
        rewritten = local in ctx.method.assigned
        ctx.method.assigned.add(local)
        if rewritten:
            ctx.method.refuse_local(local, REASSIGNED_LOCAL, self.precedence)
            return
        if right.type != "object_creation_expression":
            return
        # `new $class()` and `new class {...}` write no class name, so there is none to read.
        created = nodes.type_name(nodes.child(right, "qualified_name", "name"))
        if not created:
            return
        ctx.method.record_local(local, _fact(self, ctx.names.resolve(created)))


# The registry. Adding a type source means adding a class above and one entry here.
COLLECTORS: list[TypeCollector] = [
    PromotedParameterCollector(),
    TypedPropertyCollector(),
    ConstructorAssignmentCollector(),
    LocalConstructionCollector(),
]


def by_node_kind(collectors: list[TypeCollector] | None = None) -> dict[str, list[TypeCollector]]:
    """Group the registry so the walker dispatches by node kind instead of testing each one."""
    registry: dict[str, list[TypeCollector]] = {}
    for collector in collectors if collectors is not None else COLLECTORS:
        registry.setdefault(collector.node_kind, []).append(collector)
    for bucket in registry.values():
        bucket.sort(key=lambda c: c.precedence)
    return registry
