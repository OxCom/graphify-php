"""Environment references: which variable NAME a configuration file reads.

`client_secret: '%env(MICROSOFT_CLIENT_SECRET)%'` is the only place that binds the variable to
the service that consumes it. No PHP names it — the container resolves the placeholder before
any code runs — so "what breaks if this variable is unset" has no answer in a graph built from
PHP alone.

The name is indexed. The value is never touched, in any form: not the default inside
`%env(default:app.fallback:NAME)%`, not the DSN the placeholder is embedded in, not the result
of resolving it. A configuration file is where credentials live, and a graph is published,
diffed and served to other tools. `eval/canaries.py` plants fake secrets in fixtures and asserts
they reach no artifact; this reader is the one that would break that if it kept a value.

Processor forms are Symfony's: `%env(bool:DEBUG)%`, `%env(default:fallback_param:DSN)%`,
`%env(key:secret:JSON_FILE)%`. Every one of them puts the variable name last, so the innermost
name is the final colon-separated segment.
"""

from __future__ import annotations

import re
from typing import Iterator

from .document import file_key
from .facts import ConfigEdge, ConfigNode, ConfigDocument, FactSet, NodeRef

RELATION_READS_ENV = "reads_env"

REASON_UNRECOGNISED_NAME = "env_name_not_an_identifier"

# `[^()%]*` on purpose: a placeholder cannot contain a nested `%env(` and stopping at the first
# `%` keeps a run-on string like `'%env(A)%:%env(B)%'` as two matches instead of one wrong one.
_PLACEHOLDER = re.compile(r"%env\(([^()%]*)\)%")

# The name as an environment variable can actually be spelled. A segment that fails this is a
# processor argument this reader does not model, and emitting it would put a made-up variable
# in the graph.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Configuration nests a few levels. The bound stops a generated or vendored dump from turning
# one file into an unbounded walk.
_MAX_DEPTH = 12


class EnvReferenceReader:
    """Reads `%env(...)%` placeholders out of one document, names only."""

    name = "env_references"

    def read(self, document: ConfigDocument) -> FactSet:
        facts = FactSet()
        file_ref = NodeRef.config(file_key(document.path))
        seen: set[str] = set()
        for placeholder in self._placeholders(document.data, _MAX_DEPTH):
            name = placeholder.rsplit(":", 1)[-1].strip()
            if not _NAME.match(name):
                facts.unresolvable(REASON_UNRECOGNISED_NAME)
                continue
            if name in seen:
                continue
            seen.add(name)
            node_key = env_key(name)
            facts.nodes.append(ConfigNode(key=node_key, label=name, kind="env_var",
                                          source_file=document.path))
            facts.edges.append(ConfigEdge(source=file_ref, target=NodeRef.config(node_key),
                                          relation=RELATION_READS_ENV,
                                          source_file=document.path))
        return facts

    def _placeholders(self, node, depth: int) -> Iterator[str]:
        """Every placeholder body in the document, keys included.

        Only the text inside `env(...)` ever leaves this method. The scalar that carried it —
        which may be a DSN with an inline password — is not yielded, not stored and not logged.
        """
        if depth < 0:
            return
        if isinstance(node, str):
            yield from _PLACEHOLDER.findall(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str):
                    yield from _PLACEHOLDER.findall(key)
                yield from self._placeholders(value, depth - 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from self._placeholders(item, depth - 1)


def env_key(name: str) -> str:
    """Node key for a variable, shared across every file that reads it.

    One variable is one node however many files reference it: the question this answers is
    "everything that breaks when `MICROSOFT_CLIENT_SECRET` is unset", which a per-file node
    would split into as many answers as there are files.
    """
    return f"cfg::env::{name}"
