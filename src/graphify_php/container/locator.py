"""Finds the compiled container, and distinguishes the several ways there can be none.

The distinction is the whole job. `var/cache/prod/` on a machine that never ran with
`kernel.debug` on has no XML dump at all — Symfony writes the human-readable XML only for the
debug container — so "no file" is the ordinary state of a production checkout and must not be
reported the same way as "this is not a Symfony project" or "the file is there and broken".
"""

from __future__ import annotations

import json
from pathlib import Path

from . import reasons

# Both halves of a Symfony install: the framework itself, and the bundle that compiles the
# container the dump comes from. Either one present is enough to call the project Symfony.
_SYMFONY_MARKERS = ("symfony/framework-bundle", "symfony/http-kernel", "symfony/dependency-injection")


class SymfonyCacheLocator:
    """Looks in `var/cache/*/` for `*Container.xml`."""

    name = "symfony-var-cache"

    def __init__(self, preferred_environments: tuple[str, ...] = ("dev", "test")) -> None:
        # Preference, not a filter: a project with a custom environment name still gets found,
        # it just gets picked after the conventional ones.
        self._preferred = preferred_environments

    def locate(self, repo_root: Path) -> tuple[Path | None, str]:
        composer = repo_root / "composer.json"
        if not composer.exists():
            return None, reasons.SKIP_NOT_PHP_PROJECT
        if not self._is_symfony(composer):
            return None, reasons.SKIP_NOT_SYMFONY

        cache_root = repo_root / "var" / "cache"
        if not cache_root.is_dir():
            return None, reasons.SKIP_NO_CACHE_DIR

        candidates: list[tuple[int, Path]] = []
        for environment_dir in sorted(p for p in cache_root.iterdir() if p.is_dir()):
            rank = (
                self._preferred.index(environment_dir.name)
                if environment_dir.name in self._preferred
                else len(self._preferred)
            )
            for candidate in sorted(environment_dir.glob("*Container.xml")):
                candidates.append((rank, candidate))
        if not candidates:
            return None, reasons.SKIP_XML_ABSENT
        candidates.sort(key=lambda item: (item[0], str(item[1])))
        return candidates[0][1], ""

    def _is_symfony(self, composer: Path) -> bool:
        try:
            manifest = json.loads(composer.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError):
            return False
        required = {}
        for section in ("require", "require-dev"):
            value = manifest.get(section)
            if isinstance(value, dict):
                required.update(value)
        return any(marker in required for marker in _SYMFONY_MARKERS)
