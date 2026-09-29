"""What a compiled DI container says, modelled without reference to any file format.

This is the middle of three units: `reader` turns one container format into these objects,
`overlay` turns these objects into an artifact. A second container format (a Laravel dump, a
PHP-DI compiled file) is a second reader against this model, never a branch inside the first.

Two rules are carried by the model itself rather than by discipline in the reader.

**No values, ever.** An argument keeps its KIND (`service`, `scalar`, `env`, ...) and, for an
env reference, only the variable NAME. There is no field to put a DSN, a password, a default
or an expanded `%env(...)%` in, so a reader cannot leak one by being careless. `eval/canaries.py`
plants fake secrets and asserts they reach no artifact; the absence of the field is what makes
that hold by construction.

**Injection-site identity.** Measured on the reference application: 715 injected properties,
652 with a concrete class (resolved from source by the existing sources) and 63 with an
interface — 8%. Those 63 are why this exists, and a global interface-to-class map would answer
them wrongly: named aliases, per-argument bindings, decorators and contextual wiring all make
the same interface arrive as different classes at different parameters. So the unit of this
model is `Injection` — consumer, member, position — not "what implements X".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ArgumentKind(str, Enum):
    """The kind of thing wired into one argument slot. Never the thing itself."""

    SERVICE = "service"                    # a reference to another service id
    SERVICE_CLOSURE = "service_closure"    # a lazy reference; same target, deferred
    TAGGED_ITERATOR = "tagged_iterator"    # every service carrying a tag
    ITERATOR = "iterator"                  # an explicit list of service references
    COLLECTION = "collection"              # a structured literal; its leaves are scalars
    ENV = "env"                            # a `%env(...)%` reference, name kept, value not
    SCALAR = "scalar"                      # a literal. Recorded as a kind and discarded.
    ABSTRACT = "abstract"                  # an unfilled slot in an abstract definition
    CONSTANT = "constant"                  # a PHP constant reference
    UNKNOWN = "unknown"


class Member(str, Enum):
    """Where in the consumer the argument lands."""

    CONSTRUCTOR = "constructor"
    CALL = "call"            # a setter or any other post-construction method call
    FACTORY = "factory"      # an argument to the factory, not to the constructor
    PROPERTY = "property"


@dataclass(frozen=True)
class Service:
    """One service definition. `class_fqn` is None for a synthetic or alias-only entry."""

    service_id: str
    class_fqn: str | None
    public: bool = False
    abstract: bool = False
    lazy: bool = False
    synthetic: bool = False
    factory_class: str | None = None
    factory_service: str | None = None
    factory_method: str | None = None
    # A named constructor (`constructor="fromCallable"`): the class is instantiated through a
    # static method, so a consumer looking for `__construct` would find the wrong signature.
    constructor: str | None = None


@dataclass(frozen=True)
class Alias:
    """`alias_id` resolves to `target_id`. The interface-to-implementation edge, when the
    alias id happens to be an interface name — which the container never states, so the
    reader does not claim it either."""

    alias_id: str
    target_id: str
    public: bool = False


@dataclass(frozen=True)
class Injection:
    """One argument slot of one consumer, and what the container puts in it.

    `position` is the 0-based index among the consumer's arguments. `parameter` is the
    parameter NAME and is almost always None: the compiled XML keeps argument order, not
    signatures. Recorded as a field anyway because a reader for a format that does keep names
    can fill it, and a consumer joining on position must be able to tell "unnamed" from
    "unknown".
    """

    consumer_id: str
    consumer_class: str | None
    member: Member
    member_name: str | None        # the method for Member.CALL, None for a constructor
    position: int
    parameter: str | None
    kind: ArgumentKind
    requested_id: str | None = None      # the service id written in the definition
    resolved_id: str | None = None       # after following aliases
    resolved_class: str | None = None    # the class that actually arrives here
    via_alias: bool = False
    tag: str | None = None               # for TAGGED_ITERATOR, the tag being collected
    env_name: str | None = None          # for ENV, the variable name. Never its value.


@dataclass(frozen=True)
class TagMembership:
    tag: str
    service_id: str
    class_fqn: str | None
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Decoration:
    """`decorator_id` wraps `inner_id`, which holds the definition that was displaced.

    The compiled container has no `decorates` attribute: Symfony applies decoration at compile
    time, renaming the decorated definition to `<id>.inner` and giving its id to the decorator.
    Measured on the reference application: 0 occurrences of `decorates=`, 28 services whose id
    ends in `.inner`. So decoration is recovered from that renaming, and the field name says
    what was observed rather than what was declared.
    """

    decorator_id: str
    decorator_class: str | None
    inner_id: str
    inner_class: str | None


@dataclass
class ContainerFacts:
    """Everything one container file states, plus what the reader refused and why."""

    services: dict[str, Service] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)
    injections: list[Injection] = field(default_factory=list)
    tags: list[TagMembership] = field(default_factory=list)
    decorations: list[Decoration] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str, count: int = 1) -> None:
        """Count a fact left out, under a fixed reason.

        Free-text reasons produce a histogram of unique strings, which is not a work list.
        """
        self.skipped[reason] = self.skipped.get(reason, 0) + count

    def resolve_id(self, service_id: str, _depth: int = 0) -> str:
        """Follow aliases to the id that is actually instantiated.

        Bounded depth because an alias cycle in a hand-written container would otherwise hang
        the build, and a build that hangs is indistinguishable from one that is slow.
        """
        seen = service_id
        for _ in range(16):
            alias = self.aliases.get(seen)
            if alias is None:
                return seen
            seen = alias.target_id
        return seen

    def class_of(self, service_id: str) -> str | None:
        service = self.services.get(self.resolve_id(service_id))
        return service.class_fqn if service else None

    @property
    def interface_bindings(self) -> list[Alias]:
        """Aliases whose id looks like a PHP interface name.

        `Interface` suffix only, which is a convention and not a guarantee — the container
        states no such thing. Kept as a derived view so nothing downstream mistakes it for a
        fact the container asserted.
        """
        return [a for a in self.aliases.values() if a.alias_id.endswith("Interface")]
