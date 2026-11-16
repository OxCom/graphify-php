"""Which framework providers apply to a repository, read from its root `composer.json`.

Only the DIRECT `require` and `require-dev` of the root manifest count. `composer.lock` is not
read: it adds versions, not intent, and it lists transitive packages, so a Laravel lock names
`symfony/routing` and would select Symfony for an application that is not one. Several
providers may match. None matching is a skip with a fixed reason, never a guess from the code:
a route attribute's short name alone proves no framework.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

REASON_NONE = "framework-profile-none"
# No manifest at the root but one below it: the build root is probably the wrong one.
REASON_NONE_BELOW_ROOT = "framework-profile-none-composer-below-root"

_MAX_DEPTH = 3
_SKIP_DIRS = frozenset({"vendor", "node_modules", ".git", "var"})


def required_packages(repo_root: str) -> set[str] | None:
    """Lower-cased direct requirements of the root manifest, None when it cannot be read."""
    try:
        text = (Path(repo_root) / "composer.json").read_text(encoding="utf-8", errors="replace")
        manifest = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    names: set[str] = set()
    for section in ("require", "require-dev"):
        value = manifest.get(section)
        if isinstance(value, dict):
            names.update(k.lower() for k in value if isinstance(k, str))
    return names


def select_providers(repo_root: str, registry: Sequence) -> tuple[tuple, str]:
    """The providers whose packages are required, in registry order, and a reason when none."""
    packages = required_packages(repo_root)
    chosen = tuple(
        p for p in registry if any(name.lower() in (packages or ()) for name in p.packages)
    )
    if chosen:
        return chosen, ""
    if not (Path(repo_root) / "composer.json").is_file() and _composer_below(repo_root):
        return (), REASON_NONE_BELOW_ROOT
    return (), REASON_NONE


def _composer_below(repo_root: str) -> bool:
    base = os.path.realpath(repo_root)
    for current, dirs, files in os.walk(base):
        depth = 0 if current == base else os.path.relpath(current, base).count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS) if depth < _MAX_DEPTH else []
        if current != base and "composer.json" in files:
            return True
    return False
