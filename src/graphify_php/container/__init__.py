"""Symfony container overlay: which implementation actually arrives at an injection site.

Measured on the reference application: of 715 injected properties, 652 carry a concrete class
and 63 an interface. The 652 are already answered from source by the package's existing
sources. The remaining 63 cannot be — the type engine can only say `LoggerInterface`, and which
concrete service arrives is decided by the container, not by the code. This package reads that
decision out of the compiled container and writes it as an overlay, never as graph edges.

Four units, one concern each: `locator` finds the file, `reader` parses one format, `facts`
models what was found, `overlay` writes the artifact. `layer` composes them and records a
status. A second container format is a second `ContainerReader`, not a branch in the first.
"""

from __future__ import annotations

from .facts import (
    Alias,
    ArgumentKind,
    ContainerFacts,
    Decoration,
    Injection,
    Member,
    Service,
    TagMembership,
)
from .layer import LAYER_NAME, ContainerOverlayLayer
from .locator import SymfonyCacheLocator
from .overlay import ARTIFACT_KIND, RELATION, OverlayWriter
from .ports import ContainerLocator, ContainerReader
from .reader import SymfonyXmlContainerReader

__all__ = [
    "ARTIFACT_KIND",
    "Alias",
    "ArgumentKind",
    "ContainerFacts",
    "ContainerLocator",
    "ContainerOverlayLayer",
    "ContainerReader",
    "Decoration",
    "Injection",
    "LAYER_NAME",
    "Member",
    "OverlayWriter",
    "RELATION",
    "Service",
    "SymfonyCacheLocator",
    "SymfonyXmlContainerReader",
    "TagMembership",
]
