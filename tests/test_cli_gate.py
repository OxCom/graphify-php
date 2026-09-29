"""The publish gate: which resolver states let a graph through, and what happens when one does not."""

from __future__ import annotations

import json

import pytest

from graphify_php import cli, status


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A repository with a previous graph already published and a status path of its own."""
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text(json.dumps({"nodes": ["previous"], "links": []}))
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(out / ".graphify-php-status.json"))
    monkeypatch.setenv("GRAPHIFY_PHP_GRAPH", str(graph))
    return graph


def runner_writing(state, *, name=cli.RESOLVER_NAME, reason="", graph=None):
    """A stand-in for graphify: publishes a new graph, then leaves the record `state` describes."""
    def run(args):
        if graph is not None:
            graph.write_text(json.dumps({"nodes": ["fresh"], "links": []}))
        if state is not None:
            records = [status.ResolverStatus(name=name, state=state, reason=reason)]
            if name != cli.RESOLVER_NAME_ROUTES:
                # Every resolver this package registers must report, so a test about one of
                # them still has to satisfy the other or it is testing the wrong refusal.
                records.append(status.ResolverStatus(name=cli.RESOLVER_NAME_ROUTES,
                                                     state="completed"))
            status.write(records)
        return 0
    return run


def test_completed_publishes(build):
    code = cli.main([], run_graphify=runner_writing("completed", graph=build))

    assert code == cli.EXIT_OK
    assert json.loads(build.read_text())["nodes"] == ["fresh"]


def test_skipped_with_a_reason_publishes(build):
    code = cli.main([], run_graphify=runner_writing(
        "skipped", reason="phpstan: vendor/bin/phpstan not found", graph=build))

    assert code == cli.EXIT_OK
    assert json.loads(build.read_text())["nodes"] == ["fresh"]


def test_failed_refuses_and_restores_the_previous_graph(build, capsys):
    code = cli.main([], run_graphify=runner_writing(
        "failed", reason="RuntimeError: boom", graph=build))

    assert code == cli.EXIT_RESOLVER_FAILED
    assert json.loads(build.read_text())["nodes"] == ["previous"]
    err = capsys.readouterr().err
    assert "php_member_calls" in err and "state=failed" in err and "boom" in err
    assert "refusing to publish" in err


def test_no_record_at_all_refuses(build, capsys):
    code = cli.main([], run_graphify=runner_writing(None, graph=build))

    assert code == cli.EXIT_RESOLVER_MISSING
    assert json.loads(build.read_text())["nodes"] == ["previous"]
    assert "never reported" in capsys.readouterr().err


def test_a_record_for_another_resolver_does_not_satisfy_the_gate(build, capsys):
    code = cli.main([], run_graphify=runner_writing("completed", name="something_else",
                                                    graph=build))

    assert code == cli.EXIT_RESOLVER_MISSING
    assert "php_member_calls" in capsys.readouterr().err


def test_a_stale_record_from_an_earlier_build_does_not_satisfy_the_gate(build, capsys):
    status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="completed")])

    code = cli.main([], run_graphify=runner_writing(None, graph=build))

    assert code == cli.EXIT_RESOLVER_MISSING
    assert json.loads(build.read_text())["nodes"] == ["previous"]


def test_an_unfinished_record_is_treated_as_a_failure(build):
    code = cli.main([], run_graphify=runner_writing("started", graph=build))

    assert code == cli.EXIT_RESOLVER_FAILED


def test_refusal_with_no_previous_graph_publishes_nothing(build):
    build.unlink()

    code = cli.main([], run_graphify=runner_writing("failed", reason="boom", graph=build))

    assert code == cli.EXIT_RESOLVER_FAILED
    assert not build.exists()


def test_graphify_own_exit_code_is_passed_through(build, capsys):
    code = cli.main([], run_graphify=lambda args: 7)

    assert code == 7
    assert "graphify exited 7" in capsys.readouterr().err


def test_arguments_after_the_separator_reach_graphify(build):
    seen = {}

    def run(args):
        seen["args"] = list(args)
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="completed")])
        return 0

    cli.main(["--expect", cli.RESOLVER_NAME, "--", "build", "--no-llm"], run_graphify=run)

    assert seen["args"] == ["build", "--no-llm"]


def test_arguments_without_a_separator_all_reach_graphify(build):
    seen = {}

    def run(args):
        seen["args"] = list(args)
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="completed")])
        return 0

    cli.main(["build", "--no-llm"], run_graphify=run)

    assert seen["args"] == ["build", "--no-llm"]


def test_extra_expected_resolver_must_also_report(build):
    code = cli.main(["--expect", cli.RESOLVER_NAME, "--expect", "php_attributes", "--"],
                    run_graphify=runner_writing("completed", graph=build))

    assert code == cli.EXIT_RESOLVER_MISSING


def test_the_default_gate_requires_every_resolver_this_package_registers(build, capsys):
    def only_member_calls(args):
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="completed")])
        return 0

    code = cli.main([], run_graphify=only_member_calls)

    assert code == cli.EXIT_RESOLVER_MISSING
    assert cli.RESOLVER_NAME_ROUTES in capsys.readouterr().err


def test_check_is_pure_and_needs_no_build():
    ok, message = cli.check({"resolvers": [{"name": "r", "state": "skipped"}]}, ["r"])
    assert (ok, message) == (cli.EXIT_OK, "")

    bad, message = cli.check({"resolvers": [{"name": "r", "state": "failed"}]}, ["r"])
    assert bad == cli.EXIT_RESOLVER_FAILED
    assert "no reason recorded" in message
