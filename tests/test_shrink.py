"""The attributed-loss shrink guard: every class a lost node can fall into, the edge rules, the
legacy policies, and two real builds whose loss was investigated by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify_php.shrink import LossReport, classify_loss

FIXTURES = Path(__file__).parent / "fixtures" / "shrink"


def node(nid, file=None, external=False):
    out = {"id": nid, "label": nid}
    if file is not None:
        out["source_file"] = file
    if external:
        out["external"] = True
    return out


def edge(source, target, relation="calls", file=None, resolver=None):
    out = {"source": source, "target": target, "relation": relation}
    if file is not None:
        out["source_file"] = file
    if resolver is not None:
        out["_resolver"] = resolver
    return out


def graph(nodes, links, key="links"):
    return {"nodes": nodes, key: links}


def manifest(**hashes):
    return {
        name.replace("__", "."): {"mtime": 0, "ast_hash": h, "semantic_hash": h}
        for name, h in hashes.items()
    }


def chain(prefix, file, count):
    """`count` nodes in `file`, linked in a line, so every one has degree >= 1."""
    nodes = [node(f"{prefix}{i}", file) for i in range(count)]
    links = [edge(f"{prefix}{i}", f"{prefix}{i + 1}", file=file) for i in range(count - 1)]
    return nodes, links


@pytest.fixture
def root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.py", "b.py"):
        (repo / name).write_text("")
    return repo


def baseline():
    """b.py: ten connected nodes that never change. a.py: three connected nodes under test."""
    b_nodes, b_links = chain("b", "b.py", 10)
    a_nodes, a_links = chain("a", "a.py", 3)
    return a_nodes, a_links, b_nodes, b_links


def files_by_name(report: LossReport) -> dict[str, dict]:
    return {f["file"]: f for f in report.files}


def test_deleted_file_explains_its_nodes_and_edges(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    (root / "a.py").unlink()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, manifest(b__py="2"), root)

    assert report.verdict == "accept", report.reasons
    assert files_by_name(report)["a.py"]["class"] == "deleted"
    assert report.nodes["explained"] == 3 and report.nodes["unexplained"] == 0
    assert report.edges["explained"] == 2 and report.edges["unexplained"] == 0


def test_changed_file_explains_partial_loss(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(a_nodes[:1] + b_nodes, b_links)

    report = classify_loss(
        old, new, manifest(a__py="1", b__py="2"), manifest(a__py="X", b__py="2"), root
    )

    assert report.verdict == "accept", report.reasons
    assert files_by_name(report)["a.py"] == {
        "file": "a.py",
        "class": "changed",
        "old": 3,
        "new": 1,
        "lost": 2,
    }


def test_file_new_to_the_manifest_counts_as_changed(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(a_nodes[:1] + b_nodes, b_links)

    report = classify_loss(old, new, manifest(b__py="2"), manifest(a__py="1", b__py="2"), root)

    assert files_by_name(report)["a.py"]["class"] == "changed"
    assert report.verdict == "accept", report.reasons


def test_changed_file_still_on_disk_losing_every_node_refuses(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)

    report = classify_loss(
        old, new, manifest(a__py="1", b__py="2"), manifest(a__py="X", b__py="2"), root
    )

    assert report.verdict == "refuse"
    assert any("changed file a.py" in r for r in report.reasons)


def test_unchanged_file_with_renamed_ids_is_not_loss(root):
    # graphify renames ids between versions (`types_lodash` -> `ref_types_lodash`).
    a_nodes, a_links, b_nodes, b_links = baseline()
    renamed = [node(f"ref_{n['id']}", "a.py") for n in a_nodes]
    renamed_links = [edge(f"ref_{e['source']}", f"ref_{e['target']}", file="a.py") for e in a_links]
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(renamed + b_nodes, renamed_links + b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.verdict == "accept", report.reasons
    assert files_by_name(report)["a.py"] == {
        "file": "a.py",
        "class": "unchanged",
        "old": 3,
        "new": 3,
        "lost": 0,
    }
    assert report.nodes["explained"] == 3 and report.edges["unexplained"] == 0


def test_unchanged_file_count_drop_is_unexplained(root):
    b_nodes, b_links = chain("b", "b.py", 10)
    old = graph(b_nodes, b_links)
    new = graph(b_nodes[:8], b_links[:7])
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    # 2 of 10 connected nodes lost; floor = ceil(10 * 0.9) = 9.
    assert report.verdict == "refuse"
    assert report.nodes == {
        "old": 10,
        "new": 8,
        "lost": 2,
        "orphan": 0,
        "explained": 0,
        "unexplained": 2,
        "base": 10,
        "floor": 9,
    }
    assert "unexplained node loss 2" in report.reasons[0]


def test_unchanged_file_emptied_refuses_even_within_tolerance(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    big_nodes, big_links = chain("c", "b.py", 40)
    old = graph(a_nodes[:1] + big_nodes, big_links)
    new = graph(big_nodes, big_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.nodes["unexplained"] == 1
    assert report.verdict == "refuse"
    assert report.reasons == ["unchanged file a.py lost all 1 of its nodes"]


def test_unchanged_file_dropped_by_a_new_extractor_is_explained(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root, producer_changed=True)

    assert report.verdict == "accept", report.reasons
    assert files_by_name(report)["a.py"]["class"] == "dropped_by_extractor"
    assert report.nodes["unexplained"] == 0 and report.nodes["explained"] == 3
    assert report.edges["unexplained"] == 0 and report.edges["explained"] == 2


def test_unchanged_file_emptied_without_a_producer_change_still_refuses(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.verdict == "refuse"
    assert files_by_name(report)["a.py"]["class"] == "unchanged"
    assert "unchanged file a.py lost all 3 of its nodes" in report.reasons


def test_dropped_file_with_a_cross_file_edge_refuses(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links + [edge("a0", "b0", file="a.py")])
    new = graph(b_nodes, b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root, producer_changed=True)

    assert report.verdict == "refuse"
    assert files_by_name(report)["a.py"]["class"] == "unchanged"
    assert report.nodes["unexplained"] == 3
    assert "unchanged file a.py lost all 3 of its nodes" in report.reasons


def test_unchanged_file_partial_drop_stays_unexplained_with_a_producer_change(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(a_nodes[:2] + b_nodes, a_links[:1] + b_links)
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root, producer_changed=True)

    assert files_by_name(report)["a.py"]["class"] == "unchanged"
    assert report.nodes["unexplained"] == 1 and report.nodes["explained"] == 0
    assert report.edges["unexplained"] == 1


def test_out_of_corpus_needs_a_producer_change(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    old = graph(a_nodes + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    old_m = manifest(a__py="1", b__py="2")
    new_m = manifest(b__py="2")

    refused = classify_loss(old, new, old_m, new_m, root)
    accepted = classify_loss(old, new, old_m, new_m, root, producer_changed=True)

    assert refused.verdict == "refuse"
    assert any(r.startswith("a.py is on disk but left the corpus") for r in refused.reasons)
    assert accepted.verdict == "accept", accepted.reasons
    assert files_by_name(accepted)["a.py"]["class"] == "out_of_corpus"


def test_degree_zero_external_stub_is_an_orphan(root):
    _, _, b_nodes, b_links = baseline()
    stubs = [node(f"ext{i}", external=True) for i in range(5)]
    old = graph(b_nodes + stubs, b_links)
    new = graph(b_nodes, b_links)
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.verdict == "accept", report.reasons
    assert report.nodes["orphan"] == 5 and report.nodes["unexplained"] == 0
    assert report.files == []


def test_connected_external_node_is_not_an_orphan(root):
    _, _, b_nodes, b_links = baseline()
    ext = node("ext", external=True)
    old = graph([*b_nodes, ext], [*b_links, edge("b0", "ext")])
    new = graph(b_nodes, b_links)
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.nodes["orphan"] == 0
    assert files_by_name(report)["<none>"] == {
        "file": "<none>",
        "class": "none",
        "old": 1,
        "new": 0,
        "lost": 1,
    }


def test_nodes_without_a_source_file_are_unexplained(root):
    _, _, b_nodes, b_links = baseline()
    loose, loose_links = chain("x", None, 4)
    old = graph(b_nodes + loose, b_links + loose_links)
    new = graph(b_nodes, b_links)
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.nodes["unexplained"] == 4
    assert report.verdict == "refuse"


def test_absolute_source_file_matches_relative_manifest_key(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    absolute = [node(n["id"], str(root / "a.py")) for n in a_nodes]
    old = graph(absolute + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)

    report = classify_loss(
        old, new, manifest(a__py="1", b__py="2"), manifest(a__py="X", b__py="2"), root
    )

    assert files_by_name(report)["a.py"]["class"] == "changed"


def test_relative_root_matches_absolute_source_files(root, monkeypatch):
    a_nodes, a_links, b_nodes, b_links = baseline()
    absolute = [node(n["id"], str(root / "a.py")) for n in a_nodes]
    old = graph(absolute + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    monkeypatch.chdir(root.parent)

    report = classify_loss(
        old, new, manifest(a__py="1", b__py="2"), manifest(a__py="X", b__py="2"), Path(root.name)
    )

    assert files_by_name(report)["a.py"]["class"] == "changed"


@pytest.mark.parametrize("recorded_under", ["link", "real"])
def test_symlinked_root_matches_source_files_under_either_spelling(root, recorded_under):
    link = root.parent / "link"
    link.symlink_to(root, target_is_directory=True)
    recorded, given = (link, root) if recorded_under == "link" else (root, link)
    a_nodes, a_links, b_nodes, b_links = baseline()
    absolute = [node(n["id"], str(recorded / "a.py")) for n in a_nodes]
    old = graph(absolute + b_nodes, a_links + b_links)
    new = graph(b_nodes, b_links)
    old_m = {str(recorded / "a.py"): {"ast_hash": "1"}, "b.py": {"ast_hash": "2"}}
    new_m = {str(recorded / "a.py"): {"ast_hash": "X"}, "b.py": {"ast_hash": "2"}}

    report = classify_loss(old, new, old_m, new_m, given)

    assert files_by_name(report)["a.py"]["class"] == "changed"


def test_resolver_edges_lost_between_surviving_nodes_refuse(root):
    # Mirrors cem.messages: 1534 of 5766 `_resolver` edges vanished while both endpoints stayed.
    _, _, b_nodes, b_links = baseline()
    calls = [
        edge(f"b{i}", f"b{(i + 3) % 10}", file="b.py", resolver="php_member_calls")
        for i in range(10)
    ]
    old = graph(b_nodes, b_links + calls)
    new = graph(b_nodes, b_links + calls[:7])
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.verdict == "refuse"
    assert report.edges["resolver_old"] == 10 and report.edges["resolver_lost"] == 3
    assert report.reasons == ["3 of 10 resolver edges lost between surviving nodes (tolerance 0.1)"]


def test_resolver_edges_lost_in_a_changed_file_are_explained(root):
    # An edit that removed the calls explains them, even though both endpoints survived.
    _, _, b_nodes, b_links = baseline()
    calls = [
        edge(f"b{i}", f"b{(i + 3) % 10}", file="b.py", resolver="php_member_calls")
        for i in range(10)
    ]
    old = graph(b_nodes, b_links + calls)
    new = graph(b_nodes, b_links + calls[:5])

    report = classify_loss(old, new, manifest(b__py="1"), manifest(b__py="2"), root)

    assert report.verdict == "accept", report.reasons
    assert report.edges["resolver_lost"] == 0 and report.edges["explained"] == 5


def resolver_calls_from_a(file=None):
    """a.py's three nodes each call into b.py through the resolver: ten edges in all."""
    a_nodes, a_links, b_nodes, b_links = baseline()
    calls = [edge(f"a{i % 3}", f"b{i}", file=file, resolver="php_member_calls") for i in range(10)]
    old = graph(a_nodes + b_nodes, a_links + b_links + calls)
    new = graph(a_nodes + b_nodes, a_links + b_links + calls[:5])
    return old, new


def test_resolver_edges_explained_by_the_calling_nodes_changed_file(root):
    old, new = resolver_calls_from_a(file=None)

    report = classify_loss(
        old, new, manifest(a__py="1", b__py="2"), manifest(a__py="X", b__py="2"), root
    )

    assert report.verdict == "accept", report.reasons
    assert report.edges["resolver_lost"] == 0 and report.edges["explained"] == 5


def test_resolver_edges_from_an_unchanged_caller_are_protected(root):
    old, new = resolver_calls_from_a(file="a.py")
    m = manifest(a__py="1", b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.edges["resolver_lost"] == 5
    assert report.reasons == ["5 of 10 resolver edges lost between surviving nodes (tolerance 0.1)"]


def test_resolver_edges_in_a_file_out_of_corpus_need_a_producer_change(root):
    old, new = resolver_calls_from_a(file="a.py")
    old_m, new_m = manifest(a__py="1", b__py="2"), manifest(b__py="2")

    refused = classify_loss(old, new, old_m, new_m, root)
    accepted = classify_loss(old, new, old_m, new_m, root, producer_changed=True)

    assert refused.edges["resolver_lost"] == 5 and refused.verdict == "refuse"
    assert accepted.edges["resolver_lost"] == 0 and accepted.edges["explained"] == 5
    assert accepted.verdict == "accept", accepted.reasons


def test_resolver_edges_with_no_file_anywhere_stay_protected(root):
    # Nothing on disk can explain a call whose edge and caller name no file.
    _, _, b_nodes, b_links = baseline()
    loose = [node(n["id"]) for n in b_nodes]
    calls = [edge(f"b{i}", f"b{(i + 3) % 10}", resolver="php_member_calls") for i in range(10)]
    old = graph(loose, b_links + calls)
    new = graph(loose, b_links + calls[:5])
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root)

    assert report.edges["resolver_lost"] == 5 and report.verdict == "refuse"


def test_resolver_edges_to_a_deleted_file_are_explained(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    (root / "a.py").unlink()
    calls = [edge(f"b{i}", "a0", file="b.py", resolver="php_member_calls") for i in range(10)]
    old = graph(a_nodes + b_nodes, a_links + b_links + calls)
    new = graph(b_nodes, b_links)

    report = classify_loss(old, new, manifest(a__py="1", b__py="2"), manifest(b__py="2"), root)

    assert report.verdict == "accept", report.reasons
    assert report.edges["resolver_lost"] == 0 and report.edges["explained"] == 12


def test_plain_edges_lost_between_survivors_are_unexplained(root):
    _, _, b_nodes, b_links = baseline()
    m = manifest(b__py="2")

    report = classify_loss(graph(b_nodes, b_links), graph(b_nodes, b_links[:7]), m, m, root)

    # 2 of 9 edges unexplained; floor = ceil(9 * 0.9) = 9.
    assert report.edges["unexplained"] == 2
    assert report.verdict == "refuse"
    assert report.reasons == ["unexplained edge loss 2 exceeds tolerance 0.1 of 9 edges"]


def test_edge_whose_file_changed_is_explained(root):
    _, _, b_nodes, b_links = baseline()

    report = classify_loss(
        graph(b_nodes, b_links),
        graph(b_nodes, b_links[:7]),
        manifest(b__py="1"),
        manifest(b__py="2"),
        root,
    )

    assert report.edges["explained"] == 2
    assert report.verdict == "accept", report.reasons


def test_edges_key_is_read_as_well_as_links(root):
    _, _, b_nodes, b_links = baseline()
    m = manifest(b__py="2")

    report = classify_loss(
        graph(b_nodes, b_links, key="edges"), graph(b_nodes, b_links[:7], key="edges"), m, m, root
    )

    assert report.edges["old"] == 9 and report.edges["new"] == 7


def test_growth_never_refuses(root):
    a_nodes, a_links, b_nodes, b_links = baseline()
    m = manifest(a__py="1", b__py="2")
    old = graph(b_nodes, b_links)
    new = graph(a_nodes + b_nodes, a_links + b_links)

    for policy in ("attributed", "connected", "count"):
        report = classify_loss(old, new, m, m, root, policy=policy)
        assert report.verdict == "accept", (policy, report.reasons)


def test_count_policy_floors_raw_totals(root):
    _, _, b_nodes, b_links = baseline()
    stubs = [node(f"ext{i}", external=True) for i in range(5)]
    old = graph(b_nodes + stubs, b_links)
    new = graph(b_nodes, b_links)
    m = manifest(b__py="2")

    report = classify_loss(old, new, m, m, root, policy="count")

    # The attributed policy calls these orphans; the legacy count floor does not know that.
    assert report.verdict == "refuse"
    assert report.nodes["base"] == 15 and report.nodes["floor"] == 14
    assert report.reasons == ["nodes 15 -> 10, below floor 14"]


def test_connected_policy_ignores_degree_zero_nodes(root):
    _, _, b_nodes, b_links = baseline()
    stubs = [node(f"ext{i}") for i in range(5)]
    old = graph(b_nodes + stubs, b_links)
    m = manifest(b__py="2")

    kept = classify_loss(old, graph(b_nodes, b_links), m, m, root, policy="connected")
    shrunk = classify_loss(old, graph(b_nodes[:5], b_links[:4]), m, m, root, policy="connected")

    assert kept.verdict == "accept" and kept.nodes["base"] == 10
    assert shrunk.verdict == "refuse"
    assert shrunk.reasons == [
        "connected nodes 10 -> 5, below floor 9",
        "edges 9 -> 4, below floor 9",
    ]


def test_off_policy_reports_but_never_refuses(root):
    m = manifest(b__py="2")
    b_nodes, b_links = chain("b", "b.py", 10)

    report = classify_loss(graph(b_nodes, b_links), graph([], []), m, m, root, policy="off")

    assert report.verdict == "accept" and report.nodes["lost"] == 10


def test_unknown_policy_and_bad_tolerance_raise(root):
    with pytest.raises(ValueError, match="policy"):
        classify_loss({}, {}, {}, {}, root, policy="nope")
    with pytest.raises(ValueError, match="tolerance"):
        classify_loss({}, {}, {}, {}, root, tolerance=1.5)


def test_files_are_sorted_by_loss_and_capped(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    nodes, links = [], []
    for f in range(60):
        n, ln = chain(f"f{f}_", f"f{f}.py", (f % 4) + 2)
        nodes += n
        links += ln
    old = graph(nodes, links)

    report = classify_loss(old, graph([], []), {}, {}, repo)

    assert len(report.files) == 50
    losses = [f["lost"] for f in report.files]
    assert losses == sorted(losses, reverse=True) and losses[0] == 5
    assert {f["class"] for f in report.files} == {"deleted"}


def test_to_dict_carries_the_contract_keys(root):
    report = classify_loss(graph([], []), graph([], []), {}, {}, root)

    d = report.to_dict()
    assert set(d) == {"verdict", "policy", "tolerance", "nodes", "edges", "reasons", "files"}
    assert set(d["nodes"]) == {
        "old",
        "new",
        "lost",
        "orphan",
        "explained",
        "unexplained",
        "base",
        "floor",
    }
    assert set(d["edges"]) == {
        "old",
        "new",
        "lost",
        "explained",
        "unexplained",
        "resolver_old",
        "resolver_lost",
    }


# --- real builds -------------------------------------------------------------------------------


def load_fixture(name: str, tmp_path: Path) -> tuple[dict, dict, dict, dict, Path]:
    """Graphs and manifests as published, plus a root holding exactly the files that were on disk
    in the real repository when the fixture was captured (present.txt), as empty files."""
    d = FIXTURES / name
    repo = tmp_path / name
    for rel in (d / "present.txt").read_text().split():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    load = lambda f: json.loads((d / f).read_text())  # noqa: E731
    return (
        load("old_graph.json"),
        load("new_graph.json"),
        load("old_manifest.json"),
        load("new_manifest.json"),
        repo,
    )


def test_ckeditor_degree_zero_externals_are_accepted(tmp_path):
    old, new, old_m, new_m, repo = load_fixture("ckeditor", tmp_path)

    report = classify_loss(old, new, old_m, new_m, repo)

    assert report.verdict == "accept", report.reasons
    assert report.nodes == {
        "old": 189,
        "new": 157,
        "lost": 43,
        "orphan": 37,
        "explained": 5,
        "unexplained": 1,
        "base": 152,
        "floor": 137,
    }
    # The six non-orphan losses are renames in hash-unchanged tests/manual/* files; only
    # ckeditor-cjs-version.md really shrank (2 -> 1).
    assert report.files[0] == {
        "file": "tests/manual/ckeditor-cjs-version.md",
        "class": "unchanged",
        "old": 2,
        "new": 1,
        "lost": 1,
    }
    assert report.edges["old"] == 198 and report.edges["new"] == 208


def test_games_needs_a_producer_change_for_claude_md(tmp_path):
    old, new, old_m, new_m, repo = load_fixture("games", tmp_path)

    refused = classify_loss(old, new, old_m, new_m, repo)
    accepted = classify_loss(old, new, old_m, new_m, repo, producer_changed=True)

    assert refused.verdict == "refuse"
    assert refused.reasons == [
        "CLAUDE.md is on disk but left the corpus (20 nodes lost) "
        "with no producer change to explain it"
    ]
    assert accepted.verdict == "accept", accepted.reasons
    assert accepted.nodes == {
        "old": 364,
        "new": 282,
        "lost": 209,
        "orphan": 0,
        "explained": 203,
        "unexplained": 6,
        "base": 363,
        "floor": 327,
    }
    by_file = files_by_name(accepted)
    assert by_file["CLAUDE.md"]["class"] == "out_of_corpus"
    assert by_file["package.json"] == {
        "file": "package.json",
        "class": "unchanged",
        "old": 51,
        "new": 51,
        "lost": 0,
    }
    # graphify moved tsconfig's `ref_*` nodes to an empty source_file; the files did not change,
    # so the count drop stays unexplained, and stays inside the tolerance.
    assert by_file["tsconfig.json"]["lost"] == 5 and by_file["tsconfig.node.json"]["lost"] == 1
    deleted = [f for f in accepted.files if f["class"] == "deleted"]
    assert sum(f["lost"] for f in deleted) == 163
    assert all(f["file"].startswith(("doc/tasks/", "bench/")) for f in deleted)
