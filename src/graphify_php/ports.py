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

    def resolve(self, repo_root: str, sites: Iterable[CallSite]) -> "Resolution":
        ...


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
