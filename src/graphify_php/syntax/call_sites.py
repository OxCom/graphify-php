"""Where the member calls are, and what each one is called on.

This exists because graphify does not tell us. Its PHP branch marks a call as a member call
and records the method name, but never the receiver: `extractors/engine.py:6152-6157` sets
`is_member_call` and `callee_name` and stops, while the C++, Java, C# and Ruby branches beside
it all assign `member_receiver`. The entry built at `engine.py:6403` is
`"receiver": swift_receiver or member_receiver`, so every PHP call site arrives with an empty
receiver and a source that trusted the field would resolve nothing at all.

Reading the receiver out of the tree is better than the string graphify would have handed over
anyway: the tree gives the exact extent of the receiver expression, so `$this->a->b()` as the
receiver of `->c()` is visibly a call rather than a name that happens to contain brackets.

Indexed by (line, method) because that is the only handle a `CallSite` carries —
`engine.py:6402` writes the location as `L<line>` off the call node's own start point.

Scoped calls are indexed here too, and for a sharper reason than member calls. graphify does
not leave `self::save()` unresolved, it records the wrong callee: `engine.py:6147-6152` reads
`scoped_call_expression`'s **scope** as the callee name, so `parent::run()` arrives as a call
to something named `parent` and the method never reaches the record at all. The method name
exists only in the source, so the line is the only way back to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import nodes

# `$a?->b()` binds to the same class as `$a->b()`; null is not a separate call target.
CALL_KINDS = ("member_call_expression", "nullsafe_member_call_expression")
SCOPED_KIND = "scoped_call_expression"

# `self`, `parent` and `static` arrive under this node kind; anything else in scope position
# is either a class name or an expression.
RELATIVE_SCOPE = "relative_scope"


@dataclass(frozen=True)
class ScopedCall:
    """`Scope::method()` — the scope as written, and the grammar's verdict on its shape."""

    scope: str
    kind: str

    @property
    def is_dynamic(self) -> bool:
        """`$class::m()` and `{$expr}::m()`: the class is a runtime value, not a name."""
        return self.kind not in (RELATIVE_SCOPE, "name", "qualified_name")


@dataclass
class CallSiteIndex:
    """Every member call in one file, keyed by the line it starts on and its method name."""

    receivers: dict[tuple[int, str], list[str]] = field(default_factory=dict)
    scopes: dict[tuple[int, str], list[ScopedCall]] = field(default_factory=dict)

    def receiver_for(self, line: int, method: str) -> tuple[str | None, int]:
        """The receiver expression for that call, plus how many calls matched.

        The count is returned rather than resolved here so the caller owns the refusal
        vocabulary: two calls to the same method on one line is a real shape (`$a->run();
        $b->run();`) and picking either one is a coin toss between two different classes.
        """
        found = self.receivers.get((line, method)) or []
        if len(found) != 1:
            return None, len(found)
        return found[0], 1

    def scope_for(self, line: int, method: str) -> tuple[ScopedCall | None, int]:
        """The scope expression for a `Scope::method()` on that line, and how many matched."""
        found = self.scopes.get((line, method)) or []
        if len(found) != 1:
            return None, len(found)
        return found[0], 1


def index_calls(root) -> CallSiteIndex:
    index = CallSiteIndex()
    _walk(root, index)
    return index


def _walk(node, index: CallSiteIndex) -> None:
    if node.type in CALL_KINDS:
        _record(node, index)
    elif node.type == SCOPED_KIND:
        _record_scoped(node, index)
    for child in node.children:
        _walk(child, index)


def _record_scoped(node, index: CallSiteIndex) -> None:
    name_node = node.child_by_field_name("name")
    scope_node = node.child_by_field_name("scope")
    if name_node is None or scope_node is None:
        return
    method = nodes.node_text(name_node)
    if not method:
        return
    key = (node.start_point[0] + 1, method)
    call = ScopedCall(scope=nodes.node_text(scope_node).strip(), kind=scope_node.type)
    index.scopes.setdefault(key, []).append(call)


def _record(node, index: CallSiteIndex) -> None:
    name_node = node.child_by_field_name("name")
    object_node = node.child_by_field_name("object")
    if name_node is None or object_node is None:
        return
    method = nodes.node_text(name_node)
    if not method:
        return
    # The call node's own start line, because that is what graphify records as the location.
    key = (node.start_point[0] + 1, method)
    index.receivers.setdefault(key, []).append(nodes.node_text(object_node))
