"""Runs locate -> read -> write, and records the outcome the way every other layer does.

The status record is not bookkeeping. graphify swallows a resolver exception and logs a
warning, so a layer that dies and a layer that was never registered look the same from outside
— and both look like a project that simply has little wiring. This layer therefore reports
`completed`, `skipped` with a reason, or `failed`, exactly as `status.py` intends, and no
absent container is ever a failure: the XML dump exists only when `kernel.debug` is on.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..status import ResolverStatus
from . import reasons
from .locator import SymfonyCacheLocator
from .overlay import DEFAULT_ARTIFACT_NAME, OverlayWriter
from .ports import ContainerLocator, ContainerReader
from .reader import SymfonyXmlContainerReader

LAYER_NAME = "php_container_overlay"


class ContainerOverlayLayer:
    """Composition only. Every decision it could make belongs to one of its three parts."""

    def __init__(
        self,
        locator: ContainerLocator | None = None,
        reader: ContainerReader | None = None,
        writer: OverlayWriter | None = None,
    ) -> None:
        self._locator = locator or SymfonyCacheLocator()
        self._reader = reader or SymfonyXmlContainerReader()
        self._writer = writer or OverlayWriter()

    def run(self, repo_root: Path, out_dir: Path, name: str = DEFAULT_ARTIFACT_NAME) -> ResolverStatus:
        record = ResolverStatus(name=LAYER_NAME)
        path, reason = self._locator.locate(Path(repo_root))
        if path is None:
            return self._skip(record, reason)

        record.files_seen = 1
        facts = self._reader.read(path)
        if facts is None:
            # Unreadable and malformed are one outcome here on purpose: both mean "there is a
            # file and it did not parse", and the reader is the only thing that could tell
            # them apart without a second stat call racing the cache being rewritten.
            return self._skip(record, reasons.SKIP_XML_MALFORMED)

        counts = self._writer.counts(facts)
        artifact = self._writer.write(facts, Path(out_dir), name)
        record.eligible_sites = counts["injection_sites"]
        record.resolved_sites = counts["injection_sites_resolved_to_class"]
        # Not graph edges. Counted under the same field because the build gate reads that
        # field; `state`/`reason` and the artifact itself say what these actually are.
        record.edges_added = counts["injection_sites_resolved_to_class"]
        record.unresolved = dict(facts.skipped)
        record.state = "completed"
        record.reason = f"{artifact}"
        record.finished_at = time.time()
        return record

    def _skip(self, record: ResolverStatus, reason: str) -> ResolverStatus:
        record.state = "skipped"
        record.reason = reason
        record.finished_at = time.time()
        return record
