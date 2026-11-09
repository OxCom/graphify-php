"""The interfaces this package is built around.

There are two ways to learn that `$this->emails` is an `Emails`: parse the file, or ask a PHP
type engine that already knows. They have different costs — the parser needs nothing, the
engine needs the project's `vendor/` and runs PHP — and different reach: the engine resolves
untyped properties, chains and docblocks that the parser must refuse.

Both are therefore the same thing to the code that builds edges: a source of resolved call
targets. That is `CallTargetSource` below, and it is the only thing `resolver` depends on.
Adding a third source later — a language server, an LSIF index — means implementing one
protocol, not editing the resolver.

`Confidence` is not decoration. graphify already distinguishes EXTRACTED from INFERRED on its
own edges, and the mesh surfaces that distinction to callers; a target proved by a type engine
and one guessed from a declaration must not arrive looking alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Confidence(str, Enum):
    """How the target was established, in graphify's own vocabulary."""

    EXTRACTED = "EXTRACTED"  # the receiver's type is stated in source and was read there
    INFERRED = "INFERRED"  # derived: a constructor-body assignment, a `new` binding


@dataclass(frozen=True)
class CallSite:
    """One `$receiver->method(...)` that graphify could not resolve.

    `caller_nid` is graphify's node id for the enclosing callable, carried through untouched:
    this package never invents node identity, it only connects nodes graphify already made.
    """

    caller_nid: str
    receiver: str  # source text of the receiver expression, e.g. `$this->emails`
    method: str
    source_file: str
    source_location: str
    # Not a `$receiver->method()`: a scoped call `X::m()`, or a probe guessing a method NAME
    # for a non-member call (on graphify 0.9.103 also `new X(...)`). `receiver` holds the scope.
    scoped: bool = False
    # `Class::method` spellings a type engine may report for code inside the caller: the
    # method's class, every class mixing in its trait, the method enclosing a closure. `()` when
    # no class method can be named for it, `None` when no index was asked. `::method` (no
    # class) is a method whose class the graph lost: the source must prove the class itself.
    caller_scope: tuple[str, ...] | None = None
    # No other callable starts between the caller's first line and this site's line, so a
    # call on that line that the type engine places in the caller is in the caller's own body.
    owns_line: bool = False


@dataclass(frozen=True)
class CallTarget:
    """A call site resolved to a class and method, with the evidence that resolved it."""

    site: CallSite
    class_fqn: str
    method: str
    confidence: Confidence
    via: str  # which type source proved it, for diagnosis and for the status record
    # Where the declaring class lives, '' when the source does not know. Lets the graph layer
    # tell a vendor target from a project class the build left out, without guessing from the FQN.
    declaring_file: str = ""
    # The target names an interface method: a contract, not a body.
    interface_target: bool = False
    # The source read every file the build parsed and none declares the class, so no graph
    # node can hold it, however a short name or an alias matches.
    outside_corpus: bool = False


@dataclass(frozen=True)
class LineProof:
    """Every type-engine target for one method name on one line, made inside one caller.

    `calls` is how many distinct calls those targets belong to: the arms of a union receiver
    are one call, `$a->get(); $b->get();` are two. Empty when the source has no such record.
    """

    targets: tuple[CallTarget, ...]
    calls: int


@dataclass(frozen=True)
class NullsafeCall:
    """One `$x?->method()` a parser found. graphify never sends these as raw calls.

    `caller_lines` are the lines the innermost function, method or closure around the call
    starts on (its declaration, and its name when that differs), which is how the graph layer
    finds the caller node. `refusal` is a fixed reason when the parser cannot vouch for the
    site, and then `caller_lines` is empty.
    """

    source_file: str  # spelled as the corpus spells it
    line: int
    method: str
    caller_lines: tuple[int, ...] = ()
    refusal: str = ""


@dataclass(frozen=True)
class EngineCall:
    """A `calls` edge graphify's engine bound by bare method name to a node in the caller's file.

    The engine keeps one edge per (caller, target) and the FIRST site's line, so the edge may
    stand for several `->method(` sites in the caller. `edge` is graphify's own dict, opaque
    here, carried so the sink can take that exact object out of the list.
    """

    caller_nid: str
    target_nid: str
    method: str  # the target's bare name
    source_file: str
    source_location: str
    caller_location: str  # where the caller starts, to find its body
    edge: object = field(default=None, compare=False, repr=False)
    # The graph proves the target a class method (a `method` edge, a `.m()` label). False for a
    # dot-less node a file contains: a free function and an anonymous class's method look alike.
    target_is_method: bool = False
    target_location: str = ""  # where the target is declared, for a parser to tell which


@dataclass(frozen=True)
class CallerSites:
    """What a parser read in one caller about calls of one name.

    `lines`: every `->name(` and `?->name(` in the caller's span, nested callables included, as
    {line: count}. `foreign`: lines among those where a member call of that name sits outside
    the caller's own body yet inside the method a type engine files the caller's calls under.
    `other_kinds`: a function or scoped call of that name sits in the caller's span.
    """

    lines: dict[int, int]
    foreign: frozenset[int] = frozenset()
    other_kinds: bool = False


@dataclass(frozen=True)
class MethodBoundCall:
    """A `calls` edge graphify bound by name from a PHP line to a method node; not one of ours.

    The shape a cross-file bare-name pass leaves when it case-folds `new Type(...)` onto a
    method `type()`. `edge` is graphify's own dict, opaque here, as on `EngineCall`.
    """

    caller_nid: str
    target_nid: str
    method: str  # the target's bare name
    source_file: str
    source_location: str
    edge: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class NewSiteLine:
    """What a parser read on one line about one name.

    `creates`: a `new X(...)` whose class name's last segment is the name, case-insensitive,
    starts on the line. `calls_name`: a member, nullsafe, scoped or function call of that name,
    or one whose name is dynamic, sits on the line.
    """

    creates: bool
    calls_name: bool


@runtime_checkable
class CallTargetSource(Protocol):
    """Anything that can turn unresolved call sites into targets.

    The contract that matters is not in the signature: a source returns a target ONLY when it
    can prove it. Six upstream issues across four code-graph projects trace their false edges
    to a name-only fallback — `->get()` wired to an unrelated class, `empty()` wired to a
    method called `empty`. A source that cannot prove a site leaves it out and says why
    through `unresolved`, so the gap is counted rather than filled with a guess.
    """

    name: str

    def available(self, repo_root: str) -> tuple[bool, str]:
        """Can this source run here? Returns (yes, reason-if-not).

        A repository with no `vendor/bin/phpstan` is a normal, expected "no": the build
        records a skip and carries on with whatever other sources are available. It is not a
        failure, and conflating the two is how a missing layer becomes invisible.
        """

    def resolve(
        self,
        repo_root: str,
        sites: Iterable[CallSite],
        corpus: Iterable[str] | None = None,
    ) -> Resolution:
        """Resolve `sites`, optionally with `corpus` as the set of files that may be read.

        `corpus` exists because a source needs declarations from files that hold no call site
        of their own. A leaf service class has nothing unresolved in it, so it never appears
        among `sites`, and a source that indexes only the files it was handed cannot tell
        whether that class declares the method being called on it. Measured on an intersection
        type: the same call resolves when the constituent's file happens to carry a site and
        stays `unknown-receiver-type` when it does not, which makes the answer depend on
        unrelated code.

        The caller passes the files graphify itself parsed, so the set is bounded by the build
        rather than by a filesystem walk — an earlier version walked to the nearest
        `composer.json` and cost 4.7 s against 0.16 s for the site files alone, while also
        reading paths outside the build.

        `None` means "only the files carrying sites", which stays the honest default for a
        caller that has no corpus to offer.
        """


@dataclass
class Resolution:
    """What a source made of the sites it was given."""

    targets: list[CallTarget]
    unresolved: dict[str, int]  # reason -> count, a fixed vocabulary per source
    files_seen: int = 0


@runtime_checkable
class NodeIndex(Protocol):
    """graphify's nodes, queried the one way this package needs.

    Narrow on purpose: the resolver needs "which node is method M of class C", nothing else.
    A wider interface would tie this package to graphify's node schema, which is the thing
    most likely to move under it between releases.
    """

    def method_node(self, class_fqn: str, method: str) -> str | None: ...


@runtime_checkable
class EdgeSink(Protocol):
    """Where resolved targets become graph edges."""

    def add_call(self, caller_nid: str, callee_nid: str, target: CallTarget) -> None: ...


@runtime_checkable
class ScopeIndex(Protocol):
    """What a scoped call needs from the graph, beside `NodeIndex`.

    `Helper::format()` reaches graphify as a call to `Helper` with the method name dropped
    (`extractors/engine.py`, the `scoped_call_expression` arm), so the site arrives without the
    one field it exists to carry. The graph knows which methods a class declares, and a type
    source can check each candidate against the line it came from — so the name is recoverable
    without the integration layer parsing PHP. A class with ten methods costs ten lookups and
    yields at most one edge.

    Kept apart from `NodeIndex` because the two answer different questions and an index that
    can do one need not do the other.
    """

    def scope_methods(self, caller_nid: str, scope: str) -> list[str]:
        """Methods the named scope declares, for `self`, `parent`, `static` or a class name."""


@runtime_checkable
class RouteDeclarationSource(Protocol):
    """Anything that can find route declarations in a codebase.

    Same contract as `CallTargetSource`: a declaration is returned only when it was read, and a
    route whose path could not be composed is counted under a fixed reason rather than guessed
    into existence. It is a separate port because a route has no call site — forcing it through
    `CallTargetSource` would mean inventing a `caller_nid`, and this package never invents node
    identity.
    """

    name: str

    def available(self, repo_root: str) -> tuple[bool, str]: ...

    def declarations(self, repo_root: str, files: Iterable[str]) -> object:
        """Route declarations found in `files`. The concrete result type lives with the source,
        because the shape of a declaration is the source's business and this port only needs to
        name the call."""
