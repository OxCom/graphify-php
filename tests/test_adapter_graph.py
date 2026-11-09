"""The node index and edge sink, against graphify's real node and edge shapes.

The fixtures below copy the shapes read from a real build of the reference application:
class nodes carry `_callable_class` and
a short label, method nodes hang off them by a `method` edge with a `.name()` label, and a class
hangs off its file node by `contains`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from graphify_php.graph_adapter import (
    EDGE_MARKER,
    EDGE_ORIGIN,
    GraphEdgeSink,
    GraphNodeIndex,
    GraphStubSink,
    SiteProofs,
    holds_build_edges,
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


def _callable(nodes, nid, label, path, line):
    nodes.append(
        {"id": nid, "label": label, "_callable": True, "source_file": path, "source_location": line}
    )


def _located(nodes, nid, line):
    next(n for n in nodes if n["id"] == nid)["source_location"] = line


def test_caller_scope_names_a_method_by_its_class():
    nodes, edges = graph(("src/Service.php", "Service", ["wrap"]))

    assert GraphNodeIndex(nodes, edges).caller_scope("src_service_service_wrap") == (
        "Service::wrap",
    )


def test_caller_scope_of_a_closure_is_the_method_it_sits_in():
    nodes, edges = graph(("src/Service.php", "Service", ["first", "wrap"]))
    _located(nodes, "src_service_service_first", "L6")
    _located(nodes, "src_service_service_wrap", "L8")
    _callable(nodes, "src_service_closure_1", "{closure#1}()", "src/Service.php", "L10")
    _callable(nodes, "src_service_closure_2", "{closure#2}()", "src/Service.php", "L10")

    index = GraphNodeIndex(nodes, edges)

    assert index.caller_scope("src_service_closure_1") == ("Service::wrap",)
    assert index.caller_scope("src_service_closure_2") == ("Service::wrap",)


def test_caller_scope_refuses_a_closure_two_methods_could_hold():
    nodes, edges = graph(("src/OneLine.php", "OneLine", ["first", "second"]))
    _located(nodes, "src_oneline_oneline_first", "L5")
    _located(nodes, "src_oneline_oneline_second", "L5")
    _callable(nodes, "src_oneline_closure_1", "{closure#1}()", "src/OneLine.php", "L5")

    assert GraphNodeIndex(nodes, edges).caller_scope("src_oneline_closure_1") == ()


def test_caller_scope_of_an_owner_less_callable_is_its_name_with_no_class():
    nodes, edges = graph(("src/Outer.php", "Outer", ["build"]))
    _located(nodes, "src_outer_outer_build", "L9")
    _callable(nodes, "src_outer_run", "run()", "src/Outer.php", "L11")
    _callable(nodes, "src_outer_closure_1", "{closure#1}()", "src/Outer.php", "L12")

    index = GraphNodeIndex(nodes, edges)

    assert index.caller_scope("src_outer_run") == ("::run",)
    assert index.caller_scope("src_outer_closure_1") == ("::run",)


def test_caller_scope_of_a_trait_method_includes_every_class_using_it():
    nodes, edges = graph(
        ("src/RolesTrait.php", "RolesTrait", ["grant"]),
        ("src/MoreRoles.php", "MoreRoles", []),
        ("src/AddCommand.php", "AddCommand", []),
    )
    for user, trait in (
        ("src_moreroles_moreroles", "src_rolestrait_rolestrait"),
        ("src_addcommand_addcommand", "src_moreroles_moreroles"),
    ):
        edges.append({"source": user, "target": trait, "relation": "mixes_in"})

    assert GraphNodeIndex(nodes, edges).caller_scope("src_rolestrait_rolestrait_grant") == (
        "RolesTrait::grant",
        "MoreRoles::grant",
        "AddCommand::grant",
    )


def test_caller_scope_of_an_unknown_node_is_empty():
    nodes, edges = graph(("src/Service.php", "Service", ["wrap"]))

    assert GraphNodeIndex(nodes, edges).caller_scope("nowhere") == ()


def test_caller_scope_of_a_method_whose_class_node_was_lost_is_its_name_alone():
    # A parse error dropped the class node: graphify keeps `testFoo()` with no `method` edge.
    nodes, edges = [], []
    _callable(nodes, "t_testfoo", "testFoo()", "tests/BrokenTest.php", "L20")
    _callable(nodes, "t_provider", "provider()", "tests/BrokenTest.php", "L10")

    assert GraphNodeIndex(nodes, edges).caller_scope("t_testfoo") == ("::testFoo",)


def test_caller_scope_refuses_an_owner_less_name_another_callable_in_the_file_shares():
    nodes, edges = graph(("src/Mixed.php", "Mixed", ["run"]))
    _callable(nodes, "src_mixed_run", "run()", "src/Mixed.php", "L30")

    assert GraphNodeIndex(nodes, edges).caller_scope("src_mixed_run") == ()


def test_caller_scope_refuses_a_closure_on_the_line_one_method_ends_and_the_next_starts():
    # `return fn () => 1; } public function second() {` -- the closure is in `first`.
    nodes, edges = graph(("src/Tight.php", "Tight", ["first", "second"]))
    _located(nodes, "src_tight_tight_first", "L5")
    _located(nodes, "src_tight_tight_second", "L8")
    _callable(nodes, "src_tight_closure_1", "{closure#1}()", "src/Tight.php", "L8")

    assert GraphNodeIndex(nodes, edges).caller_scope("src_tight_closure_1") == ()


def test_caller_scope_keeps_a_closure_on_the_first_line_of_the_only_method_above_it():
    nodes, edges = graph(("src/Tight.php", "Tight", ["first"]))
    _located(nodes, "src_tight_tight_first", "L5")
    _callable(nodes, "src_tight_closure_1", "{closure#1}()", "src/Tight.php", "L5")

    assert GraphNodeIndex(nodes, edges).caller_scope("src_tight_closure_1") == ("Tight::first",)


def test_caller_scope_refuses_a_class_whose_short_name_its_file_declares_twice():
    # Two namespaces in one file: `A\Foo` and `B\Foo` both label `Foo`.
    nodes, edges = graph(("src/Two.php", "Foo", ["run"]))
    nodes.append(
        {"id": "src_two_foo_b", "label": "Foo", "_callable": True, "_callable_class": True}
        | {"source_file": "src/Two.php"}
    )
    edges.append({"source": "src_two", "target": "src_two_foo_b", "relation": "contains"})

    assert GraphNodeIndex(nodes, edges).caller_scope("src_two_foo_run") == ()


def test_caller_owns_line_when_nothing_else_starts_between_it_and_the_line():
    nodes, edges = graph(("src/Sync.php", "Sync", ["syncUsers", "syncLeads"]))
    _located(nodes, "src_sync_sync_syncusers", "L60")
    _located(nodes, "src_sync_sync_syncleads", "L80")
    _callable(nodes, "src_sync_closure_1", "{closure#1}()", "src/Sync.php", "L73")

    index = GraphNodeIndex(nodes, edges)

    assert index.caller_owns_line("src_sync_closure_1", "L74") is True
    # The method body holds the closure that starts on L73, so L74 may be inside it.
    assert index.caller_owns_line("src_sync_sync_syncusers", "L74") is False
    assert index.caller_owns_line("src_sync_sync_syncusers", "L70") is True
    # A caller starting below the line cannot hold it.
    assert index.caller_owns_line("src_sync_sync_syncleads", "L74") is False
    assert index.caller_owns_line("nowhere", "L74") is False
    assert index.caller_owns_line("src_sync_closure_1", "") is False


def test_caller_owns_no_line_another_callable_starts_on():
    nodes, edges = graph(("src/OneLine.php", "OneLine", ["first", "second"]))
    _located(nodes, "src_oneline_oneline_first", "L5")
    _located(nodes, "src_oneline_oneline_second", "L5")

    index = GraphNodeIndex(nodes, edges)

    assert index.caller_owns_line("src_oneline_oneline_second", "L5") is False


def _registry_run(resolve, all_edges, *, scratch: bool) -> None:
    """Hand `all_edges` to `resolve` the way graphify does: through `run_language_resolvers`.

    `scratch` copies the list first, the shape of an incremental build (extract.py, the
    `resolution_context_*` branch), where removing from the list handed over does not stick.
    """
    registry = pytest.importorskip("graphify.resolver_registry")
    entry = registry.LanguageResolver(name="probe", suffixes=frozenset({".php"}), resolve=resolve)
    handed = list(all_edges) if scratch else all_edges
    registry.run_language_resolvers([Path("a.php")], [], [], handed, resolvers=[entry])


@pytest.mark.parametrize("scratch, expected", [(False, True), (True, False)])
def test_only_the_edge_list_graphify_holds_itself_is_recognised_as_its_own(scratch, expected):
    seen: list[bool] = []
    all_edges: list[dict] = []
    _registry_run(lambda pf, n, e: seen.append(holds_build_edges(e)), all_edges, scratch=scratch)

    assert seen == [expected]


def test_an_edge_list_handed_over_outside_graphify_is_not_graphifys():
    assert holds_build_edges([]) is False


def test_engine_calls_are_same_file_calls_without_a_resolver_or_a_score():
    nodes, edges = graph(("src/A.php", "A", ["run", "get"]), ("src/B.php", "B", ["get"]))
    a_run, a_get, b_get = "src_a_a_run", "src_a_a_get", "src_b_b_get"
    for node in nodes:
        node.setdefault("source_location", "L5")
    base = {
        "relation": "calls",
        "confidence": "EXTRACTED",
        "source_file": "src/A.php",
        "source_location": "L7",
    }
    same = dict(base, source=a_run, target=a_get)
    cross = dict(base, source=a_run, target=b_get)
    ours = dict(same, **{EDGE_MARKER: EDGE_ORIGIN})
    scored = dict(same, confidence_score=1.0)
    edges += [same, cross, ours, scored]

    found = GraphNodeIndex(nodes, edges).engine_calls(edges)

    assert [(c.caller_nid, c.target_nid, c.method, c.source_location) for c in found] == [
        (a_run, a_get, "get", "L7")
    ]
    assert found[0].edge is same
    assert found[0].caller_location == "L5"


def _proof(class_fqn: str, declaring_file: str) -> CallTarget:
    site = CallSite("c", "", "get", "src/A.php", "L7")
    return CallTarget(site, class_fqn, "get", Confidence.EXTRACTED, "phpstan", declaring_file)


@pytest.mark.parametrize(
    "class_fqn, declaring_file, expected",
    [
        ("App\\A", "src/A.php", True),  # the engine's own target
        ("App\\Other", "src/A.php", False),  # another class declared in the same file
        ("App\\B", "src/B.php", False),  # a body in another file is another body
        ("Vendor\\A", "vendor/v/A.php", False),
        ("class@anonymous", "src/A.php", None),  # same file, no class to compare against
        ("App\\B", "", None),  # PHPStan did not say where the body lives
    ],
)
def test_a_proof_names_the_engine_target_only_by_file_and_class(
    tmp_path, class_fqn, declaring_file, expected
):
    root = str(tmp_path)
    nodes, edges = graph(
        (f"{root}/src/A.php", "A", ["get"]),
        (f"{root}/src/A.php", "Other", ["get"]),
        (f"{root}/src/B.php", "B", ["get"]),
    )
    index = GraphNodeIndex(nodes, edges)
    target = next(n["id"] for n in nodes if n["id"].endswith("_a_get"))

    assert index.names_node(target, _proof(class_fqn, declaring_file), root) is expected


def _bound_proof(class_fqn: str, declaring_file: str, method: str = "get") -> CallTarget:
    site = CallSite("src_thing_thing_run", "", method, "src/Thing.php", "L7")
    return CallTarget(site, class_fqn, method, Confidence.EXTRACTED, "phpstan", declaring_file)


def _thing_with_a_local_cache(root: str):
    """`Thing` and a project `Cache` share one file; `App\\Store\\Cache` lives elsewhere."""
    nodes, edges = graph(
        ("src/Thing.php", "Thing", ["run"]),
        ("src/Thing.php", "Cache", ["get"]),
        ("src/Store/Cache.php", "Cache", ["get"]),
    )
    return GraphNodeIndex(nodes, edges)


def test_a_vendor_proof_never_binds_to_the_same_named_class_in_the_callers_file(tmp_path):
    root = str(tmp_path)
    index = _thing_with_a_local_cache(root)
    proof = _bound_proof("Vendor\\Lib\\Cache", f"{root}/vendor/lib/Cache.php")

    # The visible local `Cache` is what the short-name lookup alone picks.
    assert index.method_node_for("src_thing_thing_run", proof.class_fqn, "get") is not None
    assert index.proved_node_for("src_thing_thing_run", proof, root) is None


def test_a_proof_binds_the_class_its_declaring_file_holds_whatever_the_caller_sees(tmp_path):
    root = str(tmp_path)
    index = _thing_with_a_local_cache(root)
    proof = _bound_proof("App\\Store\\Cache", "src/Store/Cache.php")

    found = index.proved_node_for("src_thing_thing_run", proof, root)

    assert found == "src_store_cache_cache_get"


def test_a_proof_whose_class_does_not_declare_the_method_in_the_graph_binds_nothing(tmp_path):
    root = str(tmp_path)
    index = _thing_with_a_local_cache(root)

    proof = _bound_proof("App\\Store\\Cache", "src/Store/Cache.php", method="set")
    assert index.proved_node_for("src_thing_thing_run", proof, root) is None


def test_a_proof_whose_file_declares_the_short_name_twice_binds_nothing(tmp_path):
    root = str(tmp_path)
    nodes, edges = graph(("src/Thing.php", "Thing", ["run"]), ("src/Two.php", "Cache", ["get"]))
    nodes.append(
        {"id": "two_b_cache", "label": "Cache", "_callable_class": True}
        | {"source_file": "src/Two.php"}
    )
    edges.append({"source": "src_two", "target": "two_b_cache", "relation": "contains"})
    index = GraphNodeIndex(nodes, edges)

    proof = _bound_proof("B\\Cache", "src/Two.php")
    assert index.proved_node_for("src_thing_thing_run", proof, root) is None


def test_a_proof_with_no_declaring_file_falls_back_to_the_visibility_lookup(tmp_path):
    root = str(tmp_path)
    index = _thing_with_a_local_cache(root)
    proof = _bound_proof("App\\Cache", "")

    assert index.proved_node_for("src_thing_thing_run", proof, root) == index.method_node_for(
        "src_thing_thing_run", "App\\Cache", "get"
    )


def test_callable_at_finds_the_one_callable_starting_on_a_line_of_that_file(tmp_path):
    root = str(tmp_path)
    nodes, edges = graph((f"{root}/src/Sync.php", "Sync", ["syncUsers"]))
    method = next(n["id"] for n in nodes if n["id"].endswith("_syncusers"))
    _located(nodes, method, "L60")
    _callable(nodes, "closure_1", "{closure#1}()", f"{root}/src/Sync.php", "L73")
    _callable(nodes, "closure_2", "{closure#2}()", f"{root}/src/Sync.php", "L80")
    _callable(nodes, "closure_3", "{closure#3}()", f"{root}/src/Sync.php", "L80")
    _callable(nodes, "other_file", "{closure#1}()", f"{root}/src/Other.php", "L90")
    index = GraphNodeIndex(nodes, edges)

    assert index.callable_at("src/Sync.php", (73,), root) == "closure_1"
    assert index.callable_at(f"{root}/src/Sync.php", (59, 60), root) == method
    # Two callables on the line: the line cannot say which one holds the call.
    assert index.callable_at("src/Sync.php", (80,), root) is None
    assert index.callable_at("src/Sync.php", (60, 73), root) is None
    assert index.callable_at("src/Sync.php", (90,), root) is None
    assert index.callable_at("src/Sync.php", (), root) is None
    # A class starts on its own line and is never a caller.
    assert index.callable_at("src/Sync.php", (1,), root) is None


def test_a_proof_of_a_class_no_parsed_file_declares_never_binds(tmp_path):
    # `use Firebase\JWT\JWT as FirebaseJWT; class Jwt { ... FirebaseJWT::decode() }`: the
    # short names fold to one key, and the caller's own file holds the project `Jwt`.
    root = str(tmp_path)
    index = _thing_with_a_local_cache(root)
    proof = dataclasses.replace(_bound_proof("Vendor\\Lib\\Cache", ""), outside_corpus=True)

    assert index.method_node_for("src_thing_thing_run", proof.class_fqn, "get") is not None
    assert index.proved_node_for("src_thing_thing_run", proof, root) is None


# --- the proofs core_guard lists withheld core edges against ---------------------------------


def _ours(source, target, line="L20", **extra) -> dict:
    return {
        "source": source,
        "target": target,
        "relation": "calls",
        "source_location": line,
        EDGE_MARKER: EDGE_ORIGIN,
        **extra,
    }


def test_a_withheld_edge_is_proved_by_our_edge_at_the_same_caller_line_and_method_name():
    nodes = [
        {"id": "emails_get", "label": ".get()"},
        {"id": "cache_get", "label": ".GET()"},
        {"id": "cache_put", "label": ".put()"},
    ]
    ours = [_ours("caller", "cache_get"), _ours("caller", "cache_put", "L21")]
    proofs = SiteProofs(nodes, ours)
    core = {"source": "caller", "target": "emails_get", "source_location": "L20"}

    assert proofs.proof_for(core) == ("caller", "emails_get", "calls", "cache_get")
    assert proofs.proof_for({**core, "relation": "uses"})[2] == "uses"
    assert proofs.proof_for({**core, "source_location": "L21"}) is None
    assert proofs.proof_for({**core, "target": ""}) is None


def test_a_stub_proof_is_named_by_its_external_target_and_the_smallest_target_wins():
    nodes = [{"id": "emails_get", "label": ".get()"}]
    ours = [
        _ours("caller", "php_external_z", _external_target="Vendor\\Z::get"),
        _ours("caller", "php_external_a", _external_target="Vendor\\A::get"),
        {**_ours("caller", "php_external_b", _external_target="B::get"), EDGE_MARKER: "x"},
    ]
    core = {"source": "caller", "target": "emails_get", "source_location": "L20"}

    assert SiteProofs(nodes, ours).proof_for(core)[3] == "php_external_a"
