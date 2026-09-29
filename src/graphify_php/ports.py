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

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable


class Confidence(str, Enum):
    """How the target was established, in graphify's own vocabulary."""

    EXTRACTED = "EXTRACTED"   # the receiver's type is stated in source and was read there
    INFERRED = "INFERRED"     # derived: a constructor-body assignment, a `new` binding


@dataclass(frozen=True)
class CallSite:
    """One `$receiver->method(...)` that graphify could not resolve.

    `caller_nid` is graphify's node id for the enclosing callable, carried through untouched:
    this package never invents node identity, it only connects nodes graphify already made.
    """

    caller_nid: str
    receiver: str          # source text of the receiver expression, e.g. `$this->emails`
    method: str
    source_file: str
    source_location: str


@dataclass(frozen=True)
class CallTarget:
    """A call site resolved to a class and method, with the evidence that resolved it."""

    site: CallSite
    class_fqn: str
    method: str
    confidence: Confidence
    via: str               # which type source proved it, for diagnosis and for the status record


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
    ) -> "Resolution":
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
    unresolved: dict[str, int]      # reason -> count, a fixed vocabulary per source
    files_seen: int = 0


@runtime_checkable
class NodeIndex(Protocol):
    """graphify's nodes, queried the one way this package needs.

    Narrow on purpose: the resolver needs "which node is method M of class C", nothing else.
    A wider interface would tie this package to graphify's node schema, which is the thing
    most likely to move under it between releases.
    """

    def method_node(self, class_fqn: str, method: str) -> str | None:
        ...


@runtime_checkable
class EdgeSink(Protocol):
    """Where resolved targets become graph edges."""

    def add_call(self, caller_nid: str, callee_nid: str, target: CallTarget) -> None:
        ...


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

    def available(self, repo_root: str) -> tuple[bool, str]:
        ...

    def declarations(self, repo_root: str, files: Iterable[str]) -> object:
        """Route declarations found in `files`. The concrete result type lives with the source,
        because the shape of a declaration is the source's business and this port only needs to
        name the call."""
