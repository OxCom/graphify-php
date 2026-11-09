"""Shrink attribution: loss the build itself recorded in its sidecar, matched by exact key and
re-checked against the new graph, plus the external-stub rules. Anything the sidecar cannot prove
leaves the verdict exactly as it is without one.
"""

from __future__ import annotations

import json

import pytest

from graphify_php import shrink
from graphify_php.shrink import (
    REASON_CORE_SUPPRESSED,
    REASON_ENGINE_CONTRADICTS,
    REASON_STUBS_DISABLED,
    classify_loss,
)

OURS = "graphify-php"


def node(nid, file=None, **extra):
    out = {"id": nid, "label": nid}
    if file is not None:
        out["source_file"] = file
    out.update(extra)
    return out


def stub(nid):
    """The shape GraphStubSink mints for an external declaring class."""
    return node(nid, "", external=True, type="external", _resolver=OURS)


def edge(source, target, relation="calls", file=None, resolver=None):
    out = {"source": source, "target": target, "relation": relation}
    if file is not None:
        out["source_file"] = file
    if resolver is not None:
        out["_resolver"] = resolver
    return out


def graph(nodes, links):
    return {"nodes": nodes, "links": links}


def manifest(**hashes):
    return {name.replace("__", "."): {"ast_hash": h} for name, h in hashes.items()}


def chain(prefix, file, count):
    nodes = [node(f"{prefix}{i}", file) for i in range(count)]
    links = [edge(f"{prefix}{i}", f"{prefix}{i + 1}", "contains", file) for i in range(count - 1)]
    return nodes, links


def sidecar(removed=(), counts=None, stubs="on"):
    removed = list(removed)
    if counts is None:
        counts = {}
        for entry in removed:
            counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
    return {
        "schema": 1,
        "producer": "graphify-php",
        "external_stubs": stubs,
        "removed_edges": removed,
        "counts": counts,
    }


def removal(lost, proof_target, reason=REASON_CORE_SUPPRESSED):
    return {
        "source": lost["source"],
        "target": lost["target"],
        "relation": lost["relation"],
        "reason": reason,
        "proof": {"source": lost["source"], "target": proof_target},
    }


@pytest.fixture
def root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.php", "b.php"):
        (repo / name).write_text("")
    return repo


M = manifest(a__php="1", b__php="2")


def suppressed(count=3, total=10):
    """b.php: ten nodes. Core had `count` name-guessed calls b{i} -> b9; the new build drops them
    and graphify-php's proof b{i} -> b{i+1} stands in the new graph."""
    b_nodes, b_links = chain("b", "b.php", total)
    core = [edge(f"b{i}", "b9", file="b.php") for i in range(count)]
    proofs = [edge(f"b{i}", f"b{i + 1}", file="b.php", resolver=OURS) for i in range(count)]
    old = graph(b_nodes, b_links + core + proofs)
    new = graph(b_nodes, b_links + proofs)
    entries = [removal(core[i], f"b{i + 1}") for i in range(count)]
    return old, new, core, entries


def assert_invariant(report):
    e = report.edges
    assert (
        e["lost"] == e["explained"] + e["unexplained"] + e["resolver_lost"] + e["attributed_total"]
    )
    assert e["attributed_total"] == sum(e["attributed"].values())
    n = report.nodes
    assert n["lost"] == n["orphan"] + n["explained"] + n["unexplained"] + n["attributed_total"]
    assert n["attributed_total"] == sum(n["attributed"].values())


def test_lost_core_edge_with_listed_key_and_proof_is_attributed(root):
    old, new, _, entries = suppressed()

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(entries))

    assert report.verdict == "accept", report.reasons
    assert report.edges["attributed"] == {REASON_CORE_SUPPRESSED: 3}
    assert report.edges["attributed_total"] == 3 and report.edges["unexplained"] == 0
    assert_invariant(report)


def test_without_attribution_the_same_loss_is_unexplained(root):
    old, new, _, _ = suppressed()

    report = classify_loss(old, new, M, M, root, tolerance=0.0)

    assert report.verdict == "refuse"
    assert report.edges["unexplained"] == 3
    assert report.edges["attributed"] == {} and report.edges["attributed_total"] == 0
    assert_invariant(report)


def test_proof_edge_missing_from_the_new_graph_leaves_the_loss_unexplained(root):
    old, new, _, entries = suppressed()
    new["links"] = [e for e in new["links"] if not (e.get("_resolver") and e["source"] == "b0")]

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(entries))

    assert report.edges["attributed"] == {REASON_CORE_SUPPRESSED: 2}
    assert report.edges["unexplained"] == 1 and report.verdict == "refuse"
    assert any("no proof in the new graph" in r for r in report.reasons)
    assert_invariant(report)


def test_proof_from_another_caller_or_without_resolver_marker_proves_nothing(root):
    old, new, _, entries = suppressed(count=2)
    entries[0]["proof"]["source"] = "b5"  # an edge b5 -> b1 does not prove a site in b0
    new["links"].append(edge("b5", "b1", file="b.php", resolver=OURS))
    for e in new["links"]:
        if e["source"] == "b1" and e.get("_resolver"):
            del e["_resolver"]  # a core edge is no proof

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(entries))

    assert report.edges["attributed_total"] == 0 and report.edges["unexplained"] == 2


def test_attribution_is_capped_at_the_recorded_count(root):
    old, new, _, entries = suppressed(count=3)

    capped = sidecar(entries, counts={REASON_CORE_SUPPRESSED: 2})
    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=capped)

    assert report.edges["attributed"] == {REASON_CORE_SUPPRESSED: 2}
    assert report.edges["unexplained"] == 1 and report.verdict == "refuse"
    assert any("capped" in r for r in report.reasons)
    assert_invariant(report)


def test_reasons_are_capped_separately(root):
    old, new, _, entries = suppressed(count=3)
    entries[2]["reason"] = REASON_ENGINE_CONTRADICTS

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(entries))

    assert report.edges["attributed"] == {REASON_CORE_SUPPRESSED: 2, REASON_ENGINE_CONTRADICTS: 1}


def test_resolver_edge_listed_in_the_sidecar_is_never_attributed(root):
    a_nodes, a_links = chain("a", "a.php", 3)
    b_nodes, b_links = chain("b", "b.php", 10)
    ours = edge("a0", "b0", file="a.php", resolver=OURS)
    proof = edge("a0", "b1", file="a.php", resolver=OURS)
    old = graph(a_nodes + b_nodes, a_links + b_links + [ours, proof])
    new = graph(a_nodes + b_nodes, a_links + b_links + [proof])

    report = classify_loss(
        old, new, M, M, root, tolerance=0.0, attribution=sidecar([removal(ours, "b1")])
    )

    assert report.edges["attributed_total"] == 0 and report.edges["resolver_lost"] == 1
    assert report.verdict == "refuse"
    assert any("graphify-php edges" in r for r in report.reasons)


def test_stale_sidecar_explains_nothing(root):
    old, new, _, entries = suppressed()
    # An earlier build's record: its proofs name edges this build never wrote.
    for i, entry in enumerate(entries):
        entry["proof"]["target"] = f"gone{i}"

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(entries))
    without = classify_loss(old, new, M, M, root, tolerance=0.0)

    assert report.edges["attributed_total"] == 0
    assert report.verdict == without.verdict == "refuse"
    assert report.edges["unexplained"] == without.edges["unexplained"] == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema=2),
        lambda s: s.update(producer="someone-else"),
        lambda s: s.update(external_stubs="maybe"),
        lambda s: s.update(removed_edges={"not": "a list"}),
        lambda s: s["removed_edges"][0].update(reason="made-up"),
        lambda s: s["removed_edges"][0].update(proof=None),
        lambda s: s["removed_edges"][0].update(source=7),
        lambda s: s["removed_edges"][0]["proof"].pop("target"),
        lambda s: s.update(counts={REASON_CORE_SUPPRESSED: -1}),
        lambda s: s.update(counts={REASON_CORE_SUPPRESSED: True}),
        lambda s: s.update(counts=[]),
        lambda s: s.pop("counts"),
    ],
)
def test_malformed_sidecar_gives_the_verdict_without_one(root, mutate):
    old, new, _, entries = suppressed()
    bad = sidecar(entries)
    mutate(bad)

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=bad)
    without = classify_loss(old, new, M, M, root, tolerance=0.0)

    assert report.verdict == without.verdict
    assert report.edges == without.edges and report.nodes == without.nodes
    assert any(r.startswith("attribution ignored:") for r in report.reasons)


def test_non_object_sidecar_is_malformed(root):
    old, new, _, _ = suppressed()

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=["x"])

    assert report.edges["attributed_total"] == 0
    assert any(r.startswith("attribution ignored:") for r in report.reasons)


def test_attribution_notes_do_not_turn_an_accept_into_a_refusal(root):
    b_nodes, b_links = chain("b", "b.php", 10)
    same = graph(b_nodes, b_links)

    report = classify_loss(same, same, M, M, root, attribution={"schema": 99})

    assert report.verdict == "accept"
    assert report.reasons and report.reasons[0].startswith("attribution ignored:")


# --- external stubs ----------------------------------------------------------------------------


def with_stub():
    """a.php calls into vendor class X through a stub; b.php is unrelated ballast."""
    a_nodes, a_links = chain("a", "a.php", 3)
    b_nodes, b_links = chain("b", "b.php", 20)
    x = stub("php_external_vendor_x")
    call = edge("a0", x["id"], file="a.php", resolver=OURS)
    old = graph(a_nodes + b_nodes + [x], a_links + b_links + [call])
    new = graph(a_nodes + b_nodes, a_links + b_links)
    return old, new


def test_stub_node_lost_with_its_callers_file_changed_is_explained(root):
    old, new = with_stub()

    report = classify_loss(old, new, M, manifest(a__php="X", b__php="2"), root, tolerance=0.0)

    assert report.verdict == "accept", report.reasons
    assert report.nodes["explained"] == 1 and report.nodes["unexplained"] == 0
    assert report.edges["explained"] == 1
    assert "<none>" not in {f["file"] for f in report.files}
    assert_invariant(report)


def test_stub_edge_from_an_unchanged_caller_to_a_vanished_stub_is_resolver_lost(root):
    old, new = with_stub()

    report = classify_loss(old, new, M, M, root, tolerance=0.0)

    assert report.edges["resolver_lost"] == 1 and report.edges["unexplained"] == 0
    assert report.nodes["unexplained"] == 1
    assert report.verdict == "refuse"
    assert_invariant(report)


def test_stubs_switched_off_are_attributed(root):
    old, new = with_stub()

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(stubs="off"))

    assert report.verdict == "accept", report.reasons
    assert report.nodes["attributed"] == {REASON_STUBS_DISABLED: 1}
    assert report.edges["attributed"] == {REASON_STUBS_DISABLED: 1}
    assert report.edges["resolver_lost"] == 0
    assert_invariant(report)


def test_stubs_off_with_a_stub_left_in_the_new_graph_attributes_nothing(root):
    old, new = with_stub()
    y = stub("php_external_vendor_y")
    old["nodes"].append(y)
    old["links"].append(edge("a1", y["id"], file="a.php", resolver=OURS))
    new["nodes"].append(y)
    new["links"].append(edge("a1", y["id"], file="a.php", resolver=OURS))

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(stubs="off"))

    assert report.nodes["attributed_total"] == 0 and report.edges["attributed_total"] == 0
    assert report.edges["resolver_lost"] == 1 and report.verdict == "refuse"
    assert any("stub nodes" in r for r in report.reasons)


def test_stubs_on_does_not_attribute_lost_stubs(root):
    old, new = with_stub()

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(stubs="on"))

    assert report.nodes["attributed_total"] == 0 and report.verdict == "refuse"


def test_config_nodes_marked_by_the_resolver_are_not_stubs(root):
    # `_resolver` alone marks php_symfony_config's nodes too; only `external` makes a stub.
    b_nodes, b_links = chain("b", "b.php", 10)
    cfg = node("cfg::file::x.yaml", "", _resolver=OURS)
    old = graph(b_nodes + [cfg], b_links + [edge("b0", cfg["id"], "references")])
    new = graph(b_nodes, b_links)

    report = classify_loss(old, new, M, M, root, tolerance=0.0, attribution=sidecar(stubs="off"))

    assert report.nodes["attributed_total"] == 0
    assert {f["file"] for f in report.files} == {"<none>"}


# --- reading the sidecar -----------------------------------------------------------------------


def test_attribution_path_sits_next_to_the_status_record(tmp_path):
    status = tmp_path / "out" / ".graphify-php-status.json"
    assert shrink.attribution_path(status) == tmp_path / "out" / ".graphify-php-attribution.json"


def test_read_attribution_missing_file_gives_a_reason(tmp_path):
    data, note = shrink.read_attribution(tmp_path / "nope.json")
    assert data is None and "missing" in note


def test_read_attribution_unreadable_file_gives_a_reason(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    data, note = shrink.read_attribution(path)
    assert data is None and "unreadable" in note


def test_read_attribution_returns_the_document(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(sidecar()))
    data, note = shrink.read_attribution(path)
    assert data == sidecar() and note is None


def test_counted_policies_carry_the_attribution_keys(root):
    b_nodes, b_links = chain("b", "b.php", 3)
    g = graph(b_nodes, b_links)
    report = classify_loss(g, g, M, M, root, policy="count")
    assert report.edges["attributed"] == {} and report.edges["attributed_total"] == 0
    assert report.nodes["attributed"] == {} and report.nodes["attributed_total"] == 0


def test_schema_constants_are_exported_for_the_writer():
    assert shrink.ATTRIBUTION_SCHEMA == 1
    assert shrink.ATTRIBUTION_FILENAME == ".graphify-php-attribution.json"
    assert set(shrink.REMOVAL_REASONS) == {REASON_CORE_SUPPRESSED, REASON_ENGINE_CONTRADICTS}
