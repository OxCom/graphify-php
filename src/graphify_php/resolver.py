"""The resolution pass graphify runs, expressed against `ports` alone.

This module imports nothing from `syntax` or `sources`. It is handed a list of `CallTargetSource`
and asks them in order; adding a PHPStan source, a language server or an LSIF index later is a
change to the factory that assembles that list, not to anything here.

Precedence is the whole reason the list is ordered. A precise source that ran the project's own
type engine and a parser that read a declaration will both answer `$this->emails`, and they will
not always agree. The first source that proves a site owns it, and that site is never offered to
the next one — so a later, weaker source can add reach but can never overwrite a stronger
source's answer.

Every path through this function ends in a status record, including the exception path, because
graphify's driver catches whatever escapes:

    except Exception as exc:
        _LOG.warning("%s resolution failed, skipping: %s", resolver.name, exc)

The warning goes to graphify's logger. Nothing that runs the build reads it, so without the
record written below a total failure of this pass and a PHP codebase with few calls produce the
same artifact.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Sequence, runtime_checkable

from . import status
from .ports import CallSite, CallTarget, CallTargetSource, EdgeSink, NodeIndex

RESOLVER_NAME = "php_member_calls"
PHP_SUFFIXES = frozenset({".php"})

# The unresolved-reason vocabulary this module owns. Reasons a source reports are prefixed with
# that source's name, so the two namespaces cannot collide and the histogram stays a work list.
REASON_NO_SOURCE = "no_source_resolved"
REASON_NOT_IN_GRAPH = "target_not_in_graph"
REASON_SELF_CALL = "self_call"
REASON_DUPLICATE_TARGET = "multiple_targets_for_site"
REASON_SCOPE_NOT_RECOVERED = "scoped_method_not_recovered"
REASON_SCOPE_AMBIGUOUS = "scoped_method_ambiguous"

_IDENTIFIER = re.compile(r"^[A-Za-z_\\][A-Za-z0-9_\\]*$")


@runtime_checkable
class ScopeIndex(Protocol):
    """What a scoped call needs from the graph, kept apart from `ports.NodeIndex`.

    `Helper::format()` reaches graphify as callee `Helper` with the method name dropped
    (extractors/engine.py, the `scoped_call_expression` arm), so a scoped site arrives without
    the one field `CallSite` exists to carry. The graph knows which methods a class declares,
    and a type source can check a candidate against the line it came from, so the name is
    recoverable without this module parsing PHP.

    Declared here rather than added to `ports.NodeIndex` because `ports` is another author's
    file; it belongs there, and the note in the report says so.
    """

    def scope_methods(self, caller_nid: str, scope: str) -> list[str]:
        ...


@dataclass
class ScopedGroup:
    """One scoped call site, and the candidate method names offered on its behalf.

    A group is ONE eligible site however many candidates it takes to find its method, which is
    why the candidates are accounted separately: counting nine refused probes as nine failures
    would bury the real unresolved reasons under noise of our own making.
    """

    scope: str
    candidates: list[CallSite]


def collect_sites(per_file: Sequence[dict]) -> list[CallSite]:
    """Every `$receiver->method()` graphify left unresolved, as `CallSite`.

    graphify's PHP extractor sets `is_member_call` and the callee name but leaves `receiver`
    unset (extractors/engine.py, the `tree_sitter_php` branch of the call walk: unlike the C#,
    Java and C++ branches it never assigns `member_receiver`). The field is carried through
    when it is there and left empty when it is not; a source that needs the receiver expression
    re-reads it from `source_file` at `source_location`, which it must parse anyway.
    """
    sites: list[CallSite] = []
    for result in per_file:
        for raw in result.get("raw_calls", []) or []:
            if not raw.get("is_member_call"):
                continue
            caller, callee = raw.get("caller_nid"), raw.get("callee")
            source_file = raw.get("source_file") or ""
            if not caller or not callee or not source_file.endswith(".php"):
                continue
            sites.append(CallSite(
                caller_nid=caller,
                receiver=raw.get("receiver") or "",
                method=callee,
                source_file=source_file,
                source_location=raw.get("source_location") or "",
            ))
    return sites


def collect_scoped_groups(per_file: Sequence[dict], scopes: ScopeIndex) -> list[ScopedGroup]:
    """Scoped calls — `self::`, `parent::`, `static::`, `Class::` — as candidate sites.

    graphify records all four with `is_member_call` false and the SCOPE as the callee, the same
    shape it uses for an ordinary function call. They are told apart by asking the graph whether
    the callee names a class with methods: `trim` and `error_log` name no class, so they produce
    no candidates and drop out without a hardcoded list of PHP builtins.
    """
    groups: list[ScopedGroup] = []
    for result in per_file:
        for raw in result.get("raw_calls", []) or []:
            if raw.get("is_member_call"):
                continue
            caller, scope = raw.get("caller_nid"), raw.get("callee")
            source_file = raw.get("source_file") or ""
            if not caller or not scope or not source_file.endswith(".php"):
                continue
            if not _IDENTIFIER.match(str(scope)):
                continue
            candidates = [
                CallSite(caller_nid=caller, receiver=str(scope), method=name,
                         source_file=source_file, source_location=raw.get("source_location") or "")
                for name in scopes.scope_methods(caller, str(scope))
            ]
            if candidates:
                groups.append(ScopedGroup(scope=str(scope), candidates=candidates))
    return groups


def _php_files(per_file: Sequence[dict]) -> set[str]:
    seen: set[str] = set()
    for result in per_file:
        for group in ("nodes", "raw_calls"):
            for item in result.get(group, []) or []:
                path = item.get("source_file") or ""
                if path.endswith(".php"):
                    seen.add(path)
    return seen


def _ask_sources(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    sites: Sequence[CallSite],
    record: status.ResolverStatus,
    reason_prefix: str = "",
    count_unclaimed: bool = True,
) -> list[CallTarget]:
    """Offer each site to the sources in order, stopping at the first that proves it."""
    pending: list[CallSite] = list(sites)
    targets: list[CallTarget] = []
    for source in sources:
        if not pending:
            break
        resolution = source.resolve(repo_root, pending)
        record.files_seen = max(record.files_seen, resolution.files_seen)
        for reason, count in (resolution.unresolved or {}).items():
            record.unresolvable(f"{reason_prefix}{source.name}:{reason}", count)

        claimed: set[CallSite] = set()
        for target in resolution.targets:
            if target.site in claimed:
                # Two answers for one site from one source. Keeping the first is arbitrary,
                # so the extra is counted rather than quietly dropped.
                record.unresolvable(REASON_DUPLICATE_TARGET)
                continue
            claimed.add(target.site)
            targets.append(target)
        pending = [site for site in pending if site not in claimed]

    if pending and count_unclaimed:
        record.unresolvable(REASON_NO_SOURCE, len(pending))
    return targets


def _resolve_groups(
    groups: Sequence[ScopedGroup],
    sources: Sequence[CallTargetSource],
    repo_root: str,
    record: status.ResolverStatus,
) -> list[CallTarget]:
    """Turn each group's candidates into at most one target for that call site.

    A separate pass rather than one flat list with the member sites, so the probes' refusals
    land under their own `probe:` prefix. Mixed in, a class with nine other methods would report
    nine `call-site-not-found` against every scoped call and make the histogram useless.
    """
    if not groups:
        return []
    probes = [site for group in groups for site in group.candidates]
    found = {t.site: t for t in _ask_sources(sources, repo_root, probes, record,
                                             reason_prefix="probe:", count_unclaimed=False)}
    targets: list[CallTarget] = []
    for group in groups:
        hits = [found[site] for site in group.candidates if site in found]
        if len(hits) == 1:
            targets.append(hits[0])
        elif not hits:
            record.unresolvable(REASON_SCOPE_NOT_RECOVERED)
        else:
            # Two candidate names both verified against the same line. One of them is wrong and
            # nothing here says which, so the site gets no edge.
            record.unresolvable(REASON_SCOPE_AMBIGUOUS)
    return targets


def _write_edges(
    targets: Iterable[CallTarget],
    index: NodeIndex,
    sink: EdgeSink,
    record: status.ResolverStatus,
) -> None:
    # `ports.NodeIndex` takes no caller, so an index that can apply the caller's own visibility
    # offers a wider call and is used when present. The report asks for the caller to be added
    # to the port; until then this keeps `ports` untouched and fake indexes in tests working.
    origin_aware = getattr(index, "method_node_for", None)
    for target in targets:
        callee_nid = (origin_aware(target.site.caller_nid, target.class_fqn, target.method)
                      if callable(origin_aware)
                      else index.method_node(target.class_fqn, target.method))
        if callee_nid is None:
            # `NodeIndex` answers one question, so a missing class and a missing method arrive
            # here indistinguishable. Widening the port to tell them apart would tie this
            # package to graphify's node schema, which is the part most likely to move.
            record.unresolvable(REASON_NOT_IN_GRAPH)
            continue
        if callee_nid == target.site.caller_nid:
            record.unresolvable(REASON_SELF_CALL)
            continue
        sink.add_call(target.site.caller_nid, callee_nid, target)


def run(
    per_file: Sequence[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    *,
    sources: Sequence[CallTargetSource],
    repo_root: str = ".",
    index: NodeIndex | None = None,
    sink: EdgeSink | None = None,
    scopes: ScopeIndex | None = None,
) -> status.ResolverStatus:
    """Resolve PHP member calls into `calls` edges, and record what happened either way."""
    record = status.ResolverStatus(name=RESOLVER_NAME)
    try:
        if index is None or sink is None:
            from .graph_adapter import GraphEdgeSink, GraphNodeIndex
            built = GraphNodeIndex(all_nodes, all_edges)
            index = index or built
            sink = sink or GraphEdgeSink(all_edges, built)
        if scopes is None and isinstance(index, ScopeIndex):
            scopes = index

        unavailable: list[str] = []
        usable: list[CallTargetSource] = []
        for source in sources:
            ok, reason = source.available(repo_root)
            if ok:
                usable.append(source)
            else:
                unavailable.append(f"{source.name}: {reason}")

        record.files_seen = len(_php_files(per_file))
        if not usable:
            # A repository with no PHPStan in `vendor/` is the normal case, not a fault: the
            # build publishes, and the record says which layer was missing.
            record.state = "skipped"
            record.reason = "; ".join(unavailable) or "no call target sources configured"
            return record

        sites = collect_sites(per_file)
        groups = collect_scoped_groups(per_file, scopes) if scopes is not None else []
        record.eligible_sites = len(sites) + len(groups)
        targets = _ask_sources(usable, repo_root, sites, record)
        targets += _resolve_groups(groups, usable, repo_root, record)
        record.resolved_sites = len({t.site for t in targets})
        _write_edges(targets, index, sink, record)
        record.edges_added = getattr(sink, "added", record.resolved_sites)
        record.state = "completed"
        if unavailable:
            record.reason = "; ".join(unavailable)
        return record
    except Exception as exc:
        record.state = "failed"
        record.reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record.finished_at = time.time()
        status.write([record])


def make_resolver(sources_factory: Callable[[str], Sequence[CallTargetSource]],
                  repo_root: str = "."):
    """Wrap `run` in the `(per_file, all_nodes, all_edges) -> None` shape graphify calls.

    The factory is called at resolution time, not at registration time: registering happens at
    import, when the working directory and the repository under build are not yet settled.
    """
    def resolve(per_file, all_nodes, all_edges) -> None:
        run(per_file, all_nodes, all_edges,
            sources=sources_factory(repo_root), repo_root=repo_root)

    resolve.__name__ = RESOLVER_NAME
    return resolve
