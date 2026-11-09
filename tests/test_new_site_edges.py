"""graphify's cross-file pass can bind `new Type(...)` to a METHOD named `type()`.

graphify sends `new Foo(...)` as a raw call to `Foo`. When `Foo` is a vendor class its
case-sensitive lookup misses, and the PHP fallback (a case-insensitive label index) lands on a
same-named method node, emitting `calls` INFERRED 0.85. The member-call pass drops such an edge
only on parser evidence: a `new` of that class name on the edge's line and no call of that
method name there. The real index, sink and parser over a synthetic graph and a PHP file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify_php import resolver, status
from graphify_php.graph_adapter import EDGE_MARKER, EDGE_ORIGIN, GraphNodeIndex
from graphify_php.sources.tree_sitter_source import TreeSitterSource
from graphify_php.syntax import nodes as syntax_nodes

pytestmark = pytest.mark.skipif(
    not syntax_nodes.tree_sitter_available()[0], reason="tree-sitter-php not installed"
)

FORM = """<?php
namespace App\\Form;

use Symfony\\Component\\Validator\\Constraints\\Type;

class PlanForm
{
    public function build($b, $e)
    {
        $b->add('x', null, ['constraints' => [
            new Type(type: 'numeric'),
        ]]);
        // new Type( only in a comment
        $s = "new Type(";
        $e->type(new Type());
        new Count(1);
        new \\Symfony\\Component\\HttpFoundation\\Session\\Session();
        $e->$dynamic(new Type());
        Type::type(new Type());
        type(new Type());
        $e?->type(new Type());
        $x = new Type();
    }
}
"""
# Lines in FORM, 1-based.
L_NEW, L_COMMENT, L_STRING, L_MEMBER, L_COUNT, L_SESSION = 11, 13, 14, 15, 16, 17
L_DYNAMIC, L_SCOPED, L_FUNCTION, L_NULLSAFE, L_ASSIGN = 18, 19, 20, 21, 22

CALLER = "planform_build"
TYPE_METHOD = "enum_type"
SESSION_METHOD = "manager_session"
COUNT_CLASS = "count"
ANON_METHOD = "anon_type"


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "graphify-out" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "PlanForm.php").write_text(FORM)
    return tmp_path


def graph(repo: Path):
    form = str(repo / "src" / "PlanForm.php")
    enum = str(repo / "src" / "TagEnum.php")
    manager = str(repo / "src" / "SessionManager.php")
    nodes = [
        {"id": "form_file", "label": "PlanForm.php", "source_file": form},
        {"id": "planform", "label": "PlanForm", "_callable_class": True, "source_file": form},
        {"id": CALLER, "label": ".build()", "source_file": form, "source_location": "L8"},
        {"id": "enum_file", "label": "TagEnum.php", "source_file": enum},
        {"id": "enum", "label": "TagEnum", "_callable_class": True, "source_file": enum},
        {"id": TYPE_METHOD, "label": ".type()", "source_file": enum, "source_location": "L5"},
        {"id": "manager", "label": "SessionManager", "_callable_class": True},
        {"id": SESSION_METHOD, "label": ".session()", "source_file": manager},
        {"id": COUNT_CLASS, "label": "Count", "_callable_class": True, "source_file": enum},
        # An anonymous class's method: no class node, no `method` edge, no leading dot.
        {"id": ANON_METHOD, "label": "type()", "source_file": enum},
    ]
    edges = [
        {"source": "form_file", "target": "planform", "relation": "contains"},
        {"source": "enum_file", "target": "enum", "relation": "contains"},
        {"source": "planform", "target": CALLER, "relation": "method"},
        {"source": "enum", "target": TYPE_METHOD, "relation": "method"},
        {"source": "manager", "target": SESSION_METHOD, "relation": "method"},
    ]
    return nodes, edges, form


def cross_file(form: str, target: str, line: int, **extra) -> dict:
    """The edge graphify's cross-file bare-name pass writes for a case-folded hit."""
    return {
        "source": CALLER,
        "target": target,
        "relation": "calls",
        "confidence": "INFERRED",
        "confidence_score": 0.85,
        "source_file": form,
        "source_location": f"L{line}",
        "weight": 1.0,
        **extra,
    }


def run(repo: Path, lines_targets, *, owned=True, extra=()):
    nodes, edges, form = graph(repo)
    built = [cross_file(form, target, line) for line, target in lines_targets]
    edges = edges + built + list(extra)
    record = resolver.run(
        [{"nodes": [], "edges": [], "raw_calls": []}],
        nodes,
        edges,
        sources=[TreeSitterSource()],
        repo_root=str(repo),
        edges_owned=owned,
    )
    return record, edges, built


def kept(edges, built):
    ids = {id(e) for e in edges}
    return [int(e["source_location"][1:]) for e in built if id(e) in ids]


def test_a_new_bound_to_a_same_named_method_is_dropped_and_listed(repo, status_file):
    record, edges, built = run(repo, [(L_NEW, TYPE_METHOD), (L_SESSION, SESSION_METHOD)])

    assert kept(edges, built) == []
    assert (record.new_site_edges_total, record.new_site_edges_dropped) == (2, 2)
    assert record.new_site_kept == {}
    assert record.new_site_dropped_edges == [
        {"source": CALLER, "target": TYPE_METHOD, "source_location": f"L{L_NEW}"},
        {"source": CALLER, "target": SESSION_METHOD, "source_location": f"L{L_SESSION}"},
    ]
    written = json.loads(status_file.read_text())["resolvers"][0]
    assert written["new_site_edges_dropped"] == 2
    assert len(written["new_site_dropped_edges"]) == 2


def test_new_only_in_a_comment_or_a_string_is_no_evidence(repo):
    record, edges, built = run(repo, [(L_COMMENT, TYPE_METHOD), (L_STRING, TYPE_METHOD)])

    assert kept(edges, built) == [L_COMMENT, L_STRING]
    assert (record.new_site_edges_total, record.new_site_edges_dropped) == (0, 0)


@pytest.mark.parametrize("line", [L_MEMBER, L_DYNAMIC, L_SCOPED, L_FUNCTION, L_NULLSAFE])
def test_a_call_of_that_name_on_the_line_keeps_the_edge(repo, line):
    record, edges, built = run(repo, [(line, TYPE_METHOD)])

    assert kept(edges, built) == [line]
    assert record.new_site_edges_total == 1
    assert record.new_site_kept == {resolver.REASON_NEW_SITE_SHARED_LINE: 1}


def test_a_class_node_target_is_kept_and_not_counted(repo):
    record, edges, built = run(repo, [(L_COUNT, COUNT_CLASS)])

    assert kept(edges, built) == [L_COUNT]
    assert record.new_site_edges_total == 0


def test_an_anonymous_class_method_without_dot_or_method_edge_is_not_a_method(repo):
    record, edges, built = run(repo, [(L_ASSIGN, ANON_METHOD)])

    assert kept(edges, built) == [L_ASSIGN]
    assert record.new_site_edges_total == 0


def test_an_edge_of_ours_is_never_touched(repo):
    nodes, _edges, form = graph(repo)
    ours = {**cross_file(form, TYPE_METHOD, L_ASSIGN), EDGE_MARKER: EDGE_ORIGIN}
    record, edges, _built = run(repo, [], extra=[ours])

    assert ours in edges
    assert record.new_site_edges_total == 0


def test_an_edge_list_not_owned_keeps_the_edge_with_a_reason(repo):
    record, edges, built = run(repo, [(L_NEW, TYPE_METHOD)], owned=False)

    assert kept(edges, built) == [L_NEW]
    assert record.new_site_edges_total == 1
    assert record.new_site_edges_dropped == 0
    assert record.new_site_kept == {resolver.REASON_NEW_SITE_NOT_OWNED: 1}
    assert record.new_site_dropped_edges == []


def test_a_line_that_cannot_be_read_keeps_the_edge_with_a_reason(repo):
    nodes, _edges, form = graph(repo)
    missing = cross_file(str(repo / "src" / "Gone.php"), TYPE_METHOD, 3)
    beyond = cross_file(form, TYPE_METHOD, 999)
    record, edges, _built = run(repo, [], extra=[missing, beyond])

    assert missing in edges and beyond in edges
    assert record.new_site_edges_total == 2
    assert record.new_site_kept == {resolver.REASON_NEW_SITE_NOT_LOCATED: 2}


def test_dropping_frees_the_pair_for_a_proved_edge(repo):
    """The pass runs before our edges are written, so a proof of the same pair still lands."""
    from graphify_php.ports import CallTarget, Confidence, Resolution

    class Proves:
        name = "proves"

        def available(self, repo_root):
            return True, ""

        def resolve(self, repo_root, sites):
            return Resolution(
                targets=[
                    CallTarget(
                        site=site,
                        class_fqn="TagEnum",
                        method="type",
                        confidence=Confidence.EXTRACTED,
                        via="test",
                    )
                    for site in sites
                ],
                unresolved={},
            )

    nodes, edges, form = graph(repo)
    wrong = cross_file(form, TYPE_METHOD, L_NEW)
    edges.append(wrong)
    raw = {
        "caller_nid": CALLER,
        "callee": "type",
        "is_member_call": True,
        "source_file": form,
        "source_location": "L30",
    }
    record = resolver.run(
        [{"nodes": [], "edges": [], "raw_calls": [raw]}],
        nodes,
        edges,
        sources=[TreeSitterSource(), Proves()],
        repo_root=str(repo),
        index=GraphNodeIndex(nodes, edges),
        edges_owned=True,
    )

    assert wrong not in edges
    assert record.new_site_edges_dropped == 1
    proved = [e for e in edges if e.get(EDGE_MARKER) == EDGE_ORIGIN]
    assert [(e["source"], e["target"]) for e in proved] == [(CALLER, TYPE_METHOD)]


def test_through_graphifys_own_resolver_runner_the_build_list_loses_the_edge(repo):
    registry = pytest.importorskip("graphify.resolver_registry")
    # Named as extract.py names it: ownership is read from the runner's caller's `all_edges`.
    nodes, all_edges, form = graph(repo)
    wrong = cross_file(form, TYPE_METHOD, L_NEW)
    all_edges.append(wrong)
    records = []

    def resolve(per_file, all_nodes, all_edges):
        records.append(
            resolver.run(
                per_file, all_nodes, all_edges, sources=[TreeSitterSource()], repo_root=str(repo)
            )
        )

    entry = registry.LanguageResolver(name="probe", suffixes=frozenset({".php"}), resolve=resolve)
    registry.run_language_resolvers(
        [Path("a.php")], [{"raw_calls": []}], nodes, all_edges, resolvers=[entry]
    )

    assert wrong not in all_edges
    assert records[0].new_site_edges_dropped == 1
