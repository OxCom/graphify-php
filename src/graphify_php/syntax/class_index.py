"""Which classes exist in the corpus, and what each one declares.

The receiver's type is only half of a call target. Resolving `$this->magic` to
`Eval\\Dynamic\\Magic` and then emitting whatever method name the call site wrote produces an
edge to a member that does not exist — right class, invented method. That is the softer form
of the failure the six upstream issues describe, and a consumer cannot tell it from a real
edge, so it has to be checked rather than assumed.

The check has three answers, not two, and keeping them apart is the whole point:

- the method is declared, somewhere in the class's own ancestry — emit;
- the class is in the corpus, its whole ancestry is in the corpus, and none of them declares
  the method — a real refusal;
- the class or one of its ancestors was never read — absence is not proved, so the caller
  emits the target and lets `GraphNodeIndex.method_node` settle it against the whole corpus.

A class that declares `__call` answers every method name at runtime, so a name missing from it
is dispatched, not absent. That is `indirect-dispatch`, the reason that already exists for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# What the lookup concluded. Strings rather than an enum so they read plainly in a debugger.
DECLARED = "declared"
NOT_DECLARED = "not-declared"
UNKNOWN = "unknown"
MAGIC = "magic"

MAGIC_METHODS = ("__call", "__callStatic")


@dataclass(frozen=True)
class ClassRecord:
    """One declaration: what it declares itself, and where else to look."""

    fqn: str
    methods: frozenset[str]
    # extends, implements and `use`d traits together. The lookup does not care which is which:
    # all three are places a method can be declared, and PHP resolves them into one class.
    ancestors: tuple[str, ...]


@dataclass
class ClassIndex:
    classes: dict[str, ClassRecord] = field(default_factory=dict)
    # The scopes themselves, for the property walk. Kept beside the records rather than folded
    # into them because a record is a summary and this needs the declarations as parsed.
    scopes: dict[str, object] = field(default_factory=dict)

    def add(self, record: ClassRecord, scope=None) -> None:
        self.classes[record.fqn] = record
        if scope is not None:
            self.scopes[record.fqn] = scope

    def property_fact(self, fqn: str, name: str):
        """The nearest declaration of `$name` in the ancestry, as (fact, refusal reason).

        Breadth-first so the nearest declaration wins: a subclass that redeclares `$emails`
        with a narrower type beats the ancestor's, which is the whole reason PHP allows the
        redeclaration. Within one level the order is `extends`, then `implements`, then traits.

        An ancestor in a file this run never read simply is not here, so its properties are
        neither found nor disproved and the caller reports the type as unknown. Same principle
        as the method check: disprove only what was seen.
        """
        level = [fqn]
        seen: set[str] = set()
        while level:
            following: list[str] = []
            for current in level:
                if current in seen:
                    continue
                seen.add(current)
                scope = self.scopes.get(current)
                if scope is None:
                    continue
                refusal = scope.refusals.get(name)
                if refusal:
                    return None, refusal
                fact = scope.properties.get(name)
                if fact is not None:
                    return fact, None
                following.extend(scope.ancestors)
            level = following
        return None, None

    def declares(self, fqn: str, method: str) -> str:
        """Walk the ancestry for `method` and report which of the three answers applies."""
        seen: set[str] = set()
        pending = [fqn]
        saw_unknown = False
        saw_magic = False
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            record = self.classes.get(current)
            if record is None:
                saw_unknown = True
                continue
            if method in record.methods:
                return DECLARED
            if any(magic in record.methods for magic in MAGIC_METHODS):
                saw_magic = True
            pending.extend(record.ancestors)
        if saw_magic:
            return MAGIC
        return UNKNOWN if saw_unknown else NOT_DECLARED


def record_for(scope, methods: frozenset[str]) -> ClassRecord:
    return ClassRecord(fqn=scope.fqn, methods=methods, ancestors=tuple(scope.ancestors))


def index_from_facts(index: ClassIndex, facts) -> None:
    """Fold one file's declarations into the index."""
    for scope in facts.classes:
        index.add(record_for(scope, frozenset(m.name for m in scope.methods if m.name)), scope)
