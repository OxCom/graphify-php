"""The configuration pass graphify runs, and the status record that proves it ran.

A `LanguageResolver`, not a `CallTargetSource`. The distinction is not bookkeeping: a
`CallTargetSource` answers "what does this call site call" and is offered sites by the
member-call resolver, while this pass reads files no call site mentions and produces nodes that
do not exist yet. Forcing it into that port would mean inventing call sites to carry facts that
are not calls.

WHY THE SUFFIX GATE SAYS `.php` WHILE THIS READS `.yaml`

graphify activates a registered resolver only when the corpus it is building holds a file with
one of the resolver's suffixes (`resolver_registry.run_language_resolvers`). YAML is not in that
corpus: `detect.py:45` lists `.yaml` and `.yml` in `DOC_EXTENSIONS`, so a YAML file takes the
document path and never reaches the code extractor. Measured on a fixture of one PHP file and one
`config/packages/messenger.yaml`, built with two probe resolvers registered: the `.php`-gated
probe ran, the `.yaml`-gated probe did not, and the published graph held 3 nodes, none of them
from the YAML file.

So a `.yaml` gate would never fire and this pass would be dead code that looks registered. The
gate is `.php` — "this is a PHP repository, so it may be a Symfony application" — and the files
are found by walking `config/` under the repository root rather than by reading the corpus. The
two are deliberately about different files.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from . import status
from .config import (ConfigDocument, ConfigFileFinder, DocumentNodeFactory, FactSet,
                     GraphWriter, SafeDocumentParser, default_readers)
from .config.ports import ConfigFactReader
from .resolver import PHP_SUFFIXES

RESOLVER_NAME = "php_symfony_config"

# Reasons this module owns. A reader's own reasons arrive prefixed with the reader's name, so
# the histogram stays a work list rather than a pile of colliding words.
REASON_NO_FACTS = "document_held_no_indexed_fact"


def run(
    per_file: Sequence[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    *,
    repo_root: str = ".",
    readers: Sequence[ConfigFactReader] | None = None,
    finder: ConfigFileFinder | None = None,
    parser: SafeDocumentParser | None = None,
    writer: GraphWriter | None = None,
) -> status.ResolverStatus:
    """Index the configuration facts under `repo_root`, and record what happened either way."""
    record = status.ResolverStatus(name=RESOLVER_NAME)
    try:
        finder = finder or ConfigFileFinder(repo_root)
        available, why_not = finder.available()
        if not available:
            # Most repositories graphify indexes are not Symfony applications. Recording this
            # as a skip with its reason is what keeps "not applicable here" distinguishable
            # from "registered but silently did nothing", which are the same artifact otherwise.
            record.state = "skipped"
            record.reason = why_not
            return record

        parser = parser or SafeDocumentParser()
        readers = readers if readers is not None else default_readers()
        writer = writer or GraphWriter(all_nodes, all_edges)
        documents = DocumentNodeFactory()

        files = finder.find()
        record.files_seen = len(files)
        record.eligible_sites = len(files)
        for relative_path, absolute_path in files:
            parsed = parser.parse(absolute_path)
            if parsed.data is None:
                # A malformed file costs that file. It is counted by name and the pass carries
                # on, because one bad indent must not decide whether the rest is indexed.
                record.unresolvable(f"{parser.__class__.__name__.lower()}:{parsed.reason}")
                continue
            document = ConfigDocument(path=relative_path, data=parsed.data)
            collected = FactSet()
            for reader in readers:
                facts = reader.read(document)
                for reason, count in facts.unresolved.items():
                    record.unresolvable(f"{reader.name}:{reason}", count)
                facts.unresolved.clear()
                collected.extend(facts)
            if not collected.nodes and not collected.edges:
                record.unresolvable(REASON_NO_FACTS)
                continue
            # The file node goes in ahead of the readers' own, because every edge they produce
            # starts at it and the writer resolves an endpoint only to a node already written.
            collected.nodes.insert(0, documents.node(relative_path))
            writer.write(collected)
            record.resolved_sites += 1

        for reason, count in writer.unresolved.items():
            record.unresolvable(reason, count)
        record.edges_added = writer.edges_added
        record.state = "completed"
        return record
    except Exception as exc:
        record.state = "failed"
        record.reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record.finished_at = time.time()
        status.write([record])


def make_resolver(repo_root: str = ".",
                  readers_factory: Callable[[], Sequence[ConfigFactReader]] = default_readers):
    """Wrap `run` in the `(per_file, all_nodes, all_edges) -> None` shape graphify calls.

    The readers are assembled at resolution time, not at registration time: registration happens
    at import, before the repository under build is settled.
    """
    def resolve(per_file, all_nodes, all_edges) -> None:
        run(per_file, all_nodes, all_edges, repo_root=repo_root, readers=readers_factory())

    resolve.__name__ = RESOLVER_NAME
    return resolve


def register(repo_root: str = ".") -> bool:
    """Register this pass with graphify. Returns whether this call did the registering.

    Idempotent by name: graphify runs its registry in order without deduplicating, so a double
    registration would run the pass twice and the second run would find every node already
    present and report a build that did nothing.
    """
    from graphify import resolver_registry

    if any(r.name == RESOLVER_NAME for r in resolver_registry.registered_resolvers()):
        return False
    resolver_registry.register(resolver_registry.LanguageResolver(
        name=RESOLVER_NAME,
        suffixes=PHP_SUFFIXES,
        resolve=make_resolver(repo_root),
    ))
    return True
