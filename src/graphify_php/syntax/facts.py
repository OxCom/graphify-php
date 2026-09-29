"""What one PHP file says about the types of its receivers.

Scoped per class AND per method rather than flat. Two classes in one file may both declare
`$repo`, and two methods may both bind a local `$service`; a flat table silently merges them
and produces an edge to the wrong class — the same failure the short-name fallback produces,
reached by a different route.

A scope records two kinds of answer, and the second is the point of the package: a type it
knows, and a name it deliberately refuses with a reason. A property that is missing from both
is a gap; a property in `refusals` is a decision, and the reason travels all the way out to
`Resolution.unresolved` so the gap becomes a work list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ports import Confidence
from .call_sites import CallSiteIndex


@dataclass(frozen=True)
class TypeFact:
    """A receiver type, with the evidence that produced it.

    `precedence` is how two sources claiming the same property are settled without either
    collector knowing the other exists: lowest wins, and the numbers live on the collectors.
    """

    fqn: str
    confidence: Confidence
    via: str
    precedence: int


@dataclass
class MethodScope:
    name: str
    start_line: int
    end_line: int
    locals: dict[str, TypeFact] = field(default_factory=dict)
    refusals: dict[str, str] = field(default_factory=dict)
    # Every local this method writes to, whether or not the write produced a usable type.
    # A second write is what revokes a binding, so the ones that produced nothing still count.
    assigned: set = field(default_factory=set)

    def record_local(self, name: str, fact: TypeFact) -> None:
        existing = self.locals.get(name)
        if existing is not None and existing.precedence <= fact.precedence:
            return
        self.locals[name] = fact

    def refuse_local(self, name: str, reason: str, precedence: int) -> None:
        """Drop whatever was known about a local and say why.

        Refusal wins over a recorded type of equal or weaker precedence on purpose. The case
        that forces it is a local reassigned after its binding: the earlier `new Emails()` is
        still true at the point it was written and false by the time of the call, and there is
        no flow analysis here to tell the two positions apart. A fact from a strictly stronger
        source survives, so one collector's doubt cannot erase another's declaration.
        """
        existing = self.locals.get(name)
        if existing is not None and existing.precedence < precedence:
            return
        self.locals.pop(name, None)
        self.refusals[name] = reason

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


@dataclass
class ClassScope:
    fqn: str
    start_line: int
    end_line: int
    parent: str | None = None
    # extends, implements and `use`d traits, all resolved. One list because a method can be
    # declared in any of them and PHP flattens all three into one class.
    ancestors: list[str] = field(default_factory=list)
    properties: dict[str, TypeFact] = field(default_factory=dict)
    refusals: dict[str, str] = field(default_factory=dict)
    methods: list[MethodScope] = field(default_factory=list)

    def record_property(self, name: str, fact: TypeFact) -> None:
        if name in self.refusals:
            return
        existing = self.properties.get(name)
        if existing is not None and existing.precedence <= fact.precedence:
            return
        self.properties[name] = fact

    def refuse_property(self, name: str, reason: str, precedence: int) -> None:
        """See `MethodScope.refuse_local`: a stronger source's fact outranks a weaker doubt."""
        existing = self.properties.get(name)
        if existing is not None and existing.precedence < precedence:
            return
        self.properties.pop(name, None)
        self.refusals[name] = reason

    def method_at(self, line: int) -> MethodScope | None:
        for method in self.methods:
            if method.contains(line):
                return method
        return None

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


@dataclass
class FileFacts:
    """Everything one file proves about itself."""

    path: str
    namespace: str = ""
    imports: dict[str, str] = field(default_factory=dict)
    classes: list[ClassScope] = field(default_factory=list)
    # The receivers graphify does not send: see `call_sites` for why they are read here.
    calls: CallSiteIndex = field(default_factory=CallSiteIndex)

    def class_at(self, line: int) -> ClassScope | None:
        """The innermost class whose body covers `line`.

        Innermost rather than first because a file may declare several classes and the spans
        are checked, not assumed disjoint.
        """
        best: ClassScope | None = None
        for scope in self.classes:
            if not scope.contains(line):
                continue
            if best is None or scope.start_line >= best.start_line:
                best = scope
        return best

    def class_named(self, fqn: str) -> ClassScope | None:
        for scope in self.classes:
            if scope.fqn == fqn:
                return scope
        return None
