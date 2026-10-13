"""The shrink guard inside `graphify-php-build`: it runs after the resolver gate passes, and a
refusal puts back the previous graph and manifest together.
"""

from __future__ import annotations

import json

import pytest

from graphify_php import cli, status

MANIFEST = {"b.py": {"mtime": 0, "ast_hash": "h", "semantic_hash": "h"}}


def chain(count):
    nodes = [{"id": f"b{i}", "source_file": "b.py"} for i in range(count)]
    links = [
        {"source": f"b{i}", "target": f"b{i + 1}", "relation": "calls"} for i in range(count - 1)
    ]
    return {"nodes": nodes, "links": links}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A repository with b.py on disk and a ten-node graph plus manifest already published."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "b.py").write_text("")
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph, manifest = out / "graph.json", out / "manifest.json"
    graph.write_text(json.dumps(chain(10)))
    manifest.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(out / ".graphify-php-status.json"))
    monkeypatch.setenv("GRAPHIFY_PHP_GRAPH", str(graph))
    for env in ("GRAPHIFY_SHRINK_POLICY", "GRAPHIFY_SHRINK_TOLERANCE", cli.MESH_MANAGED_ENV):
        monkeypatch.delenv(env, raising=False)
    return {"repo": repo, "graph": graph, "manifest": manifest}


def publishing(build, new_graph, new_manifest=None):
    """A stand-in for graphify that passes the resolver gate and publishes `new_graph`."""

    def run(args):
        build["graph"].write_text(json.dumps(new_graph))
        build["manifest"].write_text(json.dumps(new_manifest or {**MANIFEST, "c.py": {}}))
        status.write(
            [
                status.ResolverStatus(name=name, state="completed")
                for name in (cli.RESOLVER_NAME, cli.RESOLVER_NAME_ROUTES, cli.RESOLVER_NAME_CONFIG)
            ]
        )
        return 0

    return run


def test_refusal_restores_graph_and_manifest_and_exits_4(build, capsys):
    graph_before = build["graph"].read_bytes()
    manifest_before = build["manifest"].read_bytes()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(5)))

    assert code == cli.EXIT_SHRINK_REFUSED == 4
    assert build["graph"].read_bytes() == graph_before
    assert build["manifest"].read_bytes() == manifest_before
    err = capsys.readouterr().err
    assert "shrink guard (attributed) refused: nodes 10 -> 5" in err
    assert "unexplained node loss 5" in err
    assert "refusing to publish" in err
    assert not list(build["graph"].parent.glob("*.graphify-php-prev"))


def test_explained_shrink_publishes(build):
    (build["repo"] / "b.py").unlink()  # every lost node's file is gone from disk

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(0)))

    assert code == cli.EXIT_OK
    assert json.loads(build["graph"].read_text()) == chain(0)


def test_root_defaults_to_the_working_directory(build, monkeypatch):
    monkeypatch.chdir(build["repo"])
    (build["repo"] / "b.py").unlink()

    assert cli.main([], run_graphify=publishing(build, chain(0))) == cli.EXIT_OK


def test_root_comes_from_graphifys_positional_path(build, tmp_path, monkeypatch):
    # Run from a directory where b.py does not exist: only the passthrough root has it, so the
    # loss is unexplained there and the guard must refuse.
    monkeypatch.chdir(tmp_path)

    assert (
        cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(0)))
        == cli.EXIT_SHRINK_REFUSED
    )


def test_refusal_without_a_previous_manifest_drops_the_new_one(build):
    build["manifest"].unlink()

    # With no old manifest attribution cannot judge, so the count floor refuses 10 -> 0.
    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(0)))

    assert code == cli.EXIT_SHRINK_REFUSED
    assert not build["manifest"].exists()
    assert json.loads(build["graph"].read_text()) == chain(10)


def test_mesh_managed_skips_the_guard_and_says_so_once(build, capsys, monkeypatch):
    monkeypatch.setenv(cli.MESH_MANAGED_ENV, "1")

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(5)))

    assert code == cli.EXIT_OK
    assert json.loads(build["graph"].read_text()) == chain(5)
    assert capsys.readouterr().err.count("shrink guard left to graphify-mesh") == 1


def test_policy_off_disables_the_guard(build, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_SHRINK_POLICY", "off")

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(5)))

    assert code == cli.EXIT_OK


def test_first_build_has_nothing_to_compare(build):
    build["graph"].unlink()
    build["manifest"].unlink()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(5)))

    assert code == cli.EXIT_OK


def test_bad_tolerance_fails_closed(build, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_SHRINK_TOLERANCE", "lots")

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(10)))

    assert code == cli.EXIT_SHRINK_REFUSED
    assert "shrink guard could not run" in capsys.readouterr().err


def test_resolver_gate_refusal_still_takes_precedence(build):
    def failing(args):
        build["graph"].write_text(json.dumps(chain(5)))
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="failed")])
        return 0

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=failing)

    assert code in (cli.EXIT_RESOLVER_FAILED, cli.EXIT_RESOLVER_MISSING)
    assert json.loads(build["graph"].read_text()) == chain(10)


def test_missing_previous_manifest_falls_back_to_the_count_floor(build, capsys):
    # Attributed with an empty old manifest would call b.py changed and explain all five lost
    # nodes; the count floor (9 of 10) refuses.
    build["manifest"].unlink()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(5)))

    err = capsys.readouterr().err
    assert code == cli.EXIT_SHRINK_REFUSED
    assert "no previous manifest" in err and "using the count policy" in err
    assert "shrink guard (count) refused: nodes 10 -> 5" in err
    assert not build["manifest"].exists()
    assert json.loads(build["graph"].read_text()) == chain(10)


def test_missing_previous_manifest_still_accepts_a_build_that_did_not_shrink(build):
    build["manifest"].unlink()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(10)))

    assert code == cli.EXIT_OK
    assert build["manifest"].exists()


def test_any_guard_exception_fails_closed(build, capsys, monkeypatch):
    graph_before = build["graph"].read_bytes()
    manifest_before = build["manifest"].read_bytes()

    def crash(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(cli.shrink, "classify_loss", crash)

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=publishing(build, chain(10)))

    assert code == cli.EXIT_SHRINK_REFUSED
    assert build["graph"].read_bytes() == graph_before
    assert build["manifest"].read_bytes() == manifest_before
    assert "shrink guard could not run: KeyError: 'boom'" in capsys.readouterr().err
    assert not list(build["graph"].parent.glob("*.graphify-php-prev"))


def failing_resolver(build):
    def run(args):
        build["graph"].write_text(json.dumps(chain(5)))
        build["manifest"].write_text(json.dumps({**MANIFEST, "c.py": {}}))
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="failed")])
        return 0

    return run


def test_resolver_gate_refusal_restores_the_manifest(build):
    manifest_before = build["manifest"].read_bytes()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=failing_resolver(build))

    assert code == cli.EXIT_RESOLVER_FAILED
    assert build["manifest"].read_bytes() == manifest_before
    assert not list(build["graph"].parent.glob("*.graphify-php-prev"))


def test_resolver_gate_refusal_drops_a_manifest_the_build_created(build):
    build["manifest"].unlink()

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=failing_resolver(build))

    assert code == cli.EXIT_RESOLVER_FAILED
    assert not build["manifest"].exists()
    assert json.loads(build["graph"].read_text()) == chain(10)


def test_repo_root_is_resolved_through_a_symlink(build, tmp_path, monkeypatch):
    link = tmp_path / "link"
    link.symlink_to(build["repo"], target_is_directory=True)
    monkeypatch.chdir(link)

    assert cli.repo_root_from(["update", str(link)]) == str(build["repo"].resolve())
    assert cli.repo_root_from(["update"]) == str(build["repo"].resolve())
