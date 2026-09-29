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
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from ..ports import RouteDeclarationSource  # noqa: F401  the port this module implements
from ..syntax import nodes
from ..syntax.facts import FileFacts
from ..syntax.file_scan import scan_source
from ..syntax.routes import RouteDeclaration

UNREADABLE_FILE = "unreadable-file"


@dataclass
class RouteResolution:
    """What a source made of the files it was given."""

    declarations: list[RouteDeclaration] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)
    files_seen: int = 0

    @property
    def resolved(self) -> list[RouteDeclaration]:
        return [d for d in self.declarations if d.resolved]




class TreeSitterRouteSource:
    """Route declarations read from PHP attributes.

    A declaration proves that this codebase declares the path and names this controller method
    to serve it. It does not prove that any deployed ingress sends that path here, nor that a
    given environment loads the route — Symfony filters on `env`, and a reverse proxy or a
    different service may answer first. The graph gains "who in this code claims this path",
    which is the question that has no symbol in it and is therefore unanswerable today.
    """

    name = "tree_sitter_routes"

    def available(self, repo_root: str) -> tuple[bool, str]:
        return nodes.tree_sitter_available()

    def declarations(self, repo_root: str, files: Iterable[str]) -> RouteResolution:
        resolution = RouteResolution()
        seen: set[str] = set()
        for path in files:
            full = path if os.path.isabs(path) else os.path.join(repo_root, path)
            full = os.path.realpath(full)
            if full in seen:
                continue
            seen.add(full)
            facts = self._facts(path, full)
            if facts is None:
                _count(resolution, UNREADABLE_FILE)
                continue
            resolution.files_seen += 1
            for declaration in facts.routes:
                resolution.declarations.append(declaration)
                if declaration.reason:
                    _count(resolution, declaration.reason)
        return resolution

    def _facts(self, path: str, full: str) -> FileFacts | None:
        try:
            with open(full, "rb") as handle:
                source = handle.read()
        except OSError:
            return None
        return scan_source(path, source)


def _count(resolution: RouteResolution, reason: str) -> None:
    resolution.unresolved[reason] = resolution.unresolved.get(reason, 0) + 1
