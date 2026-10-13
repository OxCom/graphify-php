"""`graphify-shrink-check`: graphify-mesh reads its stdout JSON and exit code; both are pinned."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graphify_php import shrink_cli


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def builds(tmp_path, monkeypatch):
    """Ten connected nodes in b.py; `new` keeps them, `shrunk` keeps half."""
    monkeypatch.delenv(shrink_cli.POLICY_ENV, raising=False)
    monkeypatch.delenv(shrink_cli.TOLERANCE_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "b.py").write_text("")
    nodes = [{"id": f"b{i}", "source_file": "b.py"} for i in range(10)]
    links = [{"source": f"b{i}", "target": f"b{i + 1}", "relation": "calls"} for i in range(9)]
    m = {"b.py": {"mtime": 0, "ast_hash": "h", "semantic_hash": "h"}}
    return {
        "old": write(tmp_path / "old.json", {"nodes": nodes, "links": links}),
        "new": write(tmp_path / "new.json", {"nodes": nodes, "links": links}),
        "shrunk": write(tmp_path / "shrunk.json", {"nodes": nodes[:5], "links": links[:4]}),
        "manifest": write(tmp_path / "manifest.json", m),
        "root": repo,
        "tmp": tmp_path,
    }


def argv(b, new="new", **overrides):
    args = {
        "--old-graph": b["old"],
        "--new-graph": b[new],
        "--old-manifest": b["manifest"],
        "--new-manifest": b["manifest"],
        "--root": b["root"],
        **overrides,
    }
    return [x for k, v in args.items() for x in (k, str(v))]


def test_accept_exits_0_with_the_report_on_stdout(builds, capsys):
    code = shrink_cli.main(argv(builds))

    out = json.loads(capsys.readouterr().out)
    assert code == shrink_cli.EXIT_ACCEPT == 0
    assert set(out) == {"verdict", "policy", "tolerance", "nodes", "edges", "reasons", "files"}
    assert set(out["nodes"]) == {
        "old",
        "new",
        "lost",
        "orphan",
        "explained",
        "unexplained",
        "base",
        "floor",
    }
    assert set(out["edges"]) == {
        "old",
        "new",
        "lost",
        "explained",
        "unexplained",
        "resolver_old",
        "resolver_lost",
    }
    assert out["verdict"] == "accept" and out["policy"] == "attributed"
    assert out["tolerance"] == 0.10 and out["reasons"] == []


def test_refuse_exits_1_with_reasons_and_files(builds, capsys):
    code = shrink_cli.main(argv(builds, new="shrunk"))

    out = json.loads(capsys.readouterr().out)
    assert code == shrink_cli.EXIT_REFUSE == 1
    assert out["verdict"] == "refuse" and out["reasons"]
    assert set(out["files"][0]) == {"file", "class", "old", "new", "lost"}
    assert out["files"][0] == {"file": "b.py", "class": "unchanged", "old": 10, "new": 5, "lost": 5}


def test_missing_manifests_read_as_empty(builds, capsys):
    gone = builds["tmp"] / "absent.json"
    code = shrink_cli.main(argv(builds, **{"--old-manifest": gone, "--new-manifest": gone}))

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verdict"] == "accept"


@pytest.mark.parametrize("old_manifest", ["absent", "empty"])
def test_missing_old_manifest_falls_back_to_the_count_floor(builds, capsys, old_manifest):
    # Attributed with no old manifest would call b.py changed and explain all five lost nodes.
    path = builds["tmp"] / f"{old_manifest}.json"
    if old_manifest == "empty":
        write(path, {})

    code = shrink_cli.main(argv(builds, new="shrunk", **{"--old-manifest": path}))

    out = json.loads(capsys.readouterr().out)
    assert code == shrink_cli.EXIT_REFUSE == 1
    assert out["verdict"] == "refuse" and out["policy"] == "count"
    assert out["reasons"][-1] == shrink_cli.NO_MANIFEST_REASON
    assert "nodes 10 -> 5, below floor 9" in out["reasons"]


def test_missing_old_manifest_note_rides_on_an_accept(builds, capsys):
    gone = builds["tmp"] / "absent.json"

    code = shrink_cli.main(argv(builds, **{"--old-manifest": gone}))

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verdict"] == "accept" and out["policy"] == "count"
    assert out["reasons"] == [shrink_cli.NO_MANIFEST_REASON]


def test_missing_graph_exits_2_without_a_report(builds, capsys):
    code = shrink_cli.main(argv(builds, **{"--new-graph": builds["tmp"] / "absent.json"}))

    captured = capsys.readouterr()
    assert code == shrink_cli.EXIT_ERROR == 2
    assert captured.out == "" and "cannot read" in captured.err


def test_unparseable_or_non_object_graph_exits_2(builds, capsys):
    (builds["tmp"] / "bad.json").write_text("{not json")
    write(builds["tmp"] / "list.json", [1, 2])

    assert shrink_cli.main(argv(builds, **{"--old-graph": builds["tmp"] / "bad.json"})) == 2
    assert shrink_cli.main(argv(builds, **{"--old-graph": builds["tmp"] / "list.json"})) == 2


def test_unexpected_crash_exits_2_without_a_report(builds, capsys, monkeypatch):
    # Exit 1 means refuse; a crash must not be read as a verdict.
    def crash(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(shrink_cli, "classify_loss", crash)

    code = shrink_cli.main(argv(builds))

    captured = capsys.readouterr()
    assert code == shrink_cli.EXIT_ERROR == 2
    assert captured.out == "" and "KeyError" in captured.err and "boom" in captured.err


def test_usage_errors_exit_2(builds, capsys):
    assert shrink_cli.main(["--old-graph", str(builds["old"])]) == 2
    assert shrink_cli.main(argv(builds, **{"--policy": "nope"})) == 2
    assert shrink_cli.main(argv(builds, **{"--tolerance": "1.5"})) == 2
    assert shrink_cli.main(argv(builds, **{"--tolerance": "lots"})) == 2


def test_policy_and_tolerance_come_from_the_environment(builds, capsys, monkeypatch):
    monkeypatch.setenv(shrink_cli.POLICY_ENV, "count")
    monkeypatch.setenv(shrink_cli.TOLERANCE_ENV, "0.6")

    code = shrink_cli.main(argv(builds, new="shrunk"))

    out = json.loads(capsys.readouterr().out)
    assert out["policy"] == "count" and out["tolerance"] == 0.6
    assert code == 0  # 10 -> 5 nodes is above the 0.6 floor of 4


def test_flags_override_the_environment(builds, capsys, monkeypatch):
    monkeypatch.setenv(shrink_cli.POLICY_ENV, "off")

    code = shrink_cli.main(argv(builds, new="shrunk", **{"--policy": "attributed"}))

    assert code == 1
    assert json.loads(capsys.readouterr().out)["policy"] == "attributed"


def test_bad_tolerance_in_the_environment_exits_2(builds, capsys, monkeypatch):
    monkeypatch.setenv(shrink_cli.TOLERANCE_ENV, "ten percent")

    assert shrink_cli.main(argv(builds)) == 2
    assert "GRAPHIFY_SHRINK_TOLERANCE" in capsys.readouterr().err


def test_producer_changed_flag_reaches_the_classifier(builds, capsys):
    # b.py stays on disk but leaves the corpus: only a producer change explains that.
    write(builds["tmp"] / "empty.json", {})
    args = argv(builds, new="shrunk", **{"--new-manifest": builds["tmp"] / "empty.json"})

    assert shrink_cli.main(args) == 1
    assert shrink_cli.main([*args, "--producer-changed"]) == 0


def test_runs_as_a_module_with_the_same_contract(builds):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    env.pop(shrink_cli.POLICY_ENV, None)
    env.pop(shrink_cli.TOLERANCE_ENV, None)
    done = subprocess.run(
        [sys.executable, "-m", "graphify_php.shrink_cli", *argv(builds, new="shrunk")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 1, done.stderr
    assert json.loads(done.stdout)["verdict"] == "refuse"
