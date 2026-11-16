"""The framework provider seam: what one PHP framework contributes to the route pass.

The pass itself is framework-free. It hands every selected provider a `ProviderContext`, takes
back one `ProviderResult` per provider, and publishes the records in it: route nodes and
`handled_by` edges, `guarded_by` edges from holders and from routes, `checks_access` edges. All
providers emit the same three records from `syntax.routes` (`RouteDeclaration`, `AccessGuard`,
`AccessCheck`) plus `RouteGuard`, each carrying `provider`, `via` and `guard_kind`, so the
publishing side has one shape.

## How to add a provider

1. Create `frameworks/<name>.py` exporting `PROVIDER`, any object with `name`, `packages` and
   `collect(ctx) -> ProviderResult`. `packages` lists the Composer packages that select it;
   `detect.py` reads only the DIRECT `require` and `require-dev` of the root `composer.json`.
2. In `collect`, read routes however the framework declares them:
   - attributes: `ctx.facts(path).attributes` lists every attribute with its FQCN resolved;
     `matching.AttributeMatcher` matches an FQCN, or a repo subclass of one, against a list;
   - a routes file or a config array: `ctx.parse(path)` gives the tree and the file's names;
     `syntax.routes` has the value readers (`arguments`, `pick`, `string_value`,
     `string_list`, `path_value`, `class_constant_name` for `Foo::class`).
   Set `provider`, `via` ("attribute", "route_file") and, when the handler class is declared
   in another file than the route, `handler_file` (from `ctx.class_files`, only when unique).
3. Guards: a holder guard (class or method) is an `AccessGuard`; a guard that applies to a
   route is a `RouteGuard` keyed by the route's `node_key`. Set `via`, `guard_attribute`
   (what is written on the edge: "middleware", "filter") and `guard_kind`
   (`syntax.routes.GUARD_KIND_*`: authorization, authentication, public, middleware,
   unknown). Only `authorization` makes a handler guarded in the coverage counters and a route
   `covered`; an `unknown` guard makes it `undecided`. Outside
   `syntax.routes.FROZEN_KEY_PROVIDERS` the guard node key is
   `guard#<provider>:<guard_kind>:<label>` (`<Holder>::guard#...` for source text), so a role
   and a middleware alias with one text get two nodes. A route-scoped guard read from a config
   rule is keyed `config_rule_key(provider, file, index)`. Configuration matched against route
   paths (Symfony's `access_control`) is computed inside `collect` too; a route it cannot
   decide goes into `route_undecided` with an `AUTHORIZATION_*` reason.
   Route templates: set `RouteDeclaration.template` to the path with `{name}` / `{name?}`
   placeholders (`syntax.routes.brace_template` builds it), or "" with a `TEMPLATE_*` reason in
   `template_reason`, and count that reason. None publishes "".
4. Handler-body grant calls: `syntax.routes.collect_access_checks(root, names, path, handlers,
   grants, provider)`, with `handlers` the `(class, method)` pairs THIS provider declared in
   that file. `grants` is keyed by call spec `(receiver kind, method)`: `("this", "authorize")`
   for `$this->authorize(...)` and `$this?->authorize(...)`,
   `("static:Illuminate\\Support\\Facades\\Gate", "authorize")` for a static call whose class
   resolves to that FQCN through the file's imports. Values are `GrantCall`s. A typed property
   receiver (`$this->auth->check()`) is not supported yet.
5. A provider's guards and grant calls apply only to routes and handlers it produced itself.
6. Register it with one line in `frameworks/__init__.py` `REGISTRY`.

Every refusal is a fixed reason string with no variable in it, counted in `reasons`; a new
provider prefixes its own (`laravel-closure-action`). A wrong edge is worse than a missing one:
emit a route or a guard only when the source proves it. New providers may use their own node-key
schemes; Symfony's keys are frozen.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..syntax.facts import FileFacts
from ..syntax.file_scan import parse_source
from ..syntax.names import NameResolver
from ..syntax.routes import GUARD_KIND_AUTHORIZATION, GrantCall

__all__ = [
    "AUTHORIZATION_ACCESS_CONTROL_BLOCKED",
    "AUTHORIZATION_ACCESS_CONTROL_UNDECIDED",
    "FrameworkProvider",
    "GrantCall",
    "ParsedFile",
    "ProviderContext",
    "ProviderResult",
    "RouteGuard",
    "config_rule_key",
]

# Why a route's `authorization_state` is undecided, set per route in `route_undecided`: the
# config step could not decide which rule applies, or could not run (unread config, a route
# prefix or path it cannot read).
AUTHORIZATION_ACCESS_CONTROL_UNDECIDED = "access-control-undecided"
AUTHORIZATION_ACCESS_CONTROL_BLOCKED = "access-control-blocked"


def config_rule_key(provider: str, file: str, index: int) -> str:
    """The guard node key of rule `index` in a provider's config `file` (not Symfony's)."""
    return f"{provider}:config#{file}:{index}"


@dataclass(frozen=True)
class RouteGuard:
    """A guard proved to apply to one route, published as `route -guarded_by-> guard`.

    `route_key` is the guarded `RouteDeclaration.node_key`. Equal `node_key`s share one guard
    node; a new provider puts `guard_kind` into its keys. `guard_attribute` is written on the
    node and the edge, `via` as the edge's `_via`. `line` 0 writes an empty `source_location`.
    `extra_node_fields` are appended to the node, in order. Only `authorization` guards make a
    route's handler guarded in the coverage counters.
    """

    route_key: str
    node_key: str
    label: str
    guard_attribute: str
    via: str
    source_file: str
    line: int = 0
    literal: bool = True
    subject: str = ""
    provider: str = ""
    guard_kind: str = GUARD_KIND_AUTHORIZATION
    extra_node_fields: tuple[tuple[str, Any], ...] = ()
    # False: published with its `guard_kind`, but a request may reach the route without it
    # running, so it neither makes the route `covered` nor its handler guarded.
    decides: bool = True


@dataclass
class ProviderResult:
    """What one provider read.

    `reasons` are refusals; the pass records them prefixed with the route source's name.
    `pass_reasons` are recorded unprefixed (Symfony's `access-control-*`, kept as they were).
    `undecided` counts routes a config step could not decide; `route_undecided` names them,
    route `node_key` -> an `AUTHORIZATION_*` reason. The two attribute counters feed
    the status record: attributes this provider mapped, and attributes it did not map whose
    short name equals one of its FQCNs' short names.
    """

    routes: list = field(default_factory=list)
    guards: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    route_guards: list[RouteGuard] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    pass_reasons: dict[str, int] = field(default_factory=dict)
    undecided: int = 0
    route_undecided: dict[str, str] = field(default_factory=dict)
    attributes_mapped: int = 0
    attributes_short_name_only: int = 0

    def count(self, reason: str, n: int = 1) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + n

    def count_pass(self, reason: str, n: int = 1) -> None:
        self.pass_reasons[reason] = self.pass_reasons.get(reason, 0) + n


@dataclass(frozen=True)
class ParsedFile:
    """One file as a reader sees it: the tree's root, its names, and what the scan proved."""

    path: str
    root: Any
    names: NameResolver
    facts: FileFacts


class ProviderContext:
    """What a provider may read. Paths are as the corpus spells them, or repo-relative.

    `files` is the corpus, de-duplicated, readable files only. `facts(path)` is cached and holds
    no tree. `parse(path)` re-parses on every call, inside the corpus or not, so a provider
    parses only the files it needs and keeps no tree alive.
    """

    def __init__(
        self,
        repo_root: str,
        files: tuple[str, ...],
        read: Callable[[str], bytes | None],
        facts: dict[str, FileFacts] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.files = tuple(files)
        self._read = read
        self._facts: dict[str, FileFacts] = dict(facts or {})
        self._parents: dict[str, tuple[str | None, ...]] | None = None
        self._class_files: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def from_sources(cls, sources: dict[str, bytes], repo_root: str = ".") -> ProviderContext:
        """A context over in-memory files, for tests and one-file scans."""
        return cls(repo_root, tuple(sources), sources.get)

    def read(self, path: str) -> bytes | None:
        return self._read(path)

    def facts(self, path: str) -> FileFacts | None:
        cached = self._facts.get(path)
        if cached is None:
            parsed = self.parse(path)
            if parsed is None:
                return None
            cached = self._facts[path] = parsed.facts
        return cached

    def parse(self, path: str) -> ParsedFile | None:
        source = self._read(path)
        if source is None:
            return None
        root, names, facts = parse_source(path, source)
        return ParsedFile(path, root, names, facts)

    def class_parents(self) -> dict[str, tuple[str | None, ...]]:
        """Case-folded FQCN -> the `extends` parent of each corpus declaration of that class."""
        if self._parents is None:
            self._index()
        return self._parents or {}

    def class_files(self, fqcn: str) -> tuple[str, ...]:
        """The corpus files declaring `fqcn`, compared case-insensitively."""
        if self._class_files is None:
            self._index()
        return (self._class_files or {}).get(fqcn.strip().lstrip("\\").casefold(), ())

    def _index(self) -> None:
        parents: dict[str, list] = {}
        files: dict[str, list] = {}
        for path in self.files:
            facts = self.facts(path)
            if facts is None:
                continue
            for scope in facts.classes:
                key = scope.fqn.casefold()
                parents.setdefault(key, []).append(scope.parent)
                files.setdefault(key, []).append(path)
        self._parents = {k: tuple(v) for k, v in parents.items()}
        self._class_files = {k: tuple(dict.fromkeys(v)) for k, v in files.items()}


def file_reader(repo_root: str) -> Callable[[str], bytes | None]:
    """Read a path as given, or relative to `repo_root`; None when it cannot be read."""

    def read(path: str) -> bytes | None:
        full = path if os.path.isabs(path) else os.path.join(repo_root, path)
        try:
            with open(full, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    return read


@runtime_checkable
class FrameworkProvider(Protocol):
    """One framework. See the module docstring for how to write one."""

    name: str
    packages: tuple[str, ...]

    def collect(self, ctx: ProviderContext) -> ProviderResult: ...
