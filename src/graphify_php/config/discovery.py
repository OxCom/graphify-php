"""Finding the files to read, and nothing else.

Scope is `config/**/*.yaml` under the repository root. Not a tree walk: the repositories this
runs against carry `vendor/` and `node_modules/`, where a walk costs seconds and finds YAML that
belongs to somebody else's package. `config/` is where Symfony puts the declarations this
package indexes, and a file outside it is out of scope by construction rather than by a filter.

A repository with no `config/` directory is a plain skip. Most repositories graphify indexes are
not Symfony applications, so this is the common case and must not read as a fault.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_DIRNAME = "config"
SUFFIX = ".yaml"


class ConfigFileFinder:
    """Lists the configuration files in scope, relative paths and absolute paths together."""

    def __init__(self, repo_root: str | Path, dirname: str = CONFIG_DIRNAME) -> None:
        self._root = Path(repo_root)
        self._dirname = dirname

    @property
    def config_dir(self) -> Path:
        return self._root / self._dirname

    def available(self) -> tuple[bool, str]:
        directory = self.config_dir
        if not directory.is_dir():
            return False, f"no {self._dirname}/ directory under {self._root}"
        return True, ""

    def find(self) -> list[tuple[str, Path]]:
        """`(path relative to the repository root, absolute path)`, sorted.

        Sorted because the node and edge lists this feeds are published and diffed between
        builds; directory order is not stable across machines and would make every build look
        changed.

        Symlinks are skipped: a link inside `config/` can point anywhere on the machine, and
        following one would read a file outside the repository under the repository's name.
        """
        directory = self.config_dir
        if not directory.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for path in sorted(directory.rglob(f"*{SUFFIX}")):
            if path.is_symlink() or not path.is_file():
                continue
            found.append((path.relative_to(self._root).as_posix(), path))
        return found
