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

import dataclasses
import inspect
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from . import core_guard, status
from .ports import (
    CallerSites,
    CallSite,
    CallTarget,
    CallTargetSource,
    EdgeSink,
    EngineCall,
    LineProof,
    MethodBoundCall,
    NodeIndex,
    ScopeIndex,
)

RESOLVER_NAME = "php_member_calls"
PHP_SUFFIXES = frozenset({".php"})

# "1" mints a body-less `external` node per vendor class a proven call targets, instead of
# dropping the record. Off by default: graphify core refuses stubs for `calls` because they
# become hub nodes; this plugin opts in per build.
EXTERNAL_STUBS_ENV = "GRAPHIFY_PHP_EXTERNAL_STUBS"

# The unresolved-reason vocabulary this module owns. Reasons a source reports are prefixed with
# that source's name, so the two namespaces cannot collide and the histogram stays a work list.
REASON_NO_SOURCE = "no_source_resolved"
REASON_NOT_IN_GRAPH = "target_not_in_graph"
REASON_SELF_CALL = "self_call"
REASON_SCOPE_NOT_RECOVERED = "scoped_method_not_recovered"
REASON_SCOPE_AMBIGUOUS = "scoped_method_ambiguous"

# Why an engine edge (graphify's bare-name binding in the caller's file) was kept, and the one
# reason it is dropped. Kept reasons are checked in this order and the first that applies wins.
REASON_ENGINE_NO_PROOFS = "engine-proof-unavailable"  # no source holds per-line records
REASON_ENGINE_NO_RECORD = "engine-no-record"  # no record in the caller at the edge's line
REASON_ENGINE_AGREES = "engine-proof-agrees"
REASON_ENGINE_RECORDS_DISAGREE = "engine-records-disagree"  # some records agree, some do not
REASON_ENGINE_UNPLACED = "engine-proof-unplaced"  # a record's body cannot be compared
REASON_ENGINE_SITES_UNKNOWN = "engine-sites-unavailable"  # the caller's sites cannot be listed
REASON_ENGINE_SITE_NOT_FOUND = "engine-site-not-found"  # no `->m(` on the edge's own line
REASON_ENGINE_OTHER_SITE = "engine-other-site-unproved"  # a site the edge may stand for stands
REASON_ENGINE_NOT_OWNED = "engine-edge-list-not-owned"  # removal would not reach the graph
# graphify binds functions and methods by the same bare name. A dot-less target is a function
# or an anonymous class's method (or one whose class a parse error lost); a parser tells which.
REASON_ENGINE_TARGET_NOT_METHOD = "engine-target-not-a-method"  # a function is declared there
REASON_ENGINE_TARGET_UNKNOWN = "engine-target-kind-unknown"  # no parser, or it cannot tell
# A function or scoped call of that name in the caller: the edge may stand for it.
REASON_ENGINE_OTHER_CALL_KIND = "engine-other-call-kind"
REASON_ENGINE_DROPPED = "engine-contradicts-proof"
ENGINE_REASON_PREFIX = "engine:"
# Why a proved target of a dropped engine edge got no edge, under `ENGINE_REASON_PREFIX`. A type
# engine files a closure's calls under the method around it, so a record on a line the caller
# does not own alone may be another body's call.
REASON_ENGINE_PROOF_NESTED = "proof-in-nested-callable"  # a nested callable starts before it
REASON_ENGINE_PROOF_SHARED_LINE = "proof-on-shared-line"  # another body's call of it is there
# The declaring file holds the proved class and no method node of that name (`@method`, `__call`).
REASON_ENGINE_METHOD_NO_NODE = "method-not-in-graph"

# Why a graphify edge from a `new X(` line to a method named x was kept. Checked in this order.
REASON_NEW_SITE_NOT_LOCATED = "new-site-not-located"  # the line could not be read or parsed
REASON_NEW_SITE_SHARED_LINE = "new-site-shared-line"  # a call of that name is on the line too
REASON_NEW_SITE_NOT_OWNED = "new-site-edge-list-not-owned"  # removal would not reach the graph

# The nullsafe pass's own reasons. A parser's refusals arrive as `nullsafe:<source>:<reason>`.
NULLSAFE_REASON_PREFIX = "nullsafe:"
REASON_NULLSAFE_CALLER_NOT_IN_GRAPH = "caller-not-in-graph"  # no one graph callable starts there
REASON_NULLSAFE_NO_RECORD = "no-record-in-caller"  # no type-engine record of it in that caller


# The record's schema lives in `status`; the name stays importable from here.
MemberCallStatus = status.MemberCallStatus

_IDENTIFIER = re.compile(r"^[A-Za-z_\\][A-Za-z0-9_\\]*$")


def is_vendor_file(path: str, repo_root: str) -> bool:
    """Whether `path` sits under the repository's `vendor/` directory.

    Only the first segment under the root counts. A project class the build left out (a
    `Tests\\` class under `.graphifyignore`) is still project code and gets no stub for it.
    """
    if not path:
        return False
    root = os.path.abspath(repo_root)
    relative = os.path.relpath(os.path.join(root, path), root)
    return relative.replace("\\", "/").split("/", 1)[0] == "vendor"


@dataclass
class ScopedGroup:
    """One scoped call site, and the candidate method names offered on its behalf.

    A group is ONE eligible site however many candidates it takes to find its method, which is
    why the candidates are accounted separately: counting nine refused probes as nine failures
    would bury the real unresolved reasons under noise of our own making.
    """

    scope: str
    candidates: list[CallSite]


def collect_sites(per_file: Sequence[dict], raws: list[dict] | None = None) -> list[CallSite]:
    """Every `$receiver->method()` graphify left unresolved, as `CallSite`.

    The `receiver` field is carried through verbatim and is not a PHP expression a source can
    trust: graphify before 0.9.103 left it unset for PHP, and 0.9.103 fills it with a bare name
    (`mailer` for both `$this->mailer` and `$mailer`). A source that needs the receiver
    expression re-reads it from `source_file` at `source_location`, which it must parse anyway.
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
            sites.append(
                CallSite(
                    caller_nid=caller,
                    receiver=raw.get("receiver") or "",
                    method=callee,
                    source_file=source_file,
                    source_location=raw.get("source_location") or "",
                    # 0.9.103 sends `Foo::get()` as a member call flagged `php_scoped`.
                    scoped=bool(raw.get("php_scoped")),
                )
            )
            if raws is not None:
                raws.append(raw)
    return sites


def collect_scoped_groups(
    per_file: Sequence[dict], scopes: ScopeIndex, raws: list[dict] | None = None
) -> list[ScopedGroup]:
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
                CallSite(
                    caller_nid=caller,
                    receiver=str(scope),
                    method=name,
                    source_file=source_file,
                    source_location=raw.get("source_location") or "",
                    scoped=True,
                )
                for name in scopes.scope_methods(caller, str(scope))
            ]
            if candidates:
                groups.append(ScopedGroup(scope=str(scope), candidates=candidates))
                if raws is not None:
                    raws.append(raw)
    return groups


def collect_corpus(per_file: Sequence[dict]) -> list[str]:
    """Every PHP file graphify parsed, as the corpus a source may read declarations from.

    A leaf service class has nothing unresolved in it, so it never appears among the sites and
    a source indexing only those files cannot tell whether it declares the method being called
    on it. The fixture shows the shape exactly: `Emails.php` contributes nodes but no
    `raw_calls`, and it is the class every resolved call points at.

    Bounded by what the build already parsed, never by a filesystem walk: a walk to the nearest
    `composer.json` measured 4.7 s against 0.16 s and read paths outside the build.

    Paths are taken verbatim from the same dicts the sites come from. During a build they are
    ABSOLUTE and only relativized at publish time, so rebuilding them from a root here would
    hand the source the same file under two names and make it parse it twice.
    """
    seen: set[str] = set()
    for result in per_file:
        for group in ("nodes", "raw_calls"):
            for item in result.get(group, []) or []:
                path = item.get("source_file") or ""
                if path.endswith(".php"):
                    seen.add(path)
    return sorted(seen)


# Whether a source's `resolve` takes `corpus`, cached per source class. The shim exists because
# `corpus` was added to `CallTargetSource` after the first sources were written, and a source
# built against the old signature is still a valid implementation of the protocol. Reading the
# signature rather than catching `TypeError` on the call: a `TypeError` raised from inside a
# source's own body would otherwise be misread as "this source is old" and silently retried
# without the corpus.
_ACCEPTS_CORPUS: dict[type, bool] = {}


def _resolve_with(
    source: CallTargetSource,
    repo_root: str,
    sites: Sequence[CallSite],
    corpus: Sequence[str] | None,
):
    kind = type(source)
    accepts = _ACCEPTS_CORPUS.get(kind)
    if accepts is None:
        try:
            params = inspect.signature(source.resolve).parameters
        except (TypeError, ValueError):
            accepts = False
        else:
            accepts = "corpus" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        _ACCEPTS_CORPUS[kind] = accepts
    if corpus is not None and accepts:
        return source.resolve(repo_root, sites, corpus)
    return source.resolve(repo_root, sites)


def _ask_sources(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    sites: Sequence[CallSite],
    record: status.ResolverStatus,
    reason_prefix: str = "",
    count_unclaimed: bool = True,
    corpus: Sequence[str] | None = None,
) -> list[CallTarget]:
    """Offer each site to the sources in order, stopping at the first that proves it."""
    pending: list[CallSite] = list(sites)
    targets: list[CallTarget] = []
    for source in sources:
        if not pending:
            break
        resolution = _resolve_with(source, repo_root, pending, corpus)
        record.files_seen = max(record.files_seen, resolution.files_seen)
        for reason, count in (resolution.unresolved or {}).items():
            record.unresolvable(f"{reason_prefix}{source.name}:{reason}", count)

        # A site keeps every target a source proved for it: a `Emails|Cache` receiver has two
        # real arms, and one arm alone is a wrong answer. Only an exact repeat is dropped.
        claimed: set[CallSite] = set()
        seen: set[tuple[CallSite, str, str]] = set()
        for target in resolution.targets:
            key = (target.site, target.class_fqn, target.method)
            if key in seen:
                continue
            seen.add(key)
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
    corpus: Sequence[str] | None = None,
) -> list[CallTarget]:
    """Turn each group's candidates into at most one target for that call site.

    A separate pass rather than one flat list with the member sites, so the probes' refusals
    land under their own `probe:` prefix. Mixed in, a class with nine other methods would report
    nine `call-site-not-found` against every scoped call and make the histogram useless.
    """
    if not groups:
        return []
    probes = [site for group in groups for site in group.candidates]
    found: dict[CallSite, list[CallTarget]] = {}
    for target in _ask_sources(
        sources,
        repo_root,
        probes,
        record,
        reason_prefix="probe:",
        count_unclaimed=False,
        corpus=corpus,
    ):
        found.setdefault(target.site, []).append(target)
    targets: list[CallTarget] = []
    for group in groups:
        hits = [target for site in group.candidates for target in found.get(site, ())]
        if len(hits) == 1:
            targets.append(hits[0])
        elif not hits:
            record.unresolvable(REASON_SCOPE_NOT_RECOVERED)
        else:
            # Two candidate names verified against one line, or one name with two targets.
            # Either way nothing here says which body is called, so the site gets no edge.
            record.unresolvable(REASON_SCOPE_AMBIGUOUS)
    return targets


def _write_edges(
    targets: Iterable[CallTarget],
    index: NodeIndex,
    sink: EdgeSink,
    record: status.ResolverStatus,
    stubs=None,
    repo_root: str = ".",
    reason_prefix: str = "",
) -> set[CallSite]:
    """Write each target's edge, to a method or a vendor stub; return the sites that got one.

    A site whose pair the graph already held still counts: its call is in the graph.
    """
    # `ports.NodeIndex` takes no caller, so an index that can check the proof's own file, or
    # at least the caller's visibility, offers a wider call and is used when present. The
    # narrow call keeps `ports` untouched and fake indexes in tests working.
    proved = getattr(index, "proved_node_for", None)
    origin_aware = getattr(index, "method_node_for", None)
    bound: set[CallSite] = set()
    # Counted per target record, not per site: a union site with one arm missing from the graph
    # still gets its other arm, and the histogram says how many records were lost.
    for target in targets:
        if callable(proved):
            callee_nid = proved(target.site.caller_nid, target, repo_root)
        elif callable(origin_aware):
            callee_nid = origin_aware(target.site.caller_nid, target.class_fqn, target.method)
        else:
            callee_nid = index.method_node(target.class_fqn, target.method)
        if callee_nid is None:
            # Only a class proven to live under `vendor/` gets a stub. Project code the build
            # excluded is still project code, and a stub for it would shadow the real class.
            if stubs is not None and is_vendor_file(target.declaring_file, repo_root):
                stubs.add_call(target.site.caller_nid, stubs.node_for(target.class_fqn), target)
                record.external_stubbed += 1
                bound.add(target.site)
                continue
            # `NodeIndex` answers one question, so a missing class and a missing method arrive
            # here indistinguishable. Widening the port to tell them apart would tie this
            # package to graphify's node schema, which is the part most likely to move.
            record.unresolvable(reason_prefix + REASON_NOT_IN_GRAPH)
            continue
        if callee_nid == target.site.caller_nid:
            record.unresolvable(reason_prefix + REASON_SELF_CALL)
            continue
        sink.add_call(target.site.caller_nid, callee_nid, target)
        bound.add(target.site)
    return bound


def _proved_raws(
    sites: Sequence[CallSite],
    site_raws: Sequence[dict],
    groups: Sequence[ScopedGroup],
    group_raws: Sequence[dict],
    claimed: set[CallSite],
) -> list[dict]:
    """The raw calls behind every claimed site or group, each once, by identity.

    Positional: `collect_*` append one raw per site or group, and `_with_caller_scope` keeps order.
    Walking the pairs rather than a dict keyed by `CallSite` keeps two raws with equal sites apart.
    """
    proved: dict[int, dict] = {}
    for site, raw in zip(sites, site_raws, strict=True):
        if site in claimed:
            proved[id(raw)] = raw
    for group, raw in zip(groups, group_raws, strict=True):
        if any(candidate in claimed for candidate in group.candidates):
            proved[id(raw)] = raw
    return list(proved.values())


def _with_caller_scope(
    index: NodeIndex, sites: list[CallSite], groups: list[ScopedGroup]
) -> tuple[list[CallSite], list[ScopedGroup]]:
    """Stamp each site with the class methods its caller can be reported as.

    Optional on the index, as `method_node_for` is: `ports.NodeIndex` stays one question, and
    a site without the stamp is matched exactly as before.
    """
    describe: Callable[[str], Iterable[str]] | None = getattr(index, "caller_scope", None)
    owns: Callable[[str, str], bool] | None = getattr(index, "caller_owns_line", None)
    if not callable(describe):
        return sites, groups
    known: dict[str, tuple[str, ...]] = {}

    def stamp(site: CallSite) -> CallSite:
        if site.caller_nid not in known:
            known[site.caller_nid] = tuple(describe(site.caller_nid))
        owns_line = bool(owns(site.caller_nid, site.source_location)) if callable(owns) else False
        return dataclasses.replace(site, caller_scope=known[site.caller_nid], owns_line=owns_line)

    return [stamp(s) for s in sites], [
        ScopedGroup(scope=g.scope, candidates=[stamp(c) for c in g.candidates]) for g in groups
    ]


def _unsent_calls(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    groups: Sequence[ScopedGroup],
    members: Sequence[CallSite],
) -> list[CallTarget]:
    """Calls graphify never sent, found on the line of a scoped site.

    The scoped site keeps its own outcome, resolved or refused; it only proves its caller has
    code on that line. 0.9.103 sends `Foo::m()` as a `php_scoped` member site, older graphify
    as a group. Optional on a source, as `corpus` is: only per-line records can answer.
    """
    anchors = [group.candidates[0] for group in groups if group.candidates]
    anchors += [site for site in members if site.scoped]
    members = [site for site in members if not site.scoped]
    found: list[CallTarget] = []
    if not anchors:
        return found
    for source in sources:
        ask: Callable[..., Iterable[CallTarget]] | None = getattr(source, "unsent_calls", None)
        if callable(ask):
            found.extend(ask(repo_root, anchors, members))
    return found


def _takes_nullsafe(ask: Callable) -> bool:
    """Whether an `unsent_calls` accepts `nullsafe`, read from its signature as `corpus` is."""
    try:
        return "nullsafe" in inspect.signature(ask).parameters
    except (TypeError, ValueError):
        return False


def _nullsafe_calls(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    corpus: Sequence[str],
    index: NodeIndex,
    sink: EdgeSink,
    record: MemberCallStatus,
    stubs=None,
) -> None:
    """Edge `$x?->m()` calls, which graphify never sends, from the caller a parser placed.

    The `unsent_calls` anchor exists to name the caller; here the parser names it instead: the
    innermost callable around the call, found in the graph by the line it starts on. The parser
    refuses a line holding another call of that name that is not nullsafe (that call has its
    own raw site and fate) and one whose calls of that name sit in different callables. The
    type source then takes only records of that name made inside that caller.
    """
    find = _first_offered(sources, "nullsafe_calls")
    place = getattr(index, "callable_at", None)
    if find is None or not callable(place):
        return
    finder = getattr(getattr(find, "__self__", None), "name", "")
    sites: list[CallSite] = []
    for call in find(repo_root, corpus):
        record.nullsafe_sites_total += 1
        if call.refusal:
            record.unresolvable(f"{NULLSAFE_REASON_PREFIX}{finder}:{call.refusal}")
            continue
        caller = place(call.source_file, call.caller_lines, repo_root)
        if caller is None:
            record.unresolvable(NULLSAFE_REASON_PREFIX + REASON_NULLSAFE_CALLER_NOT_IN_GRAPH)
            continue
        sites.append(
            CallSite(
                caller_nid=caller,
                receiver="",
                method=call.method,
                source_file=call.source_file,
                source_location=f"L{call.line}",
            )
        )
    sites, _ = _with_caller_scope(index, sites, [])
    if not sites:
        return
    targets: list[CallTarget] = []
    for source in sources:
        ask = getattr(source, "unsent_calls", None)
        if callable(ask) and _takes_nullsafe(ask):
            targets.extend(ask(repo_root, (), (), nullsafe=sites))
    proved = {target.site for target in targets}
    unproved = sum(1 for site in sites if site not in proved)
    if unproved:
        record.unresolvable(NULLSAFE_REASON_PREFIX + REASON_NULLSAFE_NO_RECORD, unproved)
    bound = _write_edges(
        targets,
        index,
        sink,
        record,
        stubs=stubs,
        repo_root=repo_root,
        reason_prefix=NULLSAFE_REASON_PREFIX,
    )
    record.nullsafe_calls_added = sum(1 for site in sites if site in bound)


_LOCATION_LINE = re.compile(r"\d+")


def _first_offered(sources: Sequence[CallTargetSource], attribute: str) -> Callable | None:
    """The first source's optional method of that name, as `unsent_calls` is optional."""
    for source in sources:
        found = getattr(source, attribute, None)
        if callable(found):
            return found
    return None


def _engine_verdict(call: EngineCall, proof: LineProof | None, names, repo_root: str) -> str:
    """`""` when every record on the line names a body other than the engine's target."""
    if proof is None or not proof.targets:
        return REASON_ENGINE_NO_RECORD
    marks = [names(call.target_nid, target, repo_root) for target in proof.targets]
    if all(mark is True for mark in marks):
        return REASON_ENGINE_AGREES
    if any(mark is True for mark in marks):
        return REASON_ENGINE_RECORDS_DISAGREE
    if any(mark is None for mark in marks):
        return REASON_ENGINE_UNPLACED
    return ""


def _check_engine_edges(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    all_edges: list[dict],
    index: NodeIndex,
    sink: EdgeSink,
    record: status.ResolverStatus,
    *,
    owned: bool,
    stubs=None,
) -> None:
    """Drop graphify's bare-name engine edges a type proof contradicts at every site.

    The engine binds `$x->m()` to the last `m` declared in the caller's file, keeps one edge
    per (caller, target) at the first site's line, and sends no raw call for it, so no other
    pass sees it. It is dropped only when records in the caller prove another body at that
    line AND at every other `->m(` in the caller, each line's records covering each call:
    the edge may stand for any of those sites, and one unproved site may be the real one.
    The proved targets are then edged the way `unsent_calls` edges are.

    Removal needs the list graphify keeps. An incremental build hands over a copy, where a
    removal is lost while an added edge is not, so there nothing is dropped or added.
    """
    list_calls = getattr(index, "engine_calls", None)
    names = getattr(index, "names_node", None)
    if not callable(list_calls) or not callable(names):
        return
    listed: list[EngineCall] = list(list_calls(all_edges))
    record.engine_sites_total = len(listed)
    if not listed:
        return

    def keep(reason: str, count: int = 1) -> None:
        record.engine_kept[reason] = record.engine_kept.get(reason, 0) + count

    # A dot-less target is a free function or an anonymous class's method; only a parser that
    # reads a method declared there lets it in.
    declared = _first_offered(sources, "method_declared_at")
    engine: list[EngineCall] = []
    for call in listed:
        kind = (
            True
            if call.target_is_method
            else declared(repo_root, call.source_file, call.target_location, call.method)
            if declared is not None
            else None
        )
        if kind is True:
            engine.append(call)
        else:
            keep(REASON_ENGINE_TARGET_NOT_METHOD if kind is False else REASON_ENGINE_TARGET_UNKNOWN)
    if not engine:
        return

    def site_at(call: EngineCall, line: int) -> CallSite:
        return CallSite(
            caller_nid=call.caller_nid,
            receiver="",
            method=call.method,
            source_file=call.source_file,
            source_location=f"L{line}",
        )

    def line_of(item: EngineCall | CallSite) -> int:
        found = _LOCATION_LINE.search(item.source_location)
        return int(found.group()) if found else 0

    prove = _first_offered(sources, "line_proofs")
    anchors, _ = _with_caller_scope(index, [site_at(c, line_of(c)) for c in engine], [])
    proofs = prove(repo_root, anchors) if prove is not None else None
    if proofs is None:
        keep(REASON_ENGINE_NO_PROOFS, len(engine))
        return

    lines_of = _first_offered(sources, "member_call_lines")
    pending: list[tuple[EngineCall, CallSite, CallerSites, list[CallSite]]] = []
    for call, anchor in zip(engine, anchors, strict=True):
        reason = _engine_verdict(call, proofs.get(anchor), names, repo_root)
        if reason:
            keep(reason)
            continue
        record.engine_contradicted += 1
        sites: CallerSites | None = (
            lines_of(repo_root, call.source_file, call.caller_location, call.method)
            if lines_of is not None
            else None
        )
        if sites is None:
            keep(REASON_ENGINE_SITES_UNKNOWN)
            continue
        if sites.other_kinds:
            keep(REASON_ENGINE_OTHER_CALL_KIND)
            continue
        if line_of(call) not in sites.lines:
            keep(REASON_ENGINE_SITE_NOT_FOUND)
            continue
        others, _ = _with_caller_scope(
            index, [site_at(call, n) for n in sorted(sites.lines) if n != line_of(call)], []
        )
        pending.append((call, anchor, sites, others))

    asked = [site for *_, others in pending for site in others]
    proofs = {**proofs, **(prove(repo_root, asked) or {})} if asked else proofs
    remove = getattr(sink, "remove_edges", None)
    drops: list[tuple[EngineCall, list[CallTarget], list[CallTarget], frozenset[int]]] = []
    for call, anchor, sites, others in pending:
        at = {line_of(call): anchor} | {line_of(s): s for s in others}
        held = all(
            not _engine_verdict(call, proofs.get(at[n]), names, repo_root)
            and proofs[at[n]].calls >= count
            for n, count in sites.lines.items()
        )
        if not held:
            keep(REASON_ENGINE_OTHER_SITE)
            continue
        if not owned or not callable(remove):
            keep(REASON_ENGINE_NOT_OWNED)
            continue
        here = list(proofs[anchor].targets)
        proved = here + [t for s in others for t in proofs[s].targets]
        drops.append((call, here, proved, sites.foreign))
    if not drops:
        return

    proved_node = getattr(index, "proved_node_for", None)
    origin_aware = getattr(index, "method_node_for", None)
    closure_start = getattr(index, "closure_starts_on", None)
    no_method = getattr(index, "method_missing_in_declaring_file", None)

    def node_of(target: CallTarget) -> str | None:
        # The binding `_write_edges` uses: the proof's declaring file first, visibility only
        # when the proof names no file. Only a node holding the proved body will do.
        if callable(proved_node):
            nid = proved_node(target.site.caller_nid, target, repo_root)
        elif callable(origin_aware):
            nid = origin_aware(target.site.caller_nid, target.class_fqn, target.method)
        else:
            nid = index.method_node(target.class_fqn, target.method)
        return nid if nid is not None and names(nid, target, repo_root) is True else None

    def refusal(target: CallTarget, foreign: frozenset[int]) -> str:
        """Why a proof cannot be edged from the caller, or `""`. The drop stands either way."""
        site = target.site
        if not site.owns_line:
            return REASON_ENGINE_PROOF_NESTED
        shared = callable(closure_start) and closure_start(site.caller_nid, site.source_location)
        if shared or line_of(site) in foreign:
            return REASON_ENGINE_PROOF_SHARED_LINE
        return ""

    before = getattr(sink, "added", 0)
    stubbed = stubs.edges_added if stubs is not None else 0
    # The node each proved target landed on, stub included: the shrink guard credits a dropped
    # edge only when the row names a proof edge that is in the new graph.
    landed: dict[int, str] = {}
    for call, _here, proved, foreign in drops:
        for target in proved:
            refused = refusal(target, foreign)
            if refused:
                record.unresolvable(ENGINE_REASON_PREFIX + refused)
                continue
            nid = node_of(target)
            if nid is None:
                if stubs is not None and is_vendor_file(target.declaring_file, repo_root):
                    stub_nid = stubs.node_for(target.class_fqn)
                    stubs.add_call(call.caller_nid, stub_nid, target)
                    landed.setdefault(id(target), stub_nid)
                elif callable(no_method) and no_method(target, repo_root):
                    record.unresolvable(ENGINE_REASON_PREFIX + REASON_ENGINE_METHOD_NO_NODE)
                else:
                    record.unresolvable(ENGINE_REASON_PREFIX + REASON_NOT_IN_GRAPH)
            elif nid == call.caller_nid:
                record.unresolvable(ENGINE_REASON_PREFIX + REASON_SELF_CALL)
            else:
                sink.add_call(call.caller_nid, nid, target)
                landed.setdefault(id(target), nid)
    record.engine_proved_added = getattr(sink, "added", 0) - before
    if stubs is not None:
        record.engine_proved_added += stubs.edges_added - stubbed

    record.engine_dropped = remove([call.edge for call, *_ in drops])
    for call, here, proved, _foreign in drops:
        if any(target.interface_target for target in proved):
            record.engine_dropped_interface += 1
        # Only an edge this pass placed (or the graph already held) can stand as the proof.
        proof_nid = next((landed[id(t)] for t in here if id(t) in landed), None)
        record.engine_dropped_edges.append(
            {
                "source": call.caller_nid,
                "target": call.target_nid,
                "relation": "calls",
                "reason": REASON_ENGINE_DROPPED,
                "proof": {"source": call.caller_nid, "target": proof_nid},
            }
        )


def _drop_new_site_edges(
    sources: Sequence[CallTargetSource],
    repo_root: str,
    all_edges: list[dict],
    index: NodeIndex,
    sink: EdgeSink,
    record: status.MemberCallStatus,
    *,
    owned: bool,
) -> None:
    """Drop graphify's edges that bound `new Type(...)` to a method named `type()`.

    graphify sends `new Foo(...)` as a call to `Foo`; when its exact lookup misses (a vendor
    class), the PHP fallback matches labels case-insensitively and can land on a method node.
    An edge goes only when a parser reads a `new` of that name on the edge's line and no call
    of that name there: such a call could be what the edge stands for. Runs before our edges
    are written, because the sink skips a pair the graph holds and this edge would hide ours.
    """
    listed = getattr(index, "method_bound_calls", None)
    read = _first_offered(sources, "new_site_line")
    if not callable(listed) or read is None:
        return

    def keep(reason: str) -> None:
        record.new_site_edges_total += 1
        record.new_site_kept[reason] = record.new_site_kept.get(reason, 0) + 1

    remove = getattr(sink, "remove_edges", None)
    drops: list[MethodBoundCall] = []
    for call in listed(all_edges):
        found = _LOCATION_LINE.search(call.source_location)
        line = read(repo_root, call.source_file, int(found.group()), call.method) if found else None
        if line is None:
            keep(REASON_NEW_SITE_NOT_LOCATED)
            continue
        if not line.creates:
            continue
        if line.calls_name:
            keep(REASON_NEW_SITE_SHARED_LINE)
            continue
        if not owned or not callable(remove):
            keep(REASON_NEW_SITE_NOT_OWNED)
            continue
        record.new_site_edges_total += 1
        drops.append(call)
    if not drops:
        return
    record.new_site_edges_dropped = remove([call.edge for call in drops])
    record.new_site_dropped_edges = [
        {"source": c.caller_nid, "target": c.target_nid, "source_location": c.source_location}
        for c in drops
    ]


def run(
    per_file: Sequence[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    *,
    sources: Sequence[CallTargetSource] | Callable[[str], Sequence[CallTargetSource]],
    repo_root: str = ".",
    index: NodeIndex | None = None,
    sink: EdgeSink | None = None,
    scopes: ScopeIndex | None = None,
    stubs=None,
    edges_owned: bool | None = None,
) -> status.ResolverStatus:
    """Resolve PHP member calls into `calls` edges, and record what happened either way.

    `sources` may be a factory taking the repository root. It is called in here, inside the
    record's own try, so a factory that raises leaves a `failed` record and not silence.
    `edges_owned` says whether `all_edges` is graphify's own list; None asks the graph layer.
    """
    record = MemberCallStatus(name=RESOLVER_NAME)
    try:
        if callable(sources):
            sources = sources(repo_root)
        if edges_owned is None:
            from .graph_adapter import holds_build_edges

            edges_owned = holds_build_edges(all_edges)
        if index is None or sink is None:
            from .graph_adapter import GraphEdgeSink, GraphNodeIndex

            built = GraphNodeIndex(all_nodes, all_edges)
            index = index or built
            sink = sink or GraphEdgeSink(all_edges, built)
        if stubs is None and os.environ.get(EXTERNAL_STUBS_ENV) == "1":
            from .graph_adapter import GraphStubSink

            stubs = GraphStubSink(all_nodes, all_edges)
        record.external_stubs = status.STUBS_ON if stubs is not None else status.STUBS_OFF
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

        corpus = collect_corpus(per_file)
        record.files_seen = len(corpus)
        if not usable:
            # A repository with no PHPStan in `vendor/` is the normal case, not a fault: the
            # build publishes, and the record says which layer was missing.
            record.state = "skipped"
            record.reason = "; ".join(unavailable) or "no call target sources configured"
            # Nothing was proved here, so core's PHP pass keeps every site.
            core_guard.prove(per_file, ())
            return record

        _drop_new_site_edges(usable, repo_root, all_edges, index, sink, record, owned=edges_owned)
        site_raws: list[dict] = []
        group_raws: list[dict] = []
        sites = collect_sites(per_file, site_raws)
        groups = collect_scoped_groups(per_file, scopes, group_raws) if scopes is not None else []
        sites, groups = _with_caller_scope(index, sites, groups)
        record.eligible_sites = len(sites) + len(groups)
        targets = _ask_sources(usable, repo_root, sites, record, corpus=corpus)
        targets += _resolve_groups(groups, usable, repo_root, record, corpus=corpus)
        claimed = {t.site for t in targets}
        record.resolved_sites = len(claimed)
        _write_edges(targets, index, sink, record, stubs=stubs, repo_root=repo_root)
        # A claim proves the site even when its target is missing from the graph (a vendor class
        # with stubs off): core binding another target there would contradict the source.
        proved = _proved_raws(sites, site_raws, groups, group_raws, claimed)
        # The unsent edges below prove nothing: no raw call stands behind them to withhold.
        unsent = _unsent_calls(usable, repo_root, groups, sites)
        before = getattr(sink, "added", 0)
        _write_edges(unsent, index, sink, record, stubs=stubs, repo_root=repo_root)
        record.unsent_calls_added = getattr(sink, "added", 0) - before
        _nullsafe_calls(usable, repo_root, corpus, index, sink, record, stubs=stubs)
        _check_engine_edges(
            usable, repo_root, all_edges, index, sink, record, owned=edges_owned, stubs=stubs
        )
        record.edges_added = getattr(sink, "added", record.resolved_sites)
        if stubs is not None:
            record.stub_nodes_added = stubs.nodes_added
            record.stub_edges_added = stubs.edges_added
        # Published only on success: core_guard skips core when this pass failed. Refused sites
        # stay with core, whose edge there may be right.
        core_guard.prove(per_file, proved)
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


def make_resolver(
    sources_factory: Callable[[str], Sequence[CallTargetSource]], repo_root: str = "."
):
    """Wrap `run` in the `(per_file, all_nodes, all_edges) -> None` shape graphify calls.

    The factory is called at resolution time, not at registration time: registering happens at
    import, when the working directory and the repository under build are not yet settled.
    """

    def resolve(per_file, all_nodes, all_edges) -> None:
        run(per_file, all_nodes, all_edges, sources=sources_factory, repo_root=repo_root)

    resolve.__name__ = RESOLVER_NAME
    return resolve


RESOLVER_NAME_ROUTES = "php_routes"

# Reasons the route pass owns. A source's own reasons arrive prefixed with its name.
REASON_ROUTE_NO_CONTROLLER = "controller_not_in_graph"
REASON_ROUTE_NO_PATH = "route_had_no_path"


def run_routes(
    per_file: Sequence[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
    *,
    sources: Sequence[object] | Callable[[str], Sequence[object]],
    repo_root: str = ".",
    index: NodeIndex | None = None,
    sink=None,
) -> status.ResolverStatus:
    """Publish route declarations as nodes, each edged to the controller method that serves it.

    A pass of its own rather than a stage of the member-call one, and with its own record, for
    the reason this package exists: a layer that silently adds nothing is indistinguishable from
    a codebase that has none of what it looks for. Routes were extracted and measured for a
    while before anything connected them to the graph, and every test stayed green throughout,
    because the tests checked extraction and nothing checked publication.
    """
    record = status.ResolverStatus(name=RESOLVER_NAME_ROUTES)
    try:
        if callable(sources):
            # Same contract as `run`: a factory, called inside the try so that it reports.
            sources = sources(repo_root)
        if index is None or sink is None:
            from .graph_adapter import GraphNodeIndex, GraphRouteSink

            index = index or GraphNodeIndex(all_nodes, all_edges)
            sink = sink or GraphRouteSink(all_nodes, all_edges)

        unavailable: list[str] = []
        usable: list[object] = []
        for source in sources:
            ok, reason = source.available(repo_root)
            if ok:
                usable.append(source)
            else:
                unavailable.append(f"{source.name}: {reason}")

        # The same corpus the call sources get. A route lives in a controller file, which in
        # practice always carries call sites too — but "in practice" is not a reason to hand a
        # source a narrower set than the build parsed.
        corpus = collect_corpus(per_file)
        record.files_seen = len(corpus)
        if not usable:
            record.state = "skipped"
            record.reason = "; ".join(unavailable) or "no route sources configured"
            return record

        declarations = []
        for source in usable:
            resolution = source.declarations(repo_root, corpus)
            record.files_seen = max(record.files_seen, resolution.files_seen)
            for reason, count in (resolution.unresolved or {}).items():
                record.unresolvable(f"{source.name}:{reason}", count)
            declarations.extend(resolution.declarations)

        record.eligible_sites = len(declarations)
        # The declaration names the file it was read from, and the controller class is declared
        # in that same file. An index that can use it does; `ports.NodeIndex` cannot express it
        # yet, so the narrow call stays the fallback.
        in_file = getattr(index, "method_node_in_file", None)
        for declaration in declarations:
            controller_nid = (
                in_file(
                    declaration.source_file,
                    declaration.controller_class,
                    declaration.controller_method,
                )
                if callable(in_file)
                else index.method_node(declaration.controller_class, declaration.controller_method)
            )
            if controller_nid is None:
                # The node still goes in: the declaration is a fact about this codebase whether
                # or not the controller method survived extraction, and dropping it would hide
                # the path. Only the edge is withheld.
                record.unresolvable(REASON_ROUTE_NO_CONTROLLER)
            if sink.add_route(declaration, controller_nid) is None:
                record.unresolvable(REASON_ROUTE_NO_PATH)

        record.resolved_sites = sink.nodes_added
        record.edges_added = sink.edges_added
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


def make_route_resolver(sources_factory: Callable[[str], Sequence[object]], repo_root: str = "."):
    """Wrap `run_routes` in the shape graphify's registry calls."""

    def resolve(per_file, all_nodes, all_edges) -> None:
        run_routes(per_file, all_nodes, all_edges, sources=sources_factory, repo_root=repo_root)

    resolve.__name__ = RESOLVER_NAME_ROUTES
    return resolve
