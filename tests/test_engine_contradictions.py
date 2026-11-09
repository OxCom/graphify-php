"""graphify's engine binds `$x->m()` by bare name inside one file; a type proof can overrule it.

The real index and sink over a synthetic graph, with a fake source standing in for PHPStan's
records and the parser's site count: what is under test is the decision and the accounting,
not the record matching (tests/test_phpstan_source.py) or the parse (test_tree_sitter_source).
"""

from __future__ import annotations

import json

import pytest

from graphify_php import resolver, status
from graphify_php.graph_adapter import EDGE_MARKER, GraphEdgeSink, GraphNodeIndex
from graphify_php.ports import CallerSites, CallTarget, Confidence, LineProof, Resolution

F = "/repo/src/Traps.php"
VENDOR = "/repo/vendor/lib/Cache.php"


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "graphify-out" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


def graph(*, anonymous: bool = False):
    """`Cache`, `Basket` and `Checkout` in one file; `Checkout::typedGet` starts on line 42.

    The engine edge is `typedGet -> Basket::get` at L44, the shape graphify writes when the
    file-wide label map's last `get` wins.
    """
    nodes = [{"id": "file", "label": "Traps.php", "source_file": F}]
    edges = []
    for cls in ("Cache", "Basket", "Checkout"):
        cid = cls.lower()
        nodes.append({"id": cid, "label": cls, "_callable_class": True, "source_file": F})
        edges.append({"source": "file", "target": cid, "relation": "contains"})
    methods = [("cache", "get", 11), ("basket", "get", 23), ("checkout", "typedGet", 42)]
    for owner, name, line in methods:
        nid = f"{owner}_{name.lower()}"
        nodes.append(
            {
                "id": nid,
                "label": f".{name}()",
                "_callable": True,
                "source_file": F,
                "source_location": f"L{line}",
            }
        )
        edges.append({"source": owner, "target": nid, "relation": "method"})
    if anonymous:
        # An anonymous class's method: no class node, no `method` edge, no leading dot.
        nodes.append(
            {
                "id": "anon_getdata",
                "label": "getData()",
                "_callable": True,
                "source_file": F,
                "source_location": "L60",
            }
        )
    return nodes, edges


def engine_edge(target: str = "basket_get", line: int = 44, **extra) -> dict:
    edge = {
        "source": "checkout_typedget",
        "target": target,
        "relation": "calls",
        "context": "call",
        "confidence": "EXTRACTED",
        "source_file": F,
        "source_location": f"L{line}",
        "weight": 1.0,
    }
    edge.update(extra)
    return edge


class Prover:
    """Stands in for PHPStan's per-line records and the parser's per-caller site count."""

    name = "prover"

    def __init__(
        self, records=None, lines=None, calls=None, *, foreign=(), other_kinds=False, declared=None
    ):
        # (line, method) -> [(class_fqn, method, declaring_file, is_interface)]
        self.records = dict(records or {})
        # method -> {line: count} for every `->m(` in the caller, or None for "cannot tell".
        self.lines = lines
        self.calls = dict(calls or {})  # (line, method) -> distinct calls the records cover
        self.foreign = frozenset(foreign)  # lines where another body's call of it sits
        self.other_kinds = other_kinds  # a function or scoped call of that name in the caller
        self.declared = declared  # what a parser reads declared at a dot-less target
        self.asked: list = []

    def available(self, repo_root):
        return True, ""

    def resolve(self, repo_root, sites):
        return Resolution(targets=[], unresolved={})

    def line_proofs(self, repo_root, sites):
        self.asked.extend(sites)
        out = {}
        for site in sites:
            line = int(site.source_location.lstrip("L"))
            rows = self.records.get((line, site.method), [])
            targets = tuple(
                CallTarget(
                    site=site,
                    class_fqn=cls,
                    method=method,
                    confidence=Confidence.EXTRACTED,
                    via="phpstan:interface" if interface else "phpstan",
                    declaring_file=declaring,
                    interface_target=interface,
                )
                for cls, method, declaring, interface in rows
            )
            calls = self.calls.get((line, site.method), 1 if targets else 0)
            out[site] = LineProof(targets=targets, calls=calls)
        return out

    def member_call_lines(self, repo_root, source_file, caller_location, method):
        if self.lines is None:
            return None
        return CallerSites(dict(self.lines.get(method, {})), self.foreign, self.other_kinds)

    def method_declared_at(self, repo_root, source_file, location, name):
        return self.declared


CACHE_PROOF = ("Eval\\Traps\\Cache", "get", F, False)


def run(edges_extra=(), prover=None, *, owned=True, anonymous=False):
    nodes, edges = graph(anonymous=anonymous)
    edges = edges + list(edges_extra)
    record = resolver.run(
        [{"nodes": [], "edges": [], "raw_calls": []}],
        nodes,
        edges,
        sources=[prover or Prover()],
        repo_root="/repo",
        edges_owned=owned,
    )
    return record, edges


def calls(edges):
    return {(e["source"], e["target"]) for e in edges if e.get("relation") == "calls"}


def test_an_engine_edge_a_proof_contradicts_is_dropped_and_the_proved_edge_added():
    prover = Prover(records={(44, "get"): [CACHE_PROOF]}, lines={"get": {44: 1}})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "cache_get")}
    added = next(e for e in edges if e.get("relation") == "calls")
    assert added[EDGE_MARKER] == "graphify-php"
    assert added["confidence"] == "EXTRACTED"
    assert added["_via"] == "phpstan"
    assert added["source_location"] == "L44"
    assert (record.engine_sites_total, record.engine_contradicted) == (1, 1)
    assert (record.engine_dropped, record.engine_proved_added) == (1, 1)
    assert record.engine_kept == {}
    assert record.engine_dropped_edges == [
        {
            "source": "checkout_typedget",
            "target": "basket_get",
            "relation": "calls",
            "reason": resolver.REASON_ENGINE_DROPPED,
            "proof": {"source": "checkout_typedget", "target": "cache_get"},
        }
    ]


def test_an_engine_edge_the_proof_agrees_with_is_kept():
    agree = ("Eval\\Traps\\Basket", "get", F, False)
    record, edges = run([engine_edge()], Prover({(44, "get"): [agree]}, {"get": {44: 1}}))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_dropped == 0
    assert record.engine_contradicted == 0
    assert record.engine_kept == {resolver.REASON_ENGINE_AGREES: 1}


def test_an_engine_edge_with_no_record_is_kept():
    record, edges = run([engine_edge()], Prover(lines={"get": {44: 1}}))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_NO_RECORD: 1}


def test_records_that_disagree_with_each_other_keep_the_edge():
    agree = ("Eval\\Traps\\Basket", "get", F, False)
    prover = Prover({(44, "get"): [CACHE_PROOF, agree]}, {"get": {44: 1}})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_RECORDS_DISAGREE: 1}


def test_a_second_unproved_site_of_the_method_in_the_caller_keeps_the_edge():
    # The engine kept the first site's line; `$this->store->get()` on 46 has no record.
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1, 46: 1}})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_contradicted == 1
    assert record.engine_dropped == 0
    assert record.engine_kept == {resolver.REASON_ENGINE_OTHER_SITE: 1}


def test_two_calls_on_the_line_and_one_record_keep_the_edge():
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 2}})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_OTHER_SITE: 1}


def test_every_site_contradicted_drops_the_edge_and_edges_each_proof():
    vendor = ("Vendor\\Lib\\Cache", "get", VENDOR, False)
    prover = Prover({(44, "get"): [CACHE_PROOF], (46, "get"): [vendor]}, {"get": {44: 1, 46: 1}})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "cache_get")}
    assert record.engine_dropped == 1
    # The vendor proof has no node and stubs are off: counted under the pass's own prefix.
    assert record.unresolved == {f"engine:{resolver.REASON_NOT_IN_GRAPH}": 1}


def test_a_vendor_proof_landing_on_a_stub_names_the_stub_as_the_drops_proof(monkeypatch):
    # The shrink guard credits a dropped edge only through a proof edge in the new graph; a
    # vendor proof's edge goes to a stub, so the row must name that stub, not None.
    monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, "1")
    vendor = ("Vendor\\Lib\\Cache", "get", VENDOR, False)
    prover = Prover({(44, "get"): [vendor]}, {"get": {44: 1}})
    record, edges = run([engine_edge()], prover)

    assert record.engine_dropped == 1
    proof = record.engine_dropped_edges[0]["proof"]
    assert proof["target"] is not None
    assert ("checkout_typedget", proof["target"]) in calls(edges)


def test_the_parser_unable_to_list_the_callers_sites_keeps_the_edge():
    record, edges = run([engine_edge()], Prover({(44, "get"): [CACHE_PROOF]}, lines=None))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_SITES_UNKNOWN: 1}


def test_an_engine_line_the_parser_does_not_see_keeps_the_edge():
    record, edges = run([engine_edge()], Prover({(44, "get"): [CACHE_PROOF]}, {"get": {45: 1}}))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_SITE_NOT_FOUND: 1}


def test_a_proof_in_the_same_file_naming_no_known_class_keeps_the_edge():
    # `class@anonymous` in this file: nothing says it is not the engine's target.
    anon = ("class@anonymous", "get", F, False)
    record, edges = run([engine_edge()], Prover({(44, "get"): [anon]}, {"get": {44: 1}}))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_UNPLACED: 1}


def test_a_proof_without_a_declaring_file_keeps_the_edge():
    unplaced = ("Vendor\\Lib\\Cache", "get", "", False)
    record, edges = run([engine_edge()], Prover({(44, "get"): [unplaced]}, {"get": {44: 1}}))

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_UNPLACED: 1}


def test_an_interface_proof_wins_and_is_counted_apart():
    contract = ("App\\CacheInterface", "get", "/repo/src/CacheInterface.php", True)
    record, edges = run([engine_edge()], Prover({(44, "get"): [contract]}, {"get": {44: 1}}))

    assert calls(edges) == set()
    assert record.engine_dropped == 1
    assert record.engine_dropped_interface == 1
    assert record.engine_dropped_edges[0]["proof"] == {
        "source": "checkout_typedget",
        "target": None,
    }


def test_an_anonymous_class_method_target_is_recognised_without_a_dotted_label():
    edge = engine_edge(target="anon_getdata")
    vendor = ("Vendor\\Lib\\Cache", "getData", VENDOR, False)
    prover = Prover({(44, "getData"): [vendor]}, {"getData": {44: 1}}, declared=True)
    record, edges = run([edge], prover, anonymous=True)

    assert record.engine_sites_total == 1
    assert record.engine_dropped == 1
    assert calls(edges) == set()
    assert [s.method for s in prover.asked] == ["getData"]


def test_an_edge_list_the_resolver_cannot_prove_is_graphifys_is_left_whole():
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1}})
    record, edges = run([engine_edge()], prover, owned=False)

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_contradicted == 1
    assert record.engine_dropped == 0
    assert record.engine_proved_added == 0
    assert record.engine_kept == {resolver.REASON_ENGINE_NOT_OWNED: 1}


def test_edges_that_are_not_engine_same_file_calls_are_not_considered():
    ours = engine_edge(**{EDGE_MARKER: "graphify-php"})
    scored = engine_edge(target="cache_get", confidence_score=1.0)
    other_file = engine_edge(source_file="/repo/src/Other.php")
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1}})
    record, edges = run([ours, scored, other_file], prover)

    assert record.engine_sites_total == 0
    assert prover.asked == []
    assert len([e for e in edges if e.get("relation") == "calls"]) == 3


def test_no_source_offering_proofs_keeps_every_engine_edge():
    class Plain:
        name = "plain"

        def available(self, repo_root):
            return True, ""

        def resolve(self, repo_root, sites):
            return Resolution(targets=[], unresolved={})

    record, edges = run([engine_edge()], Plain())

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_NO_PROOFS: 1}


def test_the_status_file_carries_the_engine_counters_and_the_dropped_edges(status_file):
    run([engine_edge()], Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1}}))

    row = json.loads(status_file.read_text())["resolvers"][0]
    assert row["engine_sites_total"] == 1
    assert row["engine_dropped"] == 1
    assert row["engine_dropped_edges"][0]["target"] == "basket_get"


def test_sink_removal_takes_edges_out_of_the_same_list_object():
    nodes, edges = graph()
    wrong = engine_edge()
    edges.append(wrong)
    held = edges
    sink = GraphEdgeSink(edges, GraphNodeIndex(nodes, edges))

    assert sink.remove_edges([wrong]) == 1
    assert held is edges
    assert wrong not in edges


@pytest.mark.parametrize(
    ("declared", "reason"),
    [
        (None, resolver.REASON_ENGINE_TARGET_UNKNOWN),
        (False, resolver.REASON_ENGINE_TARGET_NOT_METHOD),
    ],
)
def test_a_dot_less_target_no_parser_reads_as_a_method_is_left_alone(declared, reason):
    # A free function and an anonymous class's method share the graph schema.
    edge = engine_edge(target="anon_getdata")
    vendor = ("Vendor\\Lib\\Cache", "getData", VENDOR, False)
    prover = Prover({(44, "getData"): [vendor]}, {"getData": {44: 1}}, declared=declared)
    record, edges = run([edge], prover, anonymous=True)

    assert calls(edges) == {("checkout_typedget", "anon_getdata")}
    assert record.engine_kept == {reason: 1}
    assert prover.asked == []


def test_a_function_or_scoped_call_of_the_name_in_the_caller_keeps_the_edge():
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1}}, other_kinds=True)
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == {("checkout_typedget", "basket_get")}
    assert record.engine_kept == {resolver.REASON_ENGINE_OTHER_CALL_KIND: 1}


def test_a_proof_on_a_line_another_body_shares_is_dropped_but_not_edged():
    prover = Prover({(44, "get"): [CACHE_PROOF]}, {"get": {44: 1}}, foreign={44})
    record, edges = run([engine_edge()], prover)

    assert calls(edges) == set()
    assert record.engine_dropped == 1
    assert record.engine_proved_added == 0
    assert record.unresolved == {f"engine:{resolver.REASON_ENGINE_PROOF_SHARED_LINE}": 1}
    assert record.engine_dropped_edges[0]["proof"]["target"] is None
