"""Reads the reference application's real compiled container, and skips when it is absent.

Read-only, and the artifact goes to a pytest temporary directory: nothing under the reference
application is written, no PHP runs, no kernel boots. The point of the test is that the shapes
the unit fixtures assert — an alias, a tagged iterator, a decorator, an env argument — are the
shapes a 1.36 MB production-grade dump actually has, at a scale a fixture cannot reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graphify_php.container import ArgumentKind, OverlayWriter, SymfonyXmlContainerReader

REFERENCE = Path("/var/www/www.cem.dev.lo/var/cache/dev/App_KernelDevDebugContainer.xml")

pytestmark = pytest.mark.skipif(
    not REFERENCE.exists(), reason=f"reference container not present at {REFERENCE}"
)


def _interface_aliases_in(path: Path) -> int:
    """Count, straight from the XML, the aliases the reader is expected to bind.

    Deliberately a second implementation rather than a call into the reader: a test that asks
    the code under test for its own expected answer proves only that it is self-consistent.
    The condition mirrors `OverlayWriter.counts` — an alias whose id names an interface and
    whose target resolves to a known class.
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    namespace = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    tag = (lambda name: f"{{{namespace}}}{name}") if namespace else (lambda name: name)

    classes = {
        service.get("id"): service.get("class")
        for service in root.iter(tag("service"))
        if service.get("id") and not service.get("alias")
    }
    aliases = [
        service for service in root.iter(tag("service"))
        if service.get("alias") and str(service.get("id", "")).endswith("Interface")
    ]
    return sum(1 for a in aliases if classes.get(a.get("alias")))


@pytest.fixture(scope="module")
def facts():
    parsed = SymfonyXmlContainerReader().read(REFERENCE)
    assert parsed is not None
    return parsed


def test_real_container_yields_facts_of_every_kind(facts) -> None:
    counts = OverlayWriter().counts(facts)
    # Floors, not equalities: the reference application keeps being developed, and a test that
    # pins exact totals fails on an unrelated commit while catching nothing.
    assert counts["services"] > 2500
    assert counts["aliases"] > 300
    # Completeness, not a floor: this one is counted independently from the same file, so the
    # test fails when the reader starts missing aliases rather than when the application
    # shrinks. A floor here passed at 50 while the reader recovered 82 of 82 and would have
    # gone on passing if it recovered 51.
    assert counts["interface_bindings"] == _interface_aliases_in(REFERENCE)
    assert counts["injection_sites"] > 4000
    assert counts["tag_memberships"] > 1500
    assert counts["decorations"] > 5
    assert any(i.kind is ArgumentKind.TAGGED_ITERATOR for i in facts.injections)
    assert any(i.kind is ArgumentKind.ENV and i.env_name for i in facts.injections)


def test_real_overlay_holds_no_env_values(facts, tmp_path: Path) -> None:
    text = (OverlayWriter().write(facts, tmp_path)).read_text()
    # An unexpanded `%env(...)%` in the output would mean a raw expression was copied through,
    # which is the failure mode that would carry a default value with it.
    assert "%env(" not in text
