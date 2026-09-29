"""The pass as graphify runs it: files in, nodes and edges out, a status record either way."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import canaries as cn  # noqa: E402
from graphify_php import config_resolver as cr  # noqa: E402
from graphify_php.config.document import file_key  # noqa: E402
from graphify_php.config.environment import env_key  # noqa: E402
from graphify_php.config.messaging import RELATION_ROUTED_TO, transport_key  # noqa: E402

MESSENGER_YAML = """
    framework:
        messenger:
            routing:
                'App\\Cron\\Message\\AbstractCronMessage': cron
                'App\\Messages\\TimezoneInitializedMessage': cron
"""

SERVICES_YAML = """
    parameters:
        client_secret: '%env(MICROSOFT_CLIENT_SECRET)%'
"""


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())
    return tmp_path


def php_class_node(nid: str, label: str, source_file: str) -> dict:
    return {"id": nid, "label": label, "source_file": source_file, "_callable_class": True}


def contains(file_nid: str, class_nid: str) -> dict:
    return {"source": file_nid, "target": class_nid, "relation": "contains"}


@pytest.fixture(autouse=True)
def status_in_tmp(tmp_path, monkeypatch):
    """Every run writes a record; none of them may land in the working directory."""
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))


def run(root: Path, nodes=None, edges=None):
    nodes = [] if nodes is None else nodes
    edges = [] if edges is None else edges
    record = cr.run([], nodes, edges, repo_root=str(root))
    return record, nodes, edges


def test_a_repository_without_config_is_a_skip_not_a_failure(tmp_path):
    record, nodes, edges = run(repo(tmp_path, {"src/Demo.php": "<?php\n"}))

    assert record.state == "skipped"
    assert "no config/ directory" in record.reason
    assert (nodes, edges) == ([], [])


def test_routing_reaches_the_message_class_node_when_the_graph_has_it(tmp_path):
    root = repo(tmp_path, {"config/packages/messenger.yaml": MESSENGER_YAML})
    nodes = [php_class_node("m1", "AbstractCronMessage", "src/Cron/Message/AbstractCronMessage.php")]
    edges = [contains("f1", "m1")]

    record, nodes, edges = run(root, nodes, edges)

    routed = [e for e in edges if e["relation"] == RELATION_ROUTED_TO]
    assert [(e["source"], e["target"]) for e in routed] == [("m1", transport_key("cron"))]
    assert record.state == "completed"
    # The second message class is not in this graph. The transport still is.
    assert record.unresolved["message_class_not_in_graph"] == 1
    assert transport_key("cron") in {n["id"] for n in nodes}


def test_a_message_class_outside_the_graph_keeps_its_transport_and_says_so(tmp_path):
    root = repo(tmp_path, {"config/packages/messenger.yaml": MESSENGER_YAML})
    record, nodes, edges = run(root)

    assert record.state == "completed"
    assert record.unresolved["message_class_not_in_graph"] == 2
    assert {n["id"] for n in nodes} == {file_key("config/packages/messenger.yaml"),
                                        transport_key("cron")}
    assert [e["relation"] for e in edges] == ["declares_transport"]


def test_environment_references_become_nodes_the_file_points_at(tmp_path):
    record, nodes, edges = run(repo(tmp_path, {"config/services.yaml": SERVICES_YAML}))

    assert env_key("MICROSOFT_CLIENT_SECRET") in {n["id"] for n in nodes}
    assert [(e["source"], e["target"]) for e in edges if e["relation"] == "reads_env"] == [
        (file_key("config/services.yaml"), env_key("MICROSOFT_CLIENT_SECRET"))]
    assert record.resolved_sites == 1


def test_one_variable_across_two_files_is_one_node_with_two_edges(tmp_path):
    root = repo(tmp_path, {
        "config/services.yaml": SERVICES_YAML,
        "config/packages/mailer.yaml": """
            framework:
                mailer:
                    dsn: '%env(MICROSOFT_CLIENT_SECRET)%'
        """,
    })
    _, nodes, edges = run(root)

    assert len([n for n in nodes if n["id"] == env_key("MICROSOFT_CLIENT_SECRET")]) == 1
    assert len([e for e in edges if e["relation"] == "reads_env"]) == 2


def test_a_malformed_file_is_counted_and_the_rest_is_still_indexed(tmp_path):
    root = repo(tmp_path, {
        "config/packages/messenger.yaml": MESSENGER_YAML,
        "config/broken.yaml": "parameters: [unclosed\n",
    })
    record, nodes, _ = run(root)

    assert record.state == "completed"
    assert record.files_seen == 2
    assert record.resolved_sites == 1
    assert record.unresolved["safedocumentparser:yaml_malformed"] == 1
    assert transport_key("cron") in {n["id"] for n in nodes}


def test_a_file_this_package_indexes_nothing_from_gets_no_node(tmp_path):
    root = repo(tmp_path, {"config/packages/cache.yaml": """
        framework:
            cache:
                app: cache.adapter.filesystem
    """})
    record, nodes, edges = run(root)

    assert (nodes, edges) == ([], [])
    assert record.unresolved["document_held_no_indexed_fact"] == 1


def test_the_status_record_is_written_even_when_nothing_was_indexed(tmp_path):
    run(repo(tmp_path, {"src/Demo.php": "<?php\n"}))
    written = json.loads(Path(tmp_path / "status.json").read_text())

    entry = next(r for r in written["resolvers"] if r["name"] == cr.RESOLVER_NAME)
    assert entry["state"] == "skipped"
    assert entry["finished_at"] > 0


def test_the_gate_is_php_because_a_yaml_gate_would_never_fire():
    """graphify activates on the corpus it built, and YAML is not in it (detect.py:45).

    Measured: a fixture of one PHP file and one `config/packages/messenger.yaml`, built with a
    `.php`-gated and a `.yaml`-gated probe registered, ran only the `.php` one.
    """
    from graphify_php.resolver import PHP_SUFFIXES
    assert PHP_SUFFIXES == frozenset({".php"})


def test_no_planted_value_reaches_a_node_an_edge_or_the_record(tmp_path):
    """The rule this package is held to: identifiers may be indexed, values never."""
    yaml_canary = next(c for c in cn.CANARIES if c.name == "yaml_value")
    dsn_canary = next(c for c in cn.CANARIES if c.name == "dsn")
    default_canary = next(c for c in cn.CANARIES if c.name == "env_default")
    root = repo(tmp_path, {
        "config/services.yaml": f"""
            parameters:
                {yaml_canary.identifier}: '{yaml_canary.value}'
                fallback_dsn: '{dsn_canary.value}'
                api_key: '%env(default:{default_canary.value}:{default_canary.identifier})%'
        """,
        "config/packages/messenger.yaml": MESSENGER_YAML,
    })
    record, nodes, edges = run(root)

    cn.assert_no_leaks({
        "nodes": json.dumps(nodes),
        "edges": json.dumps(edges),
        "status": Path(tmp_path / "status.json").read_text(),
        "record": repr(record),
    })
    # The check would pass on a graph that indexed nothing, so assert the identifier survived.
    assert env_key(default_canary.identifier) in {n["id"] for n in nodes}
