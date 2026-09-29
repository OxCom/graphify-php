"""The seam between "read a container format" and "everything else".

Only two protocols, because only two things vary: where the compiled container is, and how to
parse it. The model (`facts`) and the artifact (`overlay`) do not vary with the format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .facts import ContainerFacts


@runtime_checkable
class ContainerLocator(Protocol):
    """Finds a compiled container in a repository, or says why there is none."""

    name: str

    def locate(self, repo_root: Path) -> tuple[Path | None, str]:
        """Return (path, reason). A None path always carries a reason from the fixed
        vocabulary in `reasons.py` — never free text, and never an exception: a repository
        that is not Symfony, and a production cache built with `kernel.debug` off, both
        legitimately have no file here."""


@runtime_checkable
class ContainerReader(Protocol):
    """Turns one container file into facts."""

    name: str
    format: str          # recorded in the artifact, so a consumer knows which reader spoke

    def read(self, path: Path) -> ContainerFacts | None:
        """Facts, or None when the file is unreadable or malformed.

        None rather than an exception: a truncated cache file is a normal condition of a
        cache directory being rewritten under us, and the layer counts it as a skip reason.
        """
