"""graphify core's own `php_member_calls` pass, kept off the sites graphify-php decided.

graphify 0.9.103 registers it on import of `graphify.extract`, after ours, and it dedupes only by
(caller, target) pair: at a site graphify-php proved it still adds its own guess. It is withheld
only there: a site graphify-php refused keeps core's edge.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphify_php import core_guard, resolver, status
from graphify_php.ports import CallTarget, Confidence, Resolution

registry = pytest.importorskip("graphify.resolver_registry")
# Imported before any test swaps `_REGISTRY`, so core registers into the real one.
extract = pytest.importorskip("graphify.extract")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(tmp_path / "status.json"))
    monkeypatch.setattr(registry, "_REGISTRY", [])
    core_guard.prove(None, ())  # no slot left over from another test
    yield


def _record() -> dict:
    rows = status.read().get("resolvers", [])
    return {r["name"]: r for r in rows}[core_guard.RESOLVER_NAME]


def _core(calls: list):
    def resolve(per_file, all_nodes, all_edges):
        calls.append(per_file)

    return resolve


def _ours():
    def resolve(per_file, all_nodes, all_edges):
        return None

    setattr(resolve, core_guard.OWNED, True)
    return registry.LanguageResolver(core_guard.CORE_NAME, frozenset({".php"}), resolve)


def _entry(resolve, suffixes=frozenset({".php"})):
    return registry.LanguageResolver(core_guard.CORE_NAME, suffixes, resolve)


def _raw(callee: str, line: int, member: bool = True) -> dict:
    return {
        "caller_nid": "c_run",
        "callee": callee,
        "receiver": "mailer",
        "is_member_call": member,
        "source_file": "/repo/src/C.php",
        "source_location": f"L{line}",
    }


def test_core_s_entry_is_replaced_in_place_by_a_filter():
    calls: list = []
    before = registry.LanguageResolver("other", frozenset({".py"}), lambda *a: None)
    core = _entry(_core(calls))
    registry._REGISTRY[:] = [before, core]

    core_guard.install(registry)

    after = registry._REGISTRY
    assert len(after) == 2 and after[0] is before
    assert after[1].name == core_guard.CORE_NAME and after[1].suffixes == core.suffixes
    assert after[1].resolve is not core.resolve


def test_core_registered_before_ours_is_moved_to_just_after_our_last_pass():
    core, other = _entry(_core([])), registry.LanguageResolver("x", frozenset({".py"}), print)
    ours_first, ours_last = _ours(), _ours()
    registry._REGISTRY[:] = [core, ours_first, other, ours_last]

    core_guard.install(registry)

    after = registry._REGISTRY
    assert after[:3] == [ours_first, other, ours_last]
    assert core_guard.original_of(after[3].resolve) is core.resolve and len(after) == 4


def test_the_filter_hands_core_only_the_raw_calls_graphify_php_did_not_prove():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    proved, kept = _raw("get", 20), _raw("send", 30)
    per_file = [{"nodes": [], "raw_calls": [proved, kept]}, {"raw_calls": [_raw("x", 5)]}]

    core_guard.prove(per_file, [proved])
    registry._REGISTRY[0].resolve(per_file, [], [])

    assert len(calls) == 2, "the real run, then the dry run over the proved calls"
    handed, dry = calls
    assert handed is not per_file
    assert handed[0]["raw_calls"] == [kept] and handed[0]["raw_calls"][0] is kept
    assert handed[1] is per_file[1], "a file with nothing proved is passed as it was"
    assert len(dry) == 1 and dry[0]["raw_calls"] == [proved] and dry[0]["raw_calls"][0] is proved
    assert per_file[0]["raw_calls"] == [proved, kept], "graphify's own per_file is untouched"
    record = _record()
    assert record["state"] == "completed"
    assert (record["suppressed_sites"], record["core_sites_total"]) == (1, 3)
    assert record["proved_not_found"] == 0


def test_an_equal_raw_call_that_is_not_the_proved_object_is_not_suppressed():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    proved = _raw("get", 20)
    twin = dict(proved)
    per_file = [{"raw_calls": [twin]}]

    core_guard.prove(per_file, [proved])
    registry._REGISTRY[0].resolve(per_file, [], [])

    assert calls[0][0]["raw_calls"] == [twin]
    record = _record()
    assert record["proved_not_found"] == 1
    # Core saw a site graphify-php proved, so its edge there may contradict the proof.
    assert (record["state"], record["reason"]) == ("failed", core_guard.REASON_PROVED_NOT_FOUND)


def test_a_proved_raw_call_seen_in_two_files_counts_once():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    proved = _raw("get", 20)
    per_file = [{"raw_calls": [proved]}, {"raw_calls": [proved]}]

    core_guard.prove(per_file, [proved])
    registry._REGISTRY[0].resolve(per_file, [], [])

    record = _record()
    assert (record["state"], record["proved_not_found"]) == ("completed", 0)
    assert record["suppressed_sites"] == 2


def test_without_a_proved_set_core_is_skipped_never_run_unfiltered():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)

    registry._REGISTRY[0].resolve([{"raw_calls": [_raw("get", 1)]}], [], [])

    assert calls == []
    record = _record()
    assert (record["state"], record["reason"]) == ("failed", core_guard.REASON_PROVED_SET_MISSING)


def test_a_proved_set_from_another_build_is_not_used():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    core_guard.prove([{"raw_calls": []}], [])

    registry._REGISTRY[0].resolve([{"raw_calls": []}], [], [])

    assert calls == []
    assert _record()["reason"] == core_guard.REASON_PROVED_SET_MISSING


def test_the_proved_set_is_used_once():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    per_file = [{"raw_calls": []}]
    core_guard.prove(per_file, [])

    registry._REGISTRY[0].resolve(per_file, [], [])
    registry._REGISTRY[0].resolve(per_file, [], [])

    assert len(calls) == 1
    assert _record()["reason"] == core_guard.REASON_PROVED_SET_MISSING


def test_a_core_failure_is_recorded_and_re_raised():
    def boom(per_file, all_nodes, all_edges):
        raise RuntimeError("core broke")

    registry._REGISTRY[:] = [_entry(boom)]
    core_guard.install(registry)
    per_file = [{"raw_calls": []}]
    core_guard.prove(per_file, [])

    with pytest.raises(RuntimeError):
        registry._REGISTRY[0].resolve(per_file, [], [])

    assert _record()["state"] == "failed"


def test_absent_core_registers_a_recorder_that_reports_skipped():
    registry._REGISTRY[:] = []

    core_guard.install(registry)

    (entry,) = registry._REGISTRY
    assert entry.name == core_guard.RESOLVER_NAME and ".php" in entry.suffixes
    entry.resolve([], [], [])
    record = _record()
    assert (record["state"], record["reason"]) == ("skipped", core_guard.REASON_CORE_ABSENT)


def test_a_same_named_resolver_graphify_php_does_not_own_is_core_wherever_it_lives():
    """A graphify refactor moving core's function must not let it run unfiltered."""
    calls: list = []
    foreign = _core(calls)
    foreign.__module__ = "graphify.php_calls"
    registry._REGISTRY[:] = [_entry(foreign)]

    core_guard.install(registry)

    (entry,) = registry._REGISTRY
    assert core_guard.original_of(entry.resolve) is foreign


@pytest.mark.parametrize("core", ["absent", "ambiguous", "shape"])
def test_a_recorder_clears_the_proof_slot(core):
    """Otherwise `per_file` stays alive in the slot after graphify drops it."""
    registry._REGISTRY[:] = {
        "absent": [],
        "ambiguous": [_entry(_core([])), _entry(_core([]))],
        "shape": [_entry(_core([]), suffixes=frozenset({".inc"}))],
    }[core]
    core_guard.install(registry)
    per_file = [{"raw_calls": []}]
    core_guard.prove(per_file, [])

    for entry in registry._REGISTRY:
        entry.resolve(per_file, [], [])

    assert core_guard._proved is None


def test_two_core_entries_are_both_dropped():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls)), _entry(_core(calls))]

    core_guard.install(registry)
    per_file = [{"raw_calls": [_raw("get", 1)]}]
    core_guard.prove(per_file, [])
    for entry in registry._REGISTRY:
        entry.resolve(per_file, [], [])

    assert calls == []
    record = _record()
    assert (record["state"], record["reason"]) == ("skipped", core_guard.REASON_CORE_AMBIGUOUS)


def test_a_core_entry_of_unexpected_shape_is_dropped():
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls), suffixes=frozenset({".inc"}))]

    core_guard.install(registry)
    registry._REGISTRY[0].resolve([{"raw_calls": []}], [], [])

    assert calls == []
    assert _record()["reason"] == core_guard.REASON_CORE_UNEXPECTED_SHAPE


def test_a_registry_that_is_not_a_list_refuses_the_publish(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", ())
    added = []
    monkeypatch.setattr(registry, "register", added.append)

    core_guard.install(registry)

    (entry,) = added
    entry.resolve([], [], [])
    record = _record()
    assert (record["state"], record["reason"]) == ("failed", core_guard.REASON_REGISTRY_UNEXPECTED)


def test_installing_twice_changes_nothing():
    registry._REGISTRY[:] = [_entry(_core([]))]
    core_guard.install(registry)
    first = list(registry._REGISTRY)

    core_guard.install(registry)

    assert registry._REGISTRY == first


class _Proving:
    name = "fake"
    declaring_file = ""

    def available(self, repo_root):
        return True, ""

    def resolve(self, repo_root, sites, corpus=None):
        return Resolution(
            targets=[
                CallTarget(
                    site=s,
                    class_fqn="App\\Emails",
                    method=s.method,
                    confidence=Confidence.EXTRACTED,
                    via="fake",
                    declaring_file=self.declaring_file,
                )
                for s in sites
                if s.method == "send"
            ],
            unresolved={"refused": sum(1 for s in sites if s.method != "send")},
            files_seen=1,
        )


class _Index:
    def __init__(self, known=(("App\\Emails", "send"),)):
        self.known = set(known)

    def method_node(self, class_fqn, method):
        return f"n_{method}" if (class_fqn, method) in self.known else None


class _Sink:
    def __init__(self):
        self.calls: list = []
        self.added = 0

    def add_call(self, caller_nid, callee_nid, target):
        self.calls.append((caller_nid, callee_nid, target))
        self.added += 1


class _Scopes:
    def __init__(self, methods):
        self.methods = methods

    def scope_methods(self, caller_nid, scope):
        return self.methods.get(scope, [])


def _guarded(per_file, **kwargs) -> list:
    """Run our pass, then the filtered core pass; return the raw calls core was handed."""
    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    kwargs.setdefault("sources", [_Proving()])
    kwargs.setdefault("index", _Index())
    kwargs.setdefault("sink", _Sink())
    resolver.run(per_file, [], [], repo_root="/repo", **kwargs)
    registry._REGISTRY[0].resolve(per_file, [], [])
    return [raw for result in calls[0] for raw in result.get("raw_calls", [])]


def test_our_pass_publishes_only_the_sites_it_proved():
    proved, refused = _raw("send", 10), _raw("get", 11)
    function_call = _raw("trim", 12, member=False)

    handed = _guarded([{"raw_calls": [proved, refused, function_call]}])

    assert handed == [refused, function_call], "a refused site keeps core's own edge"
    record = _record()
    assert (record["suppressed_sites"], record["core_sites_total"]) == (1, 2)
    assert record["proved_not_found"] == 0


def test_a_claimed_site_whose_target_is_not_in_the_graph_is_proved():
    """A vendor class with stubs off: core binding another target there contradicts the proof."""
    claimed = _raw("send", 10)

    handed = _guarded([{"raw_calls": [claimed]}], index=_Index(known=()))

    assert handed == []
    assert _record()["suppressed_sites"] == 1


def test_raw_calls_with_equal_sites_are_each_proved_and_counted():
    """`$this->mailer->send() . $mailer->send()`: graphify sends both as receiver `mailer`."""
    first, second = _raw("send", 10), _raw("send", 10)

    handed = _guarded([{"raw_calls": [first, second]}])

    assert handed == []
    record = _record()
    assert (record["suppressed_sites"], record["proved_not_found"]) == (2, 0)


def test_a_site_edged_to_a_vendor_stub_is_proved():
    class Stubs:
        nodes_added = edges_added = 0

        def node_for(self, class_fqn):
            return "stub"

        def add_call(self, caller_nid, stub_nid, target):
            self.edges_added += 1

    source = _Proving()
    source.declaring_file = "vendor/acme/Emails.php"
    stubs = Stubs()

    handed = _guarded(
        [{"raw_calls": [_raw("send", 10)]}],
        sources=[source],
        index=_Index(known=()),
        stubs=stubs,
    )

    assert handed == [] and stubs.edges_added == 1


def test_a_scoped_group_is_proved_or_refused_as_its_one_raw_call():
    proved = _raw("Emails", 10, member=False)
    refused = _raw("Cache", 11, member=False)
    scopes = _Scopes({"Emails": ["send", "get"], "Cache": ["get"]})

    handed = _guarded([{"raw_calls": [proved, refused]}], scopes=scopes)

    assert handed == [refused]


def test_an_unsent_call_edge_proves_no_raw_call():
    """`unsent_calls` finds calls graphify never sent, so no raw call stands behind its edges."""

    class Unsent(_Proving):
        def unsent_calls(self, repo_root, anchors, members):
            return [
                CallTarget(
                    site=members[0],
                    class_fqn="App\\Emails",
                    method="send",
                    confidence=Confidence.EXTRACTED,
                    via="fake",
                )
            ]

    anchor = {**_raw("make", 10), "php_scoped": True}
    refused = _raw("get", 11)
    sink = _Sink()

    handed = _guarded([{"raw_calls": [anchor, refused]}], sources=[Unsent()], sink=sink)

    assert len(sink.calls) == 1, "the unsent edge was written"
    assert handed == [anchor, refused]


def test_our_pass_failing_publishes_nothing():
    class Raising(_Proving):
        def resolve(self, repo_root, sites, corpus=None):
            raise RuntimeError("source broke")

    calls: list = []
    registry._REGISTRY[:] = [_entry(_core(calls))]
    core_guard.install(registry)
    per_file = [{"raw_calls": [_raw("send", 10)]}]

    with pytest.raises(RuntimeError):
        resolver.run(per_file, [], [], sources=[Raising()], repo_root="/repo")
    registry._REGISTRY[0].resolve(per_file, [], [])

    assert calls == []
    assert _record()["reason"] == core_guard.REASON_PROVED_SET_MISSING


# The registry shape this guard was written against, per graphify release. A new release fails
# here until someone re-reads `graphify/extract.py` and `resolver_registry.py` and adds a row.
PINNED_SHAPE = {
    "0.9.103": {"core_entries": 1, "suffixes": frozenset({".php"})},
    # 0.9.104 changed no PHP code in extract.py, engine.py or resolver_registry.py.
    "0.9.104": {"core_entries": 1, "suffixes": frozenset({".php"})},
    # 0.9.105 left _resolve_php_member_calls, _park_unresolved_member_call and
    # resolver_registry.py byte-identical; its extract.py changes are C#, C++ and markdown.
    "0.9.105": {"core_entries": 1, "suffixes": frozenset({".php"})},
}


def test_the_installed_graphify_registers_core_php_member_calls_as_pinned(monkeypatch):
    version = status.installed_version(status.GRAPHIFY_DISTRIBUTION)
    assert version in PINNED_SHAPE, f"graphify {version}: re-check core's php_member_calls"
    monkeypatch.undo()  # the real registry, as `graphify.extract` populated it
    entries = [
        e
        for e in registry.registered_resolvers()
        if e.name == core_guard.CORE_NAME
        and core_guard.original_of(e.resolve) is extract._resolve_php_member_calls
    ]
    pinned = PINNED_SHAPE[version]
    assert len(entries) == pinned["core_entries"]
    assert all(isinstance(e, registry.LanguageResolver) for e in entries)
    assert all(e.suffixes == pinned["suffixes"] for e in entries)
    assert all(callable(getattr(registry, n, None)) for n in ("register", "run_language_resolvers"))


@pytest.mark.parametrize("core_first", [False, True], ids=["ours-first", "core-first"])
@pytest.mark.parametrize("engine", ["tree_sitter", "phpstan"])
def test_a_real_build_suppresses_core_at_proved_sites_only_by_identity(
    tmp_path, engine, core_first
):
    """Identity of raw calls has to survive from our pass to core's inside graphify's driver.

    A copied raw call would leave `proved_not_found` above zero. `locals_new.php:20` is proved
    (`Cache::get`) only by PHPStan; tree-sitter refuses the reassigned local. Line 26 both
    refuse, so core's own edge there is kept, wrong or not: that is the policy, pinned here.
    """
    pytest.importorskip("tree_sitter_php")
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("locals_new.php", "mail.php", "same_name_traps.php"):
        shutil.copy(ROOT / "eval" / "fixtures" / name, repo / name)
    if engine == "phpstan":
        # Same opt-in as tests/test_phpstan_integration.py: a full PHPStan run is not default.
        project = Path(os.environ.get("GRAPHIFY_PHP_REFERENCE_PROJECT", "/nonexistent"))
        if not (project / "vendor/bin/phpstan").is_file() or shutil.which("php") is None:
            pytest.skip("GRAPHIFY_PHP_REFERENCE_PROJECT names no PHPStan, or no php on PATH")
        (repo / "vendor").symlink_to(project / "vendor")
        (repo / "phpstan.neon").write_text(
            "parameters:\n    level: 0\n    paths:\n        - .\n"
            "    excludePaths:\n        - vendor\n"
        )
    out = tmp_path / "out"
    status_path = out / ".graphify-php-status.json"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        # Core-first: core's entry is registered before ours, so the order is the guard's job.
        + ("import graphify.extract\n" if core_first else "")
        + "import graphify_php\n"
        f"graphify_php.register(repo_root={str(repo)!r})\n"
        f"sys.argv = ['graphify', 'update', {str(repo)!r}, '--no-cluster']\n"
        "from graphify.__main__ import main\n"
        "try:\n    main()\nexcept SystemExit as exc:\n    if exc.code:\n        raise\n"
    )
    env = {
        **os.environ,
        "GRAPHIFY_OUT": str(out),
        "GRAPHIFY_PHP_STATUS": str(status_path),
        "PYTHONHASHSEED": "0",
    }
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=900
    )
    assert done.returncode == 0, done.stderr

    rows = {r["name"]: r for r in json.loads(status_path.read_text())["resolvers"]}
    guard = rows[core_guard.RESOLVER_NAME]
    assert guard["state"] == "completed", guard
    assert guard["proved_not_found"] == 0
    assert 0 < guard["suppressed_sites"] < guard["core_sites_total"], "refused sites reach core"

    graph = json.loads((out / "graph.json").read_text())
    label = {n["id"]: n.get("label", "") for n in graph["nodes"]}
    owner = {e["target"]: e["source"] for e in graph["links"] if e["relation"] == "method"}

    def core_at(line: str) -> set[tuple[str, str]]:
        return {
            (label.get(owner.get(e["target"], ""), ""), label[e["target"]])
            for e in graph["links"]
            if e["relation"] == "calls"
            and str(e.get("source_file", "")).endswith("locals_new.php")
            and e.get("source_location") == line
            and not e.get("_resolver")
        }

    if engine == "phpstan":
        assert ("Emails", ".get()") not in core_at("L20"), "graphify-php proved Cache::get there"
    else:
        assert ("Emails", ".get()") in core_at("L20"), "tree-sitter refuses L20, core keeps it"
    assert ("Emails", ".reminderSubmitWeek()") in core_at("L26"), "a refused site keeps core"


# --- attribution: the core edges the suppression withheld ---------------------------------------

LOCALS = "/repo/src/locals_new.php"


def _method_graph() -> tuple[list[dict], list[dict]]:
    """Emails and Cache, each with a `get`, and a caller `Factory::reassigned`: the
    `locals_new.php:20` shape, `$mailer` typed Emails by core's table and proved Cache."""

    def code(nid, label):
        return {"id": nid, "label": label, "file_type": "code", "source_file": LOCALS}

    nodes = [
        code("file", "locals_new.php"),
        code("emails", "Emails"),
        code("emails_get", ".get()"),
        code("cache", "Cache"),
        code("cache_get", ".get()"),
        code("factory", "Factory"),
        code("reassigned", ".reassigned()"),
    ]
    edges = [
        {"source": "file", "target": t, "relation": "contains"}
        for t in ("emails", "cache", "factory")
    ] + [
        {"source": "emails", "target": "emails_get", "relation": "method"},
        {"source": "cache", "target": "cache_get", "relation": "method"},
        {"source": "factory", "target": "reassigned", "relation": "method"},
    ]
    return nodes, edges


def _ours_edge(source, target, line, **extra) -> dict:
    return {
        "source": source,
        "target": target,
        "relation": "calls",
        "source_file": LOCALS,
        "source_location": line,
        "_resolver": "graphify-php",
        **extra,
    }


def _member(callee, line, receiver="mailer") -> dict:
    return {
        "caller_nid": "reassigned",
        "callee": callee,
        "receiver": receiver,
        "is_member_call": True,
        "source_file": LOCALS,
        "source_location": line,
    }


def _per_file(*raws) -> list[dict]:
    table = {"path": LOCALS, "table": {"mailer": "Emails", "ghost": "Missing"}}
    return [{"php_type_table": table, "raw_calls": list(raws)}]


def _run_guard(per_file, nodes, edges, proved, core=None) -> dict:
    registry._REGISTRY[:] = [_entry(core or extract._resolve_php_member_calls)]
    core_guard.install(registry)
    core_guard.prove(per_file, proved)
    registry._REGISTRY[0].resolve(per_file, nodes, edges)
    return _record()


def test_the_dry_run_lists_the_exact_core_edge_withheld_at_a_proved_site():
    nodes, edges = _method_graph()
    edges.append(_ours_edge("reassigned", "cache_get", "L20"))
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved])

    assert record["state"] == "completed"
    assert (record["suppressed_edges_listed"], record["suppressed_edges"]) == (1, 1)
    assert record["suppressed_edge_rows"] == [
        {
            "source": "reassigned",
            "target": "emails_get",
            "relation": "calls",
            "reason": core_guard.REASON_CORE_SUPPRESSED,
            "proof": {"source": "reassigned", "target": "cache_get"},
        }
    ]
    assert record["suppressed_unlisted"] == {}
    assert record["dry_run_seconds"] >= 0
    assert not any(e.get("target") == "emails_get" and e["relation"] == "calls" for e in edges)


def test_the_dry_run_leaves_the_builds_edges_and_nodes_untouched():
    nodes, edges = _method_graph()
    edges.append(_ours_edge("reassigned", "cache_get", "L20"))
    # `$ghost` is typed by a class the corpus lacks: core parks a note on the caller node.
    proved, parked = _member("get", "L20"), _member("send", "L22", receiver="ghost")
    edges_obj, edge_ids, edge_copy = edges, [id(e) for e in edges], copy.deepcopy(edges)
    nodes_obj, node_ids, node_copy = nodes, [id(n) for n in nodes], copy.deepcopy(nodes)

    record = _run_guard(_per_file(proved, parked), nodes, edges, [proved, parked])

    assert record["suppressed_edges"] == 1
    assert edges is edges_obj and [id(e) for e in edges] == edge_ids and edges == edge_copy
    assert nodes is nodes_obj and [id(n) for n in nodes] == node_ids and nodes == node_copy
    assert "metadata" not in next(n for n in nodes if n["id"] == "reassigned")


def test_the_parked_note_is_what_the_caller_copy_protects():
    # Pins the reason caller nodes are deep-copied: core does write to them.
    nodes, edges = _method_graph()
    extract._resolve_php_member_calls(
        _per_file(_member("send", "L22", receiver="ghost")), nodes, edges
    )

    assert "metadata" in next(n for n in nodes if n["id"] == "reassigned")


def test_a_pair_already_in_the_graph_is_not_listed_as_withheld():
    nodes, edges = _method_graph()
    edges.append(_ours_edge("reassigned", "cache_get", "L20"))
    edges.append({"source": "reassigned", "target": "emails_get", "relation": "calls"})
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved])

    assert (record["suppressed_edges"], record["suppressed_edge_rows"]) == (0, [])


def test_a_withheld_edge_with_no_proof_edge_is_counted_and_not_listed():
    nodes, edges = _method_graph()
    # Proved (a vendor target with stubs off, say) but no graphify-php edge stands at L20, and
    # one at another line proves nothing about this site.
    edges.append(_ours_edge("reassigned", "cache_get", "L99"))
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved])

    assert record["state"] == "completed"
    assert (record["suppressed_edges_listed"], record["suppressed_edges"]) == (0, 1)
    assert record["suppressed_edge_rows"] == []
    assert record["suppressed_unlisted"] == {core_guard.REASON_NO_PROOF_EDGE: 1}


def test_a_proof_for_another_method_at_the_same_line_proves_nothing():
    nodes, edges = _method_graph()
    edges.append(_ours_edge("reassigned", "reassigned_other", "L20"))
    nodes.append({"id": "reassigned_other", "label": ".send()", "file_type": "code"})
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved])

    assert record["suppressed_edge_rows"] == []
    assert record["suppressed_unlisted"] == {core_guard.REASON_NO_PROOF_EDGE: 1}


def test_a_vendor_stub_edge_proves_by_its_external_method():
    nodes, edges = _method_graph()
    edges.append(
        _ours_edge("reassigned", "stub_cache", "L20", _external_target="Vendor\\Cache::get")
    )
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved])

    assert record["suppressed_edge_rows"][0]["proof"] == {
        "source": "reassigned",
        "target": "stub_cache",
    }


def test_a_dry_run_that_raises_records_a_fixed_reason_and_the_build_goes_on():
    seen: list = []

    def core(per_file, all_nodes, all_edges):
        seen.append(per_file)
        if len(seen) == 2:
            raise RuntimeError("dry run broke")

    nodes, edges = _method_graph()
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved], core=core)

    assert len(seen) == 2
    assert (record["state"], record["reason"]) == ("completed", "")
    assert record["suppressed_edge_rows"] == [] and record["suppressed_edges"] == 0
    assert record["suppressed_unlisted"] == {core_guard.REASON_DRY_RUN_FAILED: 1}


def test_a_dry_run_that_rewrites_its_copy_lists_nothing():
    seen: list = []

    def core(per_file, all_nodes, all_edges):
        seen.append(per_file)
        if len(seen) == 2:
            all_edges.pop(0)
            all_edges.append({"source": "reassigned", "target": "emails_get", "relation": "calls"})

    nodes, edges = _method_graph()
    edges.append(_ours_edge("reassigned", "cache_get", "L20"))
    before = list(edges)
    proved = _member("get", "L20")

    record = _run_guard(_per_file(proved), nodes, edges, [proved], core=core)

    assert edges == before
    assert record["suppressed_edge_rows"] == []
    assert record["suppressed_unlisted"] == {core_guard.REASON_DRY_RUN_TOUCHED_BUILD: 1}


def test_nothing_proved_runs_no_dry_run():
    calls: list = []
    per_file = [{"raw_calls": [_raw("send", 30)]}]

    record = _run_guard(per_file, [], [], [], core=_core(calls))

    assert len(calls) == 1
    assert record["suppressed_edges"] == 0 and record["suppressed_unlisted"] == {}
