"""PHP member-call resolution for graphify.

graphify ships cross-file member-call resolvers for Swift, Python, Ruby, TypeScript, C#, Java
and Rust, and none for PHP: its PHP extractor marks `$this->emails->send()` as a member call and
then the shared pass drops it, because a bare `send` collides across the corpus. The measured
result on one Symfony application is 2172 `calls` edges against 3426 callable nodes — 24 % of
callables with an outgoing call edge, 20 % with an incoming one.

This package fills that gap through graphify's own extension seam, without forking it:

    import graphify_php
    graphify_php.register()

`register()` is idempotent, and it imports graphify lazily so that importing this package — in a
test, in a linter, in a tool that only wants the ports — never requires graphify to be installed.
"""

from __future__ import annotations

from importlib import import_module
from typing import Sequence

from .ports import CallTargetSource
from .resolver import (PHP_SUFFIXES, RESOLVER_NAME, RESOLVER_NAME_ROUTES,
                       make_resolver, make_route_resolver)

__all__ = ["RESOLVER_NAME", "RESOLVER_NAME_ROUTES", "default_route_sources",
           "default_sources", "register"]

_registered = False


def default_sources(repo_root: str = ".") -> Sequence[CallTargetSource]:
    """Assemble the shipped `CallTargetSource` list, in precedence order.

    The one place the concrete sources are named. A third source — a language server, an LSIF
    index — becomes a line in `graphify_php.sources`, not an edit to `resolver`, which is why
    `resolver` is handed this list instead of building it.

    Sources are optional at import: this package's only hard requirement is `ports` and
    `status`, and a build with no sources installed records a skip with a reason rather than
    raising inside graphify's exception-swallowing driver.
    """
    try:
        from . import sources as _sources
    except ImportError:
        return ()

    # A factory inside `sources` wins when there is one: it owns its own assembly, including
    # any constructor argument a source grows later.
    for attribute in ("default_sources", "build_sources"):
        factory = getattr(_sources, attribute, None)
        if callable(factory):
            return factory(repo_root)
    listed = getattr(_sources, "SOURCES", None)
    if listed:
        return tuple(listed)

    # Precedence, and the only place it is written down: the type engine knows untyped
    # properties, chains and docblocks that the parser must refuse, so it answers first.
    assembled: list[CallTargetSource] = []
    for module_name, class_name in (("phpstan_source", "PhpStanSource"),
                                    ("tree_sitter_source", "TreeSitterSource")):
        try:
            module = import_module(f"{__name__}.sources.{module_name}")
        except ImportError:
            continue
        source = getattr(module, class_name, None)
        if source is not None:
            assembled.append(source())
    return tuple(assembled)


def default_route_sources(repo_root: str = "."):
    """The shipped `RouteDeclarationSource` list.

    Separate from `default_sources` because the two ports answer different questions and a
    source implements one or the other. Same tolerance: a missing `sources` package means an
    empty list and a recorded skip, not an import error inside graphify's swallowing driver.
    """
    try:
        module = import_module(f"{__name__}.sources.route_source")
    except ImportError:
        return ()
    source = getattr(module, "TreeSitterRouteSource", None)
    return (source(),) if source is not None else ()


def register(sources_factory=default_sources, repo_root: str = ".",
             route_sources_factory=default_route_sources) -> bool:
    """Register the PHP resolver with graphify. Returns whether this call did the registering.

    Idempotent by name rather than by a flag alone: a process that imported this package twice
    under two module paths would otherwise register twice, and graphify runs the registry in
    order without deduplicating, so the pass would run twice and the second run would find
    every edge already present and report zero work.
    """
    global _registered
    from graphify import resolver_registry

    if _registered or any(r.name == RESOLVER_NAME
                          for r in resolver_registry.registered_resolvers()):
        _registered = True
        return False

    resolver_registry.register(resolver_registry.LanguageResolver(
        name=RESOLVER_NAME,
        suffixes=PHP_SUFFIXES,
        resolve=make_resolver(sources_factory, repo_root),
    ))
    # A second pass, not a stage of the first: routes add nodes rather than connect existing
    # ones, and a separate registration gives them a status record the build gate can require.
    resolver_registry.register(resolver_registry.LanguageResolver(
        name=RESOLVER_NAME_ROUTES,
        suffixes=PHP_SUFFIXES,
        resolve=make_route_resolver(route_sources_factory, repo_root),
    ))
    _registered = True
    return True
