"""The node index and edge sink, against graphify's real node and edge shapes.

The fixtures below copy the shapes read from a real build of the reference application:
class nodes carry `_callable_class` and
a short label, method nodes hang off them by a `method` edge with a `.name()` label, and a class
hangs off its file node by `contains`.
"""

from __future__ import annotations

from graphify_php.graph_adapter import (
    EDGE_MARKER,
    EDGE_ORIGIN,
    GraphEdgeSink,
    GraphNodeIndex,
    GraphStubSink,
)
from graphify_php.ports import CallSite, CallTarget, Confidence


def graph(*classes):
    """Build nodes/edges for `(file_path, class_label, [method_labels])` triples."""
    nodes, edges = [], []
    for path, label, methods in classes:
        file_nid = path.replace("/", "_").replace(".php", "").lower()
        class_nid = f"{file_nid}_{label.lower()}"
        nodes.append({"id": file_nid, "label": path.rsplit("/", 1)[-1], "source_file": path})
        nodes.append(
            {
                "id": class_nid,
                "label": label,
                "_callable": True,
                "_callable_class": True,
                "source_file": path,
                "_origin": "ast",
            }
        )
        edges.append({"source": file_nid, "target": class_nid, "relation": "contains"})
        for method in methods:
            method_nid = f"{class_nid}_{method.lower()}"
            nodes.append(
                {
                    "id": method_nid,
                    "label": f".{method}()",
                    "_callable": True,
                    "source_file": path,
                    "_origin": "ast",
                }
            )
            edges.append({"source": class_nid, "target": method_nid, "relation": "method"})
    return nodes, edges


def test_method_node_found_through_method_edge():
    nodes, edges = graph(("src/Service/Emails.php", "Emails", ["reminderSubmitWeek"]))

    index = GraphNodeIndex(nodes, edges)

    assert (
        index.method_node("App\\Service\\Emails", "reminderSubmitWeek")
        == "src_service_emails_emails_reminderSubmitWeek".lower()
    )


def test_method_lookup_is_label_normalised():
    nodes, edges = graph(("src/Service/Emails.php", "Emails", ["sendNow"]))
    index = GraphNodeIndex(nodes, edges)

    assert index.method_node("App\\Service\\Emails", "sendnow") is not None


def test_unknown_class_returns_none():
    nodes, edges = graph(("src/Service/Emails.php", "Emails", ["send"]))

    assert GraphNodeIndex(nodes, edges).method_node("App\\Service\\Missing", "send") is None


def test_unknown_method_returns_none():
    nodes, edges = graph(("src/Service/Emails.php", "Emails", ["send"]))

    assert GraphNodeIndex(nodes, edges).method_node("App\\Service\\Emails", "store") is None


def test_class_not_contained_by_a_file_is_not_a_candidate():
    nodes, edges = graph(("src/Service/Emails.php", "Emails", ["send"]))
    edges = [e for e in edges if e["relation"] != "contains"]

    assert GraphNodeIndex(nodes, edges).method_node("App\\Service\\Emails", "send") is None


def test_same_short_name_is_separated_by_the_namespace():
    nodes, edges = graph(
        ("src/Mail/Emails.php", "Emails", ["send"]),
        ("src/Legacy/Emails.php", "Emails", ["send"]),
    )
    index = GraphNodeIndex(nodes, edges)

    assert index.method_node("App\\Mail\\Emails", "send") == "src_mail_emails_emails_send"
    assert index.method_node("App\\Legacy\\Emails", "send") == "src_legacy_emails_emails_send"


def test_same_short_name_the_namespace_cannot_separate_resolves_to_nothing():
    nodes, edges = graph(
        ("src/A/Emails.php", "Emails", ["send"]),
        ("src/B/Emails.php", "Emails", ["send"]),
    )

    assert GraphNodeIndex(nodes, edges).method_node("App\\Other\\Emails", "send") is None


def test_foreign_namespace_does_not_bind_to_a_unique_local_short_name():
    nodes, edges = graph(("src/Mail/Emails.php", "Emails", ["send"]))

    assert GraphNodeIndex(nodes, edges).method_node("Vendor\\Lib\\Emails", "send") is None


def test_root_namespace_only_still_resolves():
    nodes, edges = graph(("src/Emails.php", "Emails", ["send"]))

    assert (
        GraphNodeIndex(nodes, edges).method_node("App\\Emails", "send") == "src_emails_emails_send"
    )


def test_three_segment_namespace_with_overlap_one_does_not_bind():
    nodes, edges = graph(("src/Mail/Emails.php", "Emails", ["send"]))

    # One shared directory out of two non-root segments is a coincidence, not a PSR-4 match.
    assert GraphNodeIndex(nodes, edges).method_node("Vendor\\Lib\\Mail\\Emails", "send") is None


def test_deep_project_namespace_mirrored_by_the_path_still_binds():
    nodes, edges = graph(("src/Service/Mail/Emails.php", "Emails", ["send"]))

    assert (
        GraphNodeIndex(nodes, edges).method_node("App\\Service\\Mail\\Emails", "send")
        == "src_service_mail_emails_emails_send"
    )


def _caller_extending(nodes, edges, parent_nid):
    """A caller method in its own file whose class inherits from `parent_nid`."""
    nodes.append(
        {"id": "src_controller_c", "label": "C.php", "source_file": "src/Controller/C.php"}
    )
    nodes.append(
        {
            "id": "src_controller_c_c",
            "label": "C",
            "_callable_class": True,
            "source_file": "src/Controller/C.php",
        }
    )
    nodes.append(
        {
            "id": "src_controller_c_c_run",
            "label": ".run()",
            "_callable": True,
            "source_file": "src/Controller/C.php",
        }
    )
    edges.append(
        {"source": "src_controller_c", "target": "src_controller_c_c", "relation": "contains"}
    )
    edges.append(
        {"source": "src_controller_c_c", "target": "src_controller_c_c_run", "relation": "method"}
    )
    edges.append({"source": "src_controller_c_c", "target": parent_nid, "relation": "inherits"})
    return "src_controller_c_c_run"


def test_visible_candidate_loses_to_one_the_proved_namespace_names():
    nodes, edges = graph(
        ("src/Mail/Emails.php", "Emails", ["send"]),
        ("src/Legacy/Emails.php", "Emails", ["send"]),
    )
    caller = _caller_extending(nodes, edges, "src_legacy_emails_emails")
    index = GraphNodeIndex(nodes, edges)

    # The type source proved App\Mail\Emails. The Legacy class is the one the caller can see
    # by inheritance, but binding there contradicts the proof: no edge at all.
    assert index.method_node_for(caller, "App\\Mail\\Emails", "send") is None
    assert (
        index.method_node_for(caller, "App\\Legacy\\Emails", "send")
        == "src_legacy_emails_emails_send"
    )


def _target(via="parser", confidence=Confidence.EXTRACTED):
    site = CallSite(
        caller_nid="caller",
        receiver="",
        method="send",
        source_file="src/Controller/C.php",
        source_location="L42",
    )
    return CallTarget(
        site=site, class_fqn="App\\Mail\\Emails", method="send", confidence=confidence, via=via
    )


def test_sink_appends_a_marked_calls_edge():
    all_edges = []
    GraphEdgeSink(all_edges).add_call("caller", "callee", _target())

    edge = all_edges[0]
    assert edge["relation"] == "calls"
    assert edge[EDGE_MARKER] == EDGE_ORIGIN
    assert edge["_via"] == "parser"
    assert edge["confidence"] == "EXTRACTED"
    assert edge["confidence_score"] == 1.0
    assert edge["source_location"] == "L42"


def test_sink_carries_the_targets_confidence():
    all_edges = []
    GraphEdgeSink(all_edges).add_call("caller", "callee", _target(confidence=Confidence.INFERRED))

    assert all_edges[0]["confidence"] == "INFERRED"
    assert all_edges[0]["confidence_score"] == 0.8


def test_sink_does_not_duplicate_a_pair_graphify_already_has():
    all_edges = [{"source": "caller", "target": "callee", "relation": "references"}]
    sink = GraphEdgeSink(all_edges)

    sink.add_call("caller", "callee", _target())

    assert len(all_edges) == 1
    assert sink.added == 0


def test_sink_does_not_duplicate_its_own_edge():
    all_edges = []
    sink = GraphEdgeSink(all_edges)

    sink.add_call("caller", "callee", _target())
    sink.add_call("caller", "callee", _target())

    assert sink.added == 1


def _vendor_target(method="flush", class_fqn="Doctrine\\ORM\\EntityManager"):
    site = CallSite("caller", "$this->em", method, "src/A.php", "L42")
    return CallTarget(
        site=site,
        class_fqn=class_fqn,
        method=method,
        confidence=Confidence.EXTRACTED,
        via="phpstan",
        declaring_file="/repo/vendor/doctrine/orm/EntityManager.php",
    )


def test_stub_sink_mints_one_node_per_declaring_class():
    nodes, edges = [], []
    sink = GraphStubSink(nodes, edges)

    first = sink.node_for("Doctrine\\ORM\\EntityManager")
    second = sink.node_for("Doctrine\\ORM\\EntityManager")

    assert first == second == "php_external_doctrine_orm_entitymanager"
    assert sink.nodes_added == 1
    assert nodes == [
        {
            "id": "php_external_doctrine_orm_entitymanager",
            "label": "Doctrine\\ORM\\EntityManager",
            "file_type": "concept",
            "type": "external",
            "external": True,
            "source_file": "",
            EDGE_MARKER: EDGE_ORIGIN,
        }
    ]


def test_stub_sink_id_replaces_every_character_outside_the_safe_set():
    sink = GraphStubSink([], [])
    assert (
        sink.node_for("Vendor\\Lib\\Thing-Ünïcode$1") == "php_external_vendor_lib_thing__n_code_1"
    )


def test_stub_sink_reuses_a_stub_already_in_the_graph():
    nodes = [{"id": "php_external_doctrine_orm_entitymanager", "label": "x", "external": True}]
    sink = GraphStubSink(nodes, [])

    assert (
        sink.node_for("Doctrine\\ORM\\EntityManager") == "php_external_doctrine_orm_entitymanager"
    )
    assert sink.nodes_added == 0
    assert len(nodes) == 1


def test_stub_sink_edge_matches_the_call_edge_and_names_the_method():
    nodes, edges = [], []
    sink = GraphStubSink(nodes, edges)
    target = _vendor_target()
    stub = sink.node_for(target.class_fqn)

    sink.add_call("caller", stub, target)

    assert sink.edges_added == 1
    call_edges = []
    GraphEdgeSink(call_edges).add_call("caller", stub, target)
    expected = dict(call_edges[0], _external_target="Doctrine\\ORM\\EntityManager::flush")
    assert edges == [expected]


def test_stub_sink_dedupes_by_caller_and_stub_so_two_methods_on_one_class_make_one_edge():
    nodes, edges = [], []
    sink = GraphStubSink(nodes, edges)
    stub = sink.node_for("Doctrine\\ORM\\EntityManager")

    sink.add_call("caller", stub, _vendor_target("flush"))
    sink.add_call("caller", stub, _vendor_target("persist"))

    assert sink.edges_added == 1
    assert len(edges) == 1
    assert sink.nodes_added == 1


def test_stub_sink_does_not_duplicate_a_pair_the_graph_already_has():
    edges = [
        {
            "source": "caller",
            "target": "php_external_doctrine_orm_entitymanager",
            "relation": "calls",
        }
    ]
    sink = GraphStubSink([], edges)
    stub = sink.node_for("Doctrine\\ORM\\EntityManager")

    sink.add_call("caller", stub, _vendor_target())

    assert sink.edges_added == 0
    assert len(edges) == 1
