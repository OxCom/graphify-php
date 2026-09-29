"""Message routing: which transport carries which message class.

Nothing in PHP states this. The sender calls `$bus->dispatch($message)` and the handler declares
`__invoke(TimezoneInitializedMessage $m)`; the transport between them exists only as a key in
`messenger.yaml`. A graph built from PHP alone has the sender and the handler as unconnected
islands, and `rg 'TimezoneInitializedMessage'` returns all three files without saying which is
which.

The reader emits the transport as a node in its own right and the message class as the edge
source, so "what does `cron` carry" and "where does this message go" are the same lookup from
two ends.
"""

from __future__ import annotations

import re
from typing import Iterator

from .document import file_key
from .facts import ConfigEdge, ConfigNode, ConfigDocument, FactSet, NodeRef

RELATION_ROUTED_TO = "routed_to"
RELATION_DECLARES = "declares_transport"

REASON_NOT_A_CLASS = "routing_key_is_not_a_class"
REASON_NO_TRANSPORT = "routing_entry_names_no_transport"

# Symfony accepts `'App\Foo': async`, `'App\Foo': [async, audit]` and
# `'App\Foo': {senders: [async]}`. The third form is the documented long form of the first two.
_SENDERS_KEY = "senders"

# A routing key is a class or interface FQN, possibly with a trailing `*` wildcard. Anything
# else in that position is a Symfony feature this reader does not model, and guessing would put
# a fabricated class on the edge.
_CLASS_KEY = re.compile(r"^\\?[A-Za-z_\x80-\xff][\w\x80-\xff]*(\\[A-Za-z_\x80-\xff][\w\x80-\xff]*)*$")

# How deep to look for a `messenger` block. Symfony nests it at `framework.messenger` and under
# an environment guard at `when@test.framework.messenger`; three is one more than the deepest
# real form, and the bound is what keeps a large document from being walked in full.
_MAX_DEPTH = 3


class MessageRoutingReader:
    """Reads `messenger` routing tables out of one document."""

    name = "message_routing"

    def read(self, document: ConfigDocument) -> FactSet:
        facts = FactSet()
        tables = list(self._routing_tables(document.data))
        if not tables:
            return facts

        file_ref = NodeRef.config(file_key(document.path))
        for table in tables:
            for key, value in table.items():
                transports = list(self._transports(value))
                if not transports:
                    facts.unresolvable(REASON_NO_TRANSPORT)
                    continue
                for transport in transports:
                    node_key = transport_key(transport)
                    facts.nodes.append(ConfigNode(
                        key=node_key, label=transport, kind="transport",
                        source_file=document.path,
                    ))
                    # The file-to-transport edge is written whether or not the message class is
                    # in the graph. A transport whose message lives in a package this build did
                    # not index is still a transport this application declares, and dropping the
                    # node would report the application as having fewer than it has.
                    facts.edges.append(ConfigEdge(
                        source=file_ref, target=NodeRef.config(node_key),
                        relation=RELATION_DECLARES, source_file=document.path,
                    ))
                    class_fqn = str(key).lstrip("\\")
                    if not _CLASS_KEY.match(class_fqn):
                        facts.unresolvable(REASON_NOT_A_CLASS)
                        continue
                    facts.edges.append(ConfigEdge(
                        source=NodeRef.php_class(class_fqn),
                        target=NodeRef.config(node_key),
                        relation=RELATION_ROUTED_TO, source_file=document.path,
                    ))
        return facts

    def _routing_tables(self, data: dict) -> Iterator[dict]:
        """Every `routing` mapping this document declares.

        Both shapes are accepted because both occur: the full `framework.messenger.routing`
        path an application file uses, and a bare top-level `routing` in a file that is already
        scoped to messenger by an import.
        """
        top = data.get("routing")
        if isinstance(top, dict):
            yield top
        for block in self._messenger_blocks(data, _MAX_DEPTH):
            table = block.get("routing")
            if isinstance(table, dict):
                yield table

    def _messenger_blocks(self, node, depth: int) -> Iterator[dict]:
        if depth < 0 or not isinstance(node, dict):
            return
        for key, value in node.items():
            if not isinstance(value, dict):
                continue
            if key == "messenger":
                yield value
            else:
                yield from self._messenger_blocks(value, depth - 1)

    def _transports(self, value) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield item
        elif isinstance(value, dict):
            yield from self._transports(value.get(_SENDERS_KEY))


def transport_key(name: str) -> str:
    """Node key for a transport, shared by every file that names it.

    Two files routing to `cron` describe one transport. Keying by name is what keeps them one
    node, so "everything on cron" is a single neighbourhood rather than one per file.
    """
    return f"cfg::transport::{name}"
