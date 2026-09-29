"""Parsing one YAML document, under the assumptions a configuration directory earns.

Symfony configuration is not plain YAML. It carries tags the framework resolves at container
build time — `!php/const App\\Kernel::VERSION`, `!php/enum App\\Status::Draft`, `!tagged_iterator`
— and a parser that resolves tags would need PHP to mean anything by them. They are kept as
opaque strings: the identifier inside is still text this package may index, and nothing here
ever has to decide what it evaluates to.

Three refusals, each because a configuration directory is read from a repository this package
does not control:

- the loader is `SafeLoader`, never `Loader` or `UnsafeLoader`, so no tag can construct a
  Python object;
- alias resolution is capped. PyYAML composes an alias to the node it already built rather
  than to a copy, so the classic billion-laughs file does not multiply memory here — measured,
  a four-level 9-way alias bomb parses fine. The cap bounds compose time against a file with
  far more references than a hand-written configuration has, and it is a stated limit rather
  than a guess about what PyYAML will keep doing;
- a file above the size cap is refused unread rather than parsed, so one pathological file
  cannot decide how long a build takes.

A refusal is counted and named. It never raises out of this module: graphify swallows a
resolver exception into a log line nobody reads, so a malformed file must not be able to take
the whole pass down with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# A Symfony configuration file is a few kilobytes. The cap is generous enough that no real one
# meets it and low enough that a checked-in fixture or generated dump cannot dominate a build.
MAX_BYTES = 2 * 1024 * 1024

# Aliases in real Symfony configuration number in the tens — merge keys in `services.yaml` and
# little else. Anything past this is generated or hostile, and either way is not something this
# package needs to finish parsing.
MAX_ALIASES = 500

REASON_TOO_LARGE = "file_too_large"
REASON_UNREADABLE = "file_unreadable"
REASON_MALFORMED = "yaml_malformed"
REASON_ALIAS_BUDGET = "yaml_alias_budget_exceeded"
REASON_NOT_A_MAPPING = "document_not_a_mapping"


class _AliasBudgetExceeded(Exception):
    pass


class ConfigSafeLoader(yaml.SafeLoader):
    """`SafeLoader` that tolerates Symfony's tags and refuses to expand aliases without bound."""

    def __init__(self, stream) -> None:
        super().__init__(stream)
        self._aliases_seen = 0

    def compose_node(self, parent, index):
        # Every `*ref` passes through here. Counting events rather than measuring the result
        # keeps the bound ahead of the expansion instead of behind it.
        if self.check_event(yaml.events.AliasEvent):
            self._aliases_seen += 1
            if self._aliases_seen > MAX_ALIASES:
                raise _AliasBudgetExceeded(f"more than {MAX_ALIASES} alias expansions")
        return super().compose_node(parent, index)


def _opaque(loader: ConfigSafeLoader, tag_suffix: str, node) -> str:
    """Any tag the framework owns, kept as the text it was written as.

    `!php/const App\\Kernel::VERSION` becomes `!php/const App\\Kernel::VERSION`. A reader that
    wants the class name can read it; nothing here claims to know the value, which is the only
    honest answer without running PHP.
    """
    if isinstance(node, yaml.ScalarNode):
        return f"{node.tag} {loader.construct_scalar(node)}".strip()
    # A sequence or mapping under a custom tag carries no scalar to keep. The tag alone records
    # that something framework-specific was here without inventing a structure for it.
    return str(node.tag)


ConfigSafeLoader.add_multi_constructor("", _opaque)


@dataclass(frozen=True)
class ParseResult:
    """Either a mapping, or the named reason there is none. Never both, never an exception."""

    data: dict | None
    reason: str = ""


class SafeDocumentParser:
    """Reads one file and returns its top-level mapping, or why it did not."""

    def parse(self, path: Path) -> ParseResult:
        try:
            if path.stat().st_size > MAX_BYTES:
                return ParseResult(None, REASON_TOO_LARGE)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ParseResult(None, REASON_UNREADABLE)

        try:
            data = yaml.load(text, Loader=ConfigSafeLoader)
        except _AliasBudgetExceeded:
            return ParseResult(None, REASON_ALIAS_BUDGET)
        except yaml.YAMLError:
            # A hand-edited configuration file with a bad indent is ordinary. It costs this one
            # file, and the count is what makes it visible instead of silent.
            return ParseResult(None, REASON_MALFORMED)

        if not isinstance(data, dict):
            # An empty file parses to `None` and a list-rooted file to a list. Neither is a
            # Symfony configuration document, and both are normal enough to count rather than
            # complain about.
            return ParseResult(None, REASON_NOT_A_MAPPING)
        return ParseResult(data)
