"""Route declarations as a source, beside the call-target one.

## Why this is a second port rather than a `CallTargetSource`

`CallTargetSource.resolve` returns `CallTarget`s: a call site paired with the class and method
it reaches. A route declaration is not that shape and cannot be forced into it without lying
about one end — there is no `CallSite`, nothing calls the controller from inside this codebase,
and `caller_nid` would have to be invented. `ports.CallSite` says "this package never invents
node identity"; squeezing a route through it would break exactly that promise.

So the protocol below is deliberately separate and deliberately tiny: one method, returning
declarations. It is defined here rather than in `ports.py` because `ports.py` belongs to
another agent. **Proposed change, for whoever owns it:** move `RouteDeclarationSource` and
`RouteResolution` into `ports.py` unchanged and have the resolver discover sources by it, the
same way it discovers `CallTargetSource`. Nothing here needs to change when that happens —
this module would import them instead of defining them.

## What the resolver has to do with a declaration

Each declaration is one node and one edge:

- a node keyed by `RouteDeclaration.node_key`, labelled with the path template, carrying the
  name and the HTTP methods;
- an edge from that node to `NodeIndex.method_node(controller_class, controller_method)` — the
  existing narrow port, which already answers exactly this question and adds no edge when the
  method is not in the graph.

A declaration whose `path` is None is still worth a node when `method_path` survived, and the
resolver should treat `reason` the way it treats `Resolution.unresolved`: a counted gap.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..frameworks import REGISTRY
from ..frameworks.detect import select_providers
from ..frameworks.provider import ProviderContext, RouteGuard, file_reader
from ..ports import RouteDeclarationSource  # noqa: F401  the port this module implements
from ..syntax import nodes
from ..syntax.facts import FileFacts
from ..syntax.file_scan import scan_source
from ..syntax.routes import AccessCheck, AccessGuard, RouteDeclaration, access_check_patterns

UNREADABLE_FILE = "unreadable-file"


@dataclass
class RouteResolution:
    """What a source made of the files it was given.

    `profile_reason` is set when no framework provider was selected; nothing else is then
    read. `pass_reasons` are recorded on the status record unprefixed.
    """

    declarations: list[RouteDeclaration] = field(default_factory=list)
    guards: list[AccessGuard] = field(default_factory=list)
    access_checks: list[AccessCheck] = field(default_factory=list)
    route_guards: list[RouteGuard] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)
    pass_reasons: dict[str, int] = field(default_factory=dict)
    files_seen: int = 0
    providers: tuple[str, ...] = ()
    profile_reason: str = ""
    undecided: int = 0
    # Route `node_key` -> why its authorization state is undecided (`ProviderResult`).
    route_undecided: dict[str, str] = field(default_factory=dict)
    attributes_total: int = 0
    attributes_mapped: int = 0
    attributes_short_name_only: int = 0

    @property
    def resolved(self) -> list[RouteDeclaration]:
        return [d for d in self.declarations if d.resolved]


class TreeSitterRouteSource:
    """Route declarations read by the framework providers a repository selects.

    A declaration proves that this codebase declares the path and names this controller method
    to serve it. It does not prove that any deployed ingress sends that path here, nor that a
    given environment loads the route — Symfony filters on `env`, and a reverse proxy or a
    different service may answer first. The graph gains "who in this code claims this path",
    which is the question that has no symbol in it and is therefore unanswerable today.

    `providers` None selects them from the root `composer.json` (`frameworks.detect`); a
    sequence is used as given.
    """

    name = "tree_sitter_routes"

    def __init__(self, providers: Sequence | None = None) -> None:
        self.providers = tuple(providers) if providers is not None else None

    def available(self, repo_root: str) -> tuple[bool, str]:
        return nodes.tree_sitter_available()

    def declarations(self, repo_root: str, files: Iterable[str]) -> RouteResolution:
        resolution = RouteResolution()
        if self.providers is not None:
            providers = self.providers
        else:
            providers, why = select_providers(repo_root, REGISTRY)
            if not providers:
                resolution.profile_reason = why
                return resolution
        resolution.providers = tuple(p.name for p in providers)
        _, patterns_refused = access_check_patterns()
        if patterns_refused:
            # Once per run, not per file: the override is one setting.
            _count(resolution.unresolved, patterns_refused)
        seen: set[str] = set()
        corpus: list[str] = []
        fulls: dict[str, str] = {}
        cached: dict[str, FileFacts] = {}
        for path in files:
            full = path if os.path.isabs(path) else os.path.join(repo_root, path)
            full = os.path.realpath(full)
            if full in seen:
                continue
            seen.add(full)
            facts = self._facts(path, full)
            if facts is None:
                _count(resolution.unresolved, UNREADABLE_FILE)
                continue
            resolution.files_seen += 1
            resolution.attributes_total += len(facts.attributes)
            corpus.append(path)
            fulls[path] = full
            cached[path] = facts
        outside = file_reader(repo_root)

        def read(path: str) -> bytes | None:
            full = fulls.get(path)
            if full is None:
                return outside(path)
            try:
                with open(full, "rb") as handle:
                    return handle.read()
            except OSError:
                return None

        ctx = ProviderContext(repo_root, tuple(corpus), read, cached)
        for provider in providers:
            result = provider.collect(ctx)
            resolution.declarations.extend(result.routes)
            resolution.guards.extend(result.guards)
            resolution.access_checks.extend(result.checks)
            resolution.route_guards.extend(result.route_guards)
            for reason, count in result.reasons.items():
                _count(resolution.unresolved, reason, count)
            for reason, count in result.pass_reasons.items():
                _count(resolution.pass_reasons, reason, count)
            resolution.undecided += result.undecided
            for key, why in result.route_undecided.items():
                resolution.route_undecided.setdefault(key, why)
            resolution.attributes_mapped += result.attributes_mapped
            resolution.attributes_short_name_only += result.attributes_short_name_only
        return resolution

    def _facts(self, path: str, full: str) -> FileFacts | None:
        try:
            with open(full, "rb") as handle:
                source = handle.read()
        except OSError:
            return None
        return scan_source(path, source)


def _count(table: dict[str, int], reason: str, n: int = 1) -> None:
    table[reason] = table.get(reason, 0) + n
