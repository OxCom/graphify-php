"""The engine-edge pass over graphify's real extraction and the real parser.

Only PHPStan is faked: `line_proofs` answers from a table keyed by (line, method), as the real
source answers from its records after the caller check. A type engine files a closure's calls
under the method around it, so the table hands a closure's record to the method too, as the
real records do. What is under test is which proved targets become edges and which engine
edges are considered at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graphify_php import resolver, status
from graphify_php.ports import CallTarget, Confidence, LineProof, Resolution
from graphify_php.sources.tree_sitter_source import TreeSitterSource
from graphify_php.syntax import nodes as syntax_nodes

extract = pytest.importorskip("graphify.extract")
pytestmark = pytest.mark.skipif(
    not syntax_nodes.tree_sitter_available()[0], reason="tree-sitter-php not installed"
)


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "status" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


class Records:
    """PHPStan's per-line records as a table; the parser is the real one."""

    name = "records"

    def __init__(self, root: Path, table: dict[tuple[int, str], list[tuple[str, str, str]]]):
        self.root = root
        self.table = table
        self.parser = TreeSitterSource()

    def available(self, repo_root):
        return True, ""

    def resolve(self, repo_root, sites):
        return Resolution(targets=[], unresolved={})

    def line_proofs(self, repo_root, sites):
        out = {}
        for site in sites:
            rows = self.table.get((int(site.source_location.lstrip("L")), site.method), [])
            targets = tuple(
                CallTarget(
                    site=site,
                    class_fqn=cls,
                    method=method,
                    confidence=Confidence.EXTRACTED,
                    via="phpstan",
                    declaring_file=str(self.root / path),
                )
                for cls, method, path in rows
            )
            out[site] = LineProof(targets=targets, calls=len(targets))
        return out

    def member_call_lines(self, *args):
        return self.parser.member_call_lines(*args)

    def method_declared_at(self, *args):
        return self.parser.method_declared_at(*args)


def build(root: Path, files: dict[str, str], table) -> tuple[status.MemberCallStatus, set]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    out = extract.extract(sorted(root.rglob("*.php")), root=root, parallel=False)
    nodes, edges = out["nodes"], out["edges"]
    label = {n["id"]: n.get("label") for n in nodes}
    record = resolver.run(
        [{"nodes": [], "edges": [], "raw_calls": []}],
        nodes,
        edges,
        sources=[Records(root, table)],
        repo_root=str(root),
        edges_owned=True,
    )
    # (caller label, callee label, line, ours) per call edge; ids carry the tmp path.
    found = {
        (
            label[e["source"]],
            label[e["target"]],
            e.get("source_location"),
            "_resolver" in e,
        )
        for e in edges
        if e.get("relation") == "calls"
    }
    return record, found


A = "<?php\nnamespace App;\nclass A { public function get($f = null) {} }\n"
B = "<?php\nnamespace App;\nclass B { public function get() {} }\n"
PROOF_A = ("App\\A", "get", "src/A.php")
PROOF_B = ("App\\B", "get", "src/B.php")

CLOSURE_BELOW = """<?php
namespace App;
class M {
    public function get() {}
    public function run(A $a, B $b) {
        $a->get(1);
        $f = function () use ($b) {
            return $b->get();
        };
    }
}
"""


def test_a_closures_proved_call_is_never_edged_from_the_method_around_it(tmp_path):
    files = {"src/A.php": A, "src/B.php": B, "src/M.php": CLOSURE_BELOW}
    # The record of `$b->get()` on 8 names `M::run` as its caller, as PHPStan's does.
    record, found = build(tmp_path, files, {(6, "get"): [PROOF_A], (8, "get"): [PROOF_B]})

    assert {(s, t, line) for s, t, line, ours in found if ours} == {
        (".run()", ".get()", "L6"),
        ("{closure#1}()", ".get()", "L8"),
    }
    targets = {(s, line) for s, _t, line, _ours in found}
    assert (".run()", "L8") not in targets
    assert record.engine_dropped == 2
    assert record.unresolved == {"engine:proof-in-nested-callable": 1}


SAME_LINE = """<?php
namespace App;
class X { public function get() {} }
class M {
    public function run(A $a, B $b) {
        $a->get(function () use ($b) { return $b->get(); });
    }
}
"""


def test_a_closure_on_the_methods_line_edges_neither_side(tmp_path):
    files = {"src/A.php": A, "src/B.php": B, "src/M.php": SAME_LINE}
    # Both records on 6 name `M::run`: one is the method's call, one the closure's.
    record, found = build(tmp_path, files, {(6, "get"): [PROOF_A, PROOF_B]})

    assert {f for f in found if f[3]} == set()
    assert record.engine_dropped == 2
    assert record.engine_proved_added == 0
    assert record.unresolved == {
        "engine:proof-in-nested-callable": 2,
        "engine:proof-on-shared-line": 2,
    }


CLOSURE_ENDS_ON_A_CALL = """<?php
namespace App;
class X { public function get() {} }
class M {
    public function run(A $a, B $b, $list) {
        array_map(function () use ($b) {
            return $b->get();
        }, [$a->get()]);
    }
}
"""


def test_a_closures_last_line_holding_the_methods_call_is_refused(tmp_path):
    files = {"src/A.php": A, "src/B.php": B, "src/M.php": CLOSURE_ENDS_ON_A_CALL}
    # No end lines in the graph: only the parser sees `$a->get()` after the closure's `}`.
    table = {(7, "get"): [PROOF_B], (8, "get"): [PROOF_A]}
    record, found = build(tmp_path, files, table)

    assert ("{closure#1}()", ".get()", "L8", True) not in found
    assert ("{closure#1}()", ".get()", "L7", True) in found
    assert record.unresolved.get("engine:proof-on-shared-line", 0) >= 1


FUNCTION_TARGET = """<?php
namespace App;

function helper(): void {}

class A
{
    public function run(B $b): void
    {
        $b->helper();
        helper();
    }
}
"""
B_HELPER = "<?php\nnamespace App;\nclass B { public function helper() {} }\n"


def test_an_engine_edge_to_a_free_function_is_left_alone(tmp_path):
    files = {"src/B.php": B_HELPER, "src/A.php": FUNCTION_TARGET}
    table = {(10, "helper"): [("App\\B", "helper", "src/B.php")]}
    record, found = build(tmp_path, files, table)

    assert (".run()", "helper()", "L10", False) in found
    assert record.engine_dropped == 0
    assert record.engine_dropped_edges == []
    assert record.engine_kept == {resolver.REASON_ENGINE_TARGET_NOT_METHOD: 1}


METHOD_AND_FUNCTION_CALL = """<?php
namespace App;

class A
{
    public function helper(B $b): void
    {
        $b->helper();
        helper();
    }
}
"""


def test_a_function_call_of_the_name_in_the_caller_keeps_the_edge(tmp_path):
    # The engine binds `helper()` and `$b->helper()` alike to the last `helper` in the file
    # and keeps one edge at the first line: it may stand for the function call.
    files = {"src/B.php": B_HELPER, "src/A.php": METHOD_AND_FUNCTION_CALL}
    table = {(8, "helper"): [("App\\B", "helper", "src/B.php")]}
    record, found = build(tmp_path, files, table)

    assert (".helper()", ".helper()", "L8", False) in found
    assert record.engine_dropped == 0
    assert record.engine_kept == {resolver.REASON_ENGINE_OTHER_CALL_KIND: 1}


ANONYMOUS = """<?php
namespace App;

class A
{
    public function run($y): void
    {
        $y->getData();
        $x = new class {
            public function getData() { return 1; }
        };
    }
}
"""
D = "<?php\nnamespace App;\nclass D { public function getData() {} }\n"


def test_an_anonymous_class_method_target_is_still_checked(tmp_path):
    files = {"src/D.php": D, "src/A.php": ANONYMOUS}
    table = {(8, "getData"): [("App\\D", "getData", "src/D.php")]}
    record, found = build(tmp_path, files, table)

    # The engine bound `$y->getData()` to the anonymous class's dot-less `getData()`.
    assert (".run()", "getData()", "L8", False) not in found
    assert (".run()", ".getData()", "L8", True) in found
    assert record.engine_dropped == 1
    assert record.engine_kept == {}


TWO_ACTIVITIES = """<?php
namespace App;

use App\\Handler\\Activities;
use App\\Security\\Activities as ActivitiesPermissions;

class Ctl
{
    public function live(Activities $a, ActivitiesPermissions $p): void
    {
        $a->live();
    }
}
"""


def test_a_proof_is_placed_by_its_declaring_file_when_two_visible_classes_share_a_name(
    tmp_path,
):
    files = {
        "src/Ctl.php": TWO_ACTIVITIES,
        "src/Handler/Activities.php": (
            "<?php\nnamespace App\\Handler;\nclass Activities { public function live() {} }\n"
        ),
        "src/Security/Activities.php": (
            "<?php\nnamespace App\\Security;\nclass Activities { public function live() {} }\n"
        ),
    }
    table = {(11, "live"): [("App\\Handler\\Activities", "live", "src/Handler/Activities.php")]}
    record, found = build(tmp_path, files, table)

    assert record.engine_dropped == 1
    assert record.engine_proved_added == 1
    assert record.unresolved == {}
    assert record.engine_dropped_edges[0]["proof"]["target"] is not None
    assert "handler_activities" in record.engine_dropped_edges[0]["proof"]["target"]


ANNOTATED = """<?php
namespace App;

use App\\Repo\\UserRepository;

class Ctl
{
    public function find(UserRepository $r): void
    {
        $r->find(1);
    }
}
"""


def test_a_docblock_method_with_no_node_is_counted_apart(tmp_path):
    files = {
        "src/Ctl.php": ANNOTATED,
        "src/Repo/UserRepository.php": (
            "<?php\nnamespace App\\Repo;\n/**\n * @method object|null find($id)\n */\n"
            "class UserRepository {}\n"
        ),
    }
    table = {(10, "find"): [("App\\Repo\\UserRepository", "find", "src/Repo/UserRepository.php")]}
    record, found = build(tmp_path, files, table)

    assert record.engine_dropped == 1
    assert record.unresolved == {"engine:" + resolver.REASON_ENGINE_METHOD_NO_NODE: 1}
    assert record.engine_dropped_edges[0]["proof"]["target"] is None
