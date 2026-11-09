"""The writer side of the attribution sidecar: `graphify-php-build` turns the status records into
`.graphify-php-attribution.json` after the resolver gate passes and before the shrink guard, and
the guard's reader (`shrink.classify_loss`) attributes exactly what the build recorded.
"""

from __future__ import annotations

import json

import pytest

from graphify_php import cli, core_guard, shrink, status
from graphify_php.resolver import EXTERNAL_STUBS_ENV

MANIFEST = {"src/locals_new.php": {"mtime": 0, "ast_hash": "h", "semantic_hash": "h"}}


def core_row(source="caller", target="emails_get", proof="cache_get") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": "calls",
        "reason": shrink.REASON_CORE_SUPPRESSED,
        "proof": {"source": source, "target": proof},
    }


def engine_row(source="caller", target="local_get", proof="cache_get") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": "calls",
        "reason": shrink.REASON_ENGINE_CONTRADICTS,
        "proof": {"source": source, "target": proof},
    }


def records(guard_rows=(), engine_rows=(), guard_state="completed", member_state="completed"):
    return {
        "resolvers": [
            {
                "name": cli.RESOLVER_NAME_CORE_GUARD,
                "state": guard_state,
                "suppressed_edge_rows": list(guard_rows),
            },
            {
                "name": cli.RESOLVER_NAME,
                "state": member_state,
                "engine_dropped_edges": list(engine_rows),
                "external_stubs": shrink.STUBS_OFF,
            },
        ]
    }


@pytest.fixture(autouse=True)
def stubs_env(monkeypatch):
    monkeypatch.delenv(EXTERNAL_STUBS_ENV, raising=False)


# --- the document ---------------------------------------------------------------------------


def test_document_lists_guard_rows_and_engine_rows_with_counts_per_reason():
    doc = cli.attribution_document(records([core_row()], [engine_row()]))

    assert doc == {
        "schema": shrink.ATTRIBUTION_SCHEMA,
        "producer": shrink.ATTRIBUTION_PRODUCER,
        "external_stubs": shrink.STUBS_OFF,
        "removed_edges": [core_row(), engine_row()],
        "counts": {shrink.REASON_CORE_SUPPRESSED: 1, shrink.REASON_ENGINE_CONTRADICTS: 1},
    }
    assert shrink._parse_attribution(doc).counts == doc["counts"], "the reader accepts it"


def test_an_engine_row_whose_proof_has_no_node_is_left_out():
    unproved = engine_row(proof=None)

    doc = cli.attribution_document(records(engine_rows=[unproved, engine_row(target="b")]))

    assert doc["removed_edges"] == [engine_row(target="b")]
    assert doc["counts"][shrink.REASON_ENGINE_CONTRADICTS] == 1


@pytest.mark.parametrize(
    "bad",
    [
        "not a row",
        {**core_row(), "source": ""},
        {**core_row(), "target": 7},
        {**core_row(), "relation": None},
        {**core_row(), "proof": "cache_get"},
        {**core_row(), "proof": {"source": "other", "target": "cache_get"}},
        {**core_row(), "reason": shrink.REASON_ENGINE_CONTRADICTS},
    ],
    ids=[
        "str",
        "empty-source",
        "int-target",
        "no-relation",
        "flat-proof",
        "foreign-proof",
        "reason",
    ],
)
def test_a_row_the_reader_could_not_confirm_is_left_out_and_the_rest_stand(bad):
    doc = cli.attribution_document(records([bad, core_row()]))

    assert doc["removed_edges"] == [core_row()]
    shrink._parse_attribution(doc)  # one malformed row would make the reader ignore it all


def test_rows_of_a_resolver_that_did_not_complete_are_not_written():
    doc = cli.attribution_document(
        records([core_row()], [engine_row()], guard_state="skipped", member_state="failed")
    )

    assert doc["removed_edges"] == []
    assert doc["counts"] == {r: 0 for r in shrink.REMOVAL_REASONS}


def test_a_row_listed_twice_is_written_and_counted_once():
    doc = cli.attribution_document(records([core_row(), core_row()]))

    assert doc["removed_edges"] == [core_row()]
    assert doc["counts"][shrink.REASON_CORE_SUPPRESSED] == 1


def test_only_the_schema_keys_of_a_row_are_written():
    doc = cli.attribution_document(records([{**core_row(), "source_location": "L20"}]))

    assert doc["removed_edges"] == [core_row()]


@pytest.mark.parametrize("stated", [shrink.STUBS_ON, shrink.STUBS_OFF])
def test_external_stubs_is_what_the_record_states_whatever_the_environment(monkeypatch, stated):
    # The record is the single source: the wrapper's environment is not the build's process.
    monkeypatch.setenv(EXTERNAL_STUBS_ENV, "0" if stated == shrink.STUBS_ON else "1")
    record = records()
    record["resolvers"][1]["external_stubs"] = stated

    assert cli.attribution_document(record)["external_stubs"] == stated


@pytest.mark.parametrize("unstated", [None, "", "maybe"])
def test_external_stubs_not_stated_is_on_which_attributes_no_stub_loss(monkeypatch, unstated):
    monkeypatch.setenv(EXTERNAL_STUBS_ENV, "0")
    record = records()
    if unstated is None:
        del record["resolvers"][1]["external_stubs"]
    else:
        record["resolvers"][1]["external_stubs"] = unstated

    assert cli.attribution_document(record)["external_stubs"] == shrink.STUBS_ON
    assert cli.attribution_document({})["external_stubs"] == shrink.STUBS_ON


def test_no_record_still_gives_a_valid_empty_document():
    doc = cli.attribution_document({})

    assert doc["removed_edges"] == [] and set(doc["counts"]) == set(shrink.REMOVAL_REASONS)
    shrink._parse_attribution(doc)


# --- the write ------------------------------------------------------------------------------


def test_write_is_atomic_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(tmp_path / "status.json"))

    path = cli.write_attribution(records([core_row()]))

    assert path == shrink.attribution_path(tmp_path / "status.json")
    assert json.loads(path.read_text())["removed_edges"] == [core_row()]
    assert [p.name for p in tmp_path.iterdir()] == [shrink.ATTRIBUTION_FILENAME]


def test_a_failed_write_is_reported_and_returns_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(tmp_path / "missing-dir" / "status.json"))

    assert cli.write_attribution(records([core_row()])) is None
    assert "attribution sidecar not written" in capsys.readouterr().err


# --- inside the build -----------------------------------------------------------------------


def method_graph(with_core_edge: bool) -> dict:
    nodes = [
        {"id": nid, "label": label, "file_type": "code", "source_file": "src/locals_new.php"}
        for nid, label in (
            ("file", "locals_new.php"),
            ("emails", "Emails"),
            ("emails_get", ".get()"),
            ("cache", "Cache"),
            ("cache_get", ".get()"),
            ("factory", "Factory"),
            ("caller", ".reassigned()"),
        )
    ]
    links = [
        {"source": "file", "target": t, "relation": "contains"}
        for t in ("emails", "cache", "factory")
    ] + [
        {"source": "emails", "target": "emails_get", "relation": "method"},
        {"source": "cache", "target": "cache_get", "relation": "method"},
        {"source": "factory", "target": "caller", "relation": "method"},
    ]
    if with_core_edge:
        links.append({"source": "caller", "target": "emails_get", "relation": "calls"})
    return {"nodes": nodes, "links": links}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A published graph holding core's guess caller -> Emails::get, the file unchanged on disk."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "locals_new.php").write_text("")
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph, manifest = out / "graph.json", out / "manifest.json"
    graph.write_text(json.dumps(method_graph(with_core_edge=True)))
    manifest.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(out / ".graphify-php-status.json"))
    monkeypatch.setenv("GRAPHIFY_PHP_GRAPH", str(graph))
    monkeypatch.setenv("GRAPHIFY_SHRINK_TOLERANCE", "0")
    for env in ("GRAPHIFY_SHRINK_POLICY", cli.MESH_MANAGED_ENV):
        monkeypatch.delenv(env, raising=False)
    return {"repo": repo, "graph": graph, "manifest": manifest, "out": out}


def guarded_build(build, core):
    """A stand-in for graphify that runs the real core guard over the proved `L20` site.

    Our pass edged caller -> Cache::get; core's guess caller -> Emails::get is withheld.
    """
    registry = pytest.importorskip("graphify.resolver_registry")

    def run(args):
        graph = method_graph(with_core_edge=False)
        nodes, edges = graph["nodes"], graph["links"]
        edges.append(
            {
                "source": "caller",
                "target": "cache_get",
                "relation": "calls",
                "source_location": "L20",
                "_resolver": "graphify-php",
            }
        )
        raw = {
            "caller_nid": "caller",
            "callee": "get",
            "receiver": "mailer",
            "is_member_call": True,
            "source_file": "src/locals_new.php",
            "source_location": "L20",
        }
        table = {"path": "src/locals_new.php", "table": {"mailer": "Emails"}}
        per_file = [{"php_type_table": table, "raw_calls": [raw]}]
        saved = registry._REGISTRY[:]
        try:
            registry._REGISTRY[:] = [registry.LanguageResolver("php_member_calls", {".php"}, core)]
            core_guard.install(registry)
            core_guard.prove(per_file, [raw])
            registry._REGISTRY[0].resolve(per_file, nodes, edges)
        finally:
            registry._REGISTRY[:] = saved
        status.write(
            [
                status.ResolverStatus(name=name, state="completed")
                for name in (cli.RESOLVER_NAME, cli.RESOLVER_NAME_ROUTES, cli.RESOLVER_NAME_CONFIG)
            ]
        )
        build["graph"].write_text(json.dumps({"nodes": nodes, "links": edges}))
        build["manifest"].write_text(json.dumps(MANIFEST))
        return 0

    return run


def real_core():
    extract = pytest.importorskip("graphify.extract")
    return extract._resolve_php_member_calls


def test_the_withheld_core_edge_is_attributed_end_to_end(build):
    code = cli.main(
        ["--", "update", str(build["repo"])], run_graphify=guarded_build(build, real_core())
    )

    assert code == cli.EXIT_OK
    sidecar = json.loads(shrink.attribution_path(status.status_path()).read_text())
    assert sidecar["removed_edges"] == [core_row()]
    assert sidecar["counts"][shrink.REASON_CORE_SUPPRESSED] == 1
    published = json.loads(build["graph"].read_text())
    assert {"source": "caller", "target": "emails_get", "relation": "calls"} not in [
        {k: e.get(k) for k in ("source", "target", "relation")} for e in published["links"]
    ]
    # The reader on its own, as graphify-mesh runs it: the loss is attributed, not explained.
    report = shrink.classify_loss(
        method_graph(with_core_edge=True),
        published,
        MANIFEST,
        MANIFEST,
        build["repo"],
        tolerance=0.0,
        attribution=sidecar,
    )
    assert report.verdict == "accept"
    assert report.edges["attributed"] == {shrink.REASON_CORE_SUPPRESSED: 1}
    assert report.edges["unexplained"] == 0


def test_a_dry_run_failure_leaves_the_loss_unattributed_and_refused(build):
    calls: list = []

    def core(per_file, all_nodes, all_edges):
        calls.append(per_file)
        if len(calls) == 2:
            raise RuntimeError("dry run broke")

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=guarded_build(build, core))

    assert code == cli.EXIT_SHRINK_REFUSED
    rows = {r["name"]: r for r in status.read()["resolvers"]}
    guard = rows[cli.RESOLVER_NAME_CORE_GUARD]
    assert guard["state"] == "completed"
    assert guard["suppressed_unlisted"] == {core_guard.REASON_DRY_RUN_FAILED: 1}
    sidecar = json.loads(shrink.attribution_path(status.status_path()).read_text())
    assert sidecar["removed_edges"] == []


def test_the_sidecar_is_written_when_graphify_mesh_runs_the_guard(build, monkeypatch, capsys):
    monkeypatch.setenv(cli.MESH_MANAGED_ENV, "1")

    code = cli.main(
        ["--", "update", str(build["repo"])], run_graphify=guarded_build(build, real_core())
    )

    assert code == cli.EXIT_OK
    assert "shrink guard left to graphify-mesh" in capsys.readouterr().err
    sidecar = json.loads(shrink.attribution_path(status.status_path()).read_text())
    assert sidecar["removed_edges"] == [core_row()]


def test_no_sidecar_when_the_resolver_gate_refuses(build):
    def run(args):
        status.write([status.ResolverStatus(name=cli.RESOLVER_NAME, state="failed")])
        return 0

    code = cli.main(["--", "update", str(build["repo"])], run_graphify=run)

    assert code == cli.EXIT_RESOLVER_FAILED
    assert not shrink.attribution_path(status.status_path()).exists()
