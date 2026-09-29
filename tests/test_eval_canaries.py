"""The leak check has to fail on a leak, and pass on a graph that indexes identifiers.

Both halves matter. A check that never fails proves nothing, and a check that fires on the
constant name `API_TOKEN` would force the package to stop indexing identifiers, which is the
behaviour it is supposed to keep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import canaries as cn  # noqa: E402


def test_canaries_are_actually_planted():
    assert cn.verify_planted() == []


def test_planted_values_are_obvious_fakes():
    for canary in cn.CANARIES:
        assert cn.MARKER in canary.value, canary.name


def test_clean_artifacts_pass():
    artifacts = {
        "graph.json": json.dumps(
            {
                "nodes": [
                    {"id": "n1", "name": "Eval\\Secrets\\Mailer", "kind": "class"},
                    {"id": "n2", "name": "apiKey", "kind": "method"},
                    {"id": "n3", "name": "API_TOKEN", "kind": "constant"},
                    {"id": "n4", "name": "mailer_password", "kind": "parameter"},
                ]
            }
        ),
        "status.json": json.dumps({"resolvers": [{"name": "php_types", "state": "completed"}]}),
        "build.log": "php_types: 582 files seen, 1 skipped",
    }
    cn.assert_no_leaks(artifacts)


def test_identifiers_beside_a_canary_are_allowed():
    """The rule is values never, identifiers freely; this is the second half of it."""
    indexed = {canary.identifier for canary in cn.CANARIES}
    assert cn.scan_artifacts({"graph.json": " ".join(sorted(indexed))}) == []


@pytest.mark.parametrize("canary", cn.CANARIES, ids=lambda c: c.name)
def test_each_canary_is_detected_when_it_leaks(canary):
    leaks = cn.scan_artifacts({"graph.json": f'{{"default": "{canary.value}"}}'})
    assert [leak.canary for leak in leaks] == [canary.name]


def test_assert_no_leaks_raises_and_redacts():
    canary = cn.CANARIES[2]
    with pytest.raises(cn.CanaryLeak) as raised:
        cn.assert_no_leaks({"diagnostics.txt": f"failed to parse token {canary.value}"})
    message = str(raised.value)
    assert canary.value not in message      # the failure report must not leak it again
    assert "redacted" in message
    assert "diagnostics.txt" in message


def test_leak_in_any_artifact_kind_is_caught():
    value = cn.CANARIES[1].value
    artifacts = {
        "graph.json": "{}",
        "status.json": json.dumps({"resolvers": [{"name": "php_types", "reason": f"bad dsn {value}"}]}),
    }
    leaks = cn.scan_artifacts(artifacts)
    assert [leak.artifact for leak in leaks] == ["status.json"]


def test_collect_artifacts_walks_a_directory(tmp_path):
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
    (tmp_path / "graphify-out" / "leak.log").write_text(cn.CANARIES[0].value, encoding="utf-8")
    artifacts = cn.collect_artifacts([tmp_path / "graphify-out"])
    assert len(artifacts) == 2
    with pytest.raises(cn.CanaryLeak):
        cn.assert_no_leaks(artifacts)


def test_verify_planted_reports_a_disarmed_fixture(tmp_path):
    (tmp_path / "secrets.php").write_text("<?php // values stripped", encoding="utf-8")
    problems = cn.verify_planted(tmp_path)
    assert problems
    assert any("not present" in problem for problem in problems)
