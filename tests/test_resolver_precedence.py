"""Source precedence and the status record, with fake sources only.

Fakes rather than the real type sources on purpose: what is under test here is the ordering
contract and the accounting, and a test that needed a parser would fail for parser reasons.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from graphify_php import resolver, status
from graphify_php.ports import CallSite, CallTarget, Confidence, Resolution


class FakeSource:
    """A `CallTargetSource` that resolves exactly the method names it was told to."""

    def __init__(self, name, claims=(), available=(True, ""), unresolved=None, raises=None):
        self.name = name
        # A set of method names, or a map from method name to the `(class, method)` targets a
        # site of that name receives, so one site can answer with the arms of a union.
        self.claims = (
            dict(claims)
            if isinstance(claims, dict)
            else {m: [(f"App\\{m.capitalize()}Service", m)] for m in claims}
        )
        self._available = available
        self._unresolved = dict(unresolved or {})
        self._raises = raises
        self.offered: list[list[CallSite]] = []

    def available(self, repo_root):
        return self._available

    def resolve(self, repo_root, sites):
        sites = list(sites)
        self.offered.append(sites)
        if self._raises:
            raise self._raises
        targets = [
            CallTarget(
                site=s, class_fqn=cls, method=method, confidence=Confidence.EXTRACTED, via=self.name
            )
            for s in sites
            for cls, method in self.claims.get(s.method, ())
        ]
        return Resolution(targets=targets, unresolved=dict(self._unresolved), files_seen=len(sites))


class FakeIndex:
    def __init__(self, known=None):
        self.known = dict(known or {})

    def method_node(self, class_fqn, method):
        return self.known.get((class_fqn, method))


class FakeSink:
    def __init__(self):
        self.calls = []
        self.added = 0

    def add_call(self, caller_nid, callee_nid, target):
        self.calls.append((caller_nid, callee_nid, target))
        self.added += 1


def per_file(*methods):
    return [
        {
            "nodes": [],
            "edges": [],
            "raw_calls": [
                {
                    "caller_nid": f"caller_{m}",
                    "callee": m,
                    "is_member_call": True,
                    "source_file": "src/Service/Thing.php",
                    "source_location": f"L{i + 10}",
                }
                for i, m in enumerate(methods)
            ],
        }
    ]


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "graphify-out" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


def test_first_source_wins_and_its_sites_are_not_offered_again():
    precise = FakeSource("precise", claims={"send"})
    parser = FakeSource("parser", claims={"send", "store"})
    index = FakeIndex(
        {("App\\SendService", "send"): "n_send", ("App\\StoreService", "store"): "n_store"}
    )
    sink = FakeSink()

    record = resolver.run(
        per_file("send", "store"), [], [], sources=[precise, parser], index=index, sink=sink
    )

    assert record.state == "completed"
    assert record.resolved_sites == 2
    assert sink.added == 2
    # `send` was proved by the precise source, so the parser never saw it.
    offered_to_parser = [s.method for s in parser.offered[0]]
    assert offered_to_parser == ["store"]
    assert {c[2].via for c in sink.calls} == {"precise", "parser"}


@pytest.mark.parametrize(
    "arms",
    [
        [("App\\Emails", "get"), ("App\\Cache", "get"), ("App\\Emails", "get")],
        [("App\\Cache", "get"), ("App\\Emails", "get"), ("App\\Cache", "get")],
    ],
)
def test_every_arm_of_a_union_site_becomes_an_edge_and_the_site_stays_claimed(arms):
    precise = FakeSource("precise", claims={"get": arms})
    parser = FakeSource("parser", claims={"get", "store"})
    index = FakeIndex(
        {
            ("App\\Emails", "get"): "n_emails_get",
            ("App\\Cache", "get"): "n_cache_get",
            ("App\\StoreService", "store"): "n_store",
        }
    )
    sink = FakeSink()

    record = resolver.run(
        per_file("get", "store"), [], [], sources=[precise, parser], index=index, sink=sink
    )

    union_edges = {c[1] for c in sink.calls if c[0] == "caller_get"}
    assert union_edges == {"n_emails_get", "n_cache_get"}
    assert sink.added == 3
    assert record.resolved_sites == 2
    assert [s.method for s in parser.offered[0]] == ["store"]
    assert "multiple_targets_for_site" not in record.unresolved
    assert not hasattr(resolver, "REASON_DUPLICATE_TARGET")


class FakeScopes:
    """A `ScopeIndex` that declares the given methods for every scope it is asked about."""

    def __init__(self, methods):
        self.methods = list(methods)

    def scope_methods(self, caller_nid, scope):
        return self.methods


def test_a_scoped_candidate_with_two_targets_is_ambiguous_and_emits_nothing():
    source = FakeSource("precise", claims={"format": [("App\\A", "format"), ("App\\B", "format")]})
    sink = FakeSink()
    scoped = [
        {
            "nodes": [],
            "edges": [],
            "raw_calls": [
                {
                    "caller_nid": "caller_x",
                    "callee": "Helper",
                    "is_member_call": False,
                    "source_file": "src/Service/Thing.php",
                    "source_location": "L10",
                },
            ],
        }
    ]

    record = resolver.run(
        scoped,
        [],
        [],
        sources=[source],
        index=FakeIndex({("App\\A", "format"): "n_a", ("App\\B", "format"): "n_b"}),
        sink=sink,
        scopes=FakeScopes(["format"]),
    )

    assert sink.calls == []
    assert record.eligible_sites == 1
    assert record.unresolved[resolver.REASON_SCOPE_AMBIGUOUS] == 1


def test_second_source_is_skipped_entirely_when_the_first_resolves_everything():
    precise = FakeSource("precise", claims={"send"})
    parser = FakeSource("parser", claims={"send"})

    resolver.run(
        per_file("send"),
        [],
        [],
        sources=[precise, parser],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=FakeSink(),
    )

    assert parser.offered == []


def test_unavailable_source_is_recorded_and_the_next_one_still_runs():
    absent = FakeSource("phpstan", available=(False, "vendor/bin/phpstan not found"))
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(
        per_file("send"),
        [],
        [],
        sources=[absent, parser],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=sink,
    )

    assert record.state == "completed"
    assert "vendor/bin/phpstan not found" in record.reason
    assert sink.added == 1


def test_no_available_source_is_a_skip_not_a_failure():
    absent = FakeSource("phpstan", available=(False, "vendor/bin/phpstan not found"))

    record = resolver.run(
        per_file("send"), [], [], sources=[absent], index=FakeIndex(), sink=FakeSink()
    )

    assert record.state == "skipped"
    assert record.reason == "phpstan: vendor/bin/phpstan not found"


def test_target_whose_method_is_not_in_the_index_adds_no_edge():
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(per_file("send"), [], [], sources=[parser], index=FakeIndex(), sink=sink)

    assert sink.calls == []
    assert record.edges_added == 0
    assert record.resolved_sites == 1
    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1


def test_self_call_adds_no_edge():
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(
        per_file("send"),
        [],
        [],
        sources=[parser],
        index=FakeIndex({("App\\SendService", "send"): "caller_send"}),
        sink=sink,
    )

    assert sink.calls == []
    assert record.unresolved[resolver.REASON_SELF_CALL] == 1


def test_unresolved_reasons_are_namespaced_by_source():
    parser = FakeSource("parser", claims=(), unresolved={"chained_call": 3})

    record = resolver.run(
        per_file("send"), [], [], sources=[parser], index=FakeIndex(), sink=FakeSink()
    )

    assert record.unresolved["parser:chained_call"] == 3
    assert record.unresolved[resolver.REASON_NO_SOURCE] == 1


def test_clean_run_is_written_to_the_status_file(status_file):
    parser = FakeSource("parser", claims={"send"})

    resolver.run(
        per_file("send"),
        [],
        [],
        sources=[parser],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=FakeSink(),
    )

    written = status.read()["resolvers"]
    assert [r["name"] for r in written] == [resolver.RESOLVER_NAME]
    assert written[0]["state"] == "completed"
    assert written[0]["eligible_sites"] == 1
    assert written[0]["edges_added"] == 1
    assert written[0]["finished_at"] > 0


def test_skip_is_written_to_the_status_file():
    absent = FakeSource("phpstan", available=(False, "no phpstan"))

    resolver.run(per_file("send"), [], [], sources=[absent], index=FakeIndex(), sink=FakeSink())

    assert status.read()["resolvers"][0]["state"] == "skipped"


def test_failure_is_recorded_before_the_exception_propagates():
    boom = FakeSource("parser", raises=RuntimeError("tree-sitter blew up"))

    with pytest.raises(RuntimeError, match="tree-sitter blew up"):
        resolver.run(per_file("send"), [], [], sources=[boom], index=FakeIndex(), sink=FakeSink())

    # graphify's driver swallows the exception. Without this record on disk the build would
    # publish a short graph and nothing would say why.
    written = status.read()["resolvers"][0]
    assert written["state"] == "failed"
    assert "RuntimeError: tree-sitter blew up" == written["reason"]


def _factory_that_raises(repo_root):
    raise RuntimeError("factory blew up")


@pytest.mark.parametrize(
    "make, name",
    [
        (resolver.make_resolver, resolver.RESOLVER_NAME),
        (resolver.make_route_resolver, resolver.RESOLVER_NAME_ROUTES),
    ],
)
def test_a_raising_sources_factory_is_recorded_as_a_failure(make, name):
    """The factory runs inside `run`, so its failure is a `failed` record (gate exit 2) rather
    than silence (gate exit 3, "never reported") that hides which layer broke."""
    resolve = make(_factory_that_raises, ".")

    with pytest.raises(RuntimeError, match="factory blew up"):
        resolve(per_file("send"), [], [])

    written = status.read()["resolvers"][0]
    assert written["name"] == name
    assert written["state"] == "failed"
    assert "factory blew up" in written["reason"]


def test_only_php_member_calls_are_eligible():
    sites = resolver.collect_sites(
        [
            {
                "raw_calls": [
                    {
                        "caller_nid": "a",
                        "callee": "send",
                        "is_member_call": True,
                        "source_file": "src/A.php",
                        "source_location": "L1",
                    },
                    {
                        "caller_nid": "b",
                        "callee": "send",
                        "is_member_call": False,
                        "source_file": "src/A.php",
                        "source_location": "L2",
                    },
                    {
                        "caller_nid": "c",
                        "callee": "send",
                        "is_member_call": True,
                        "source_file": "src/a.ts",
                        "source_location": "L3",
                    },
                    {
                        "callee": "send",
                        "is_member_call": True,
                        "source_file": "src/A.php",
                        "source_location": "L4",
                    },
                ],
            }
        ]
    )

    assert [s.caller_nid for s in sites] == ["a"]
    # graphify's PHP extractor never sets `receiver`; the field is carried as empty, not faked.
    assert sites[0].receiver == ""


class CorpusSource(FakeSource):
    """A source written against the current port, which takes `corpus`."""

    def resolve(self, repo_root, sites, corpus=None):
        self.corpus = list(corpus) if corpus is not None else None
        return super().resolve(repo_root, sites)


def per_file_with_leaf():
    """Two files: one carrying a call site, one a leaf class carrying only declarations."""
    caller, leaf = "src/Controller/C.php", "src/Service/Leaf.php"
    return [
        {
            "nodes": [{"source_file": caller}],
            "raw_calls": [
                {
                    "caller_nid": "caller_send",
                    "callee": "send",
                    "is_member_call": True,
                    "source_file": caller,
                    "source_location": "L10",
                }
            ],
        },
        {"nodes": [{"source_file": leaf}], "raw_calls": []},
    ]


def test_corpus_is_every_php_file_graphify_parsed_not_just_the_ones_with_sites():
    corpus = resolver.collect_corpus(per_file_with_leaf())

    # The leaf file holds no call site, so it never reaches a source through `sites`. It is
    # also the file declaring the method being called, which is the whole reason for `corpus`.
    assert corpus == ["src/Controller/C.php", "src/Service/Leaf.php"]


def test_corpus_excludes_non_php_files():
    corpus = resolver.collect_corpus(
        [{"nodes": [{"source_file": "app/main.ts"}, {"source_file": "src/A.php"}], "raw_calls": []}]
    )

    assert corpus == ["src/A.php"]


def test_corpus_paths_are_verbatim_so_a_file_is_never_parsed_under_two_names():
    absolute = "/build/repo/src/A.php"
    corpus = resolver.collect_corpus(
        [
            {
                "nodes": [{"source_file": absolute}],
                "raw_calls": [
                    {
                        "caller_nid": "a",
                        "callee": "send",
                        "is_member_call": True,
                        "source_file": absolute,
                        "source_location": "L1",
                    }
                ],
            }
        ]
    )
    sites = resolver.collect_sites(
        [
            {
                "raw_calls": [
                    {
                        "caller_nid": "a",
                        "callee": "send",
                        "is_member_call": True,
                        "source_file": absolute,
                        "source_location": "L1",
                    }
                ],
            }
        ]
    )

    assert corpus == [absolute] == [sites[0].source_file]


def test_a_source_that_takes_corpus_is_given_it():
    source = CorpusSource("parser", claims={"send"})

    resolver.run(
        per_file_with_leaf(),
        [],
        [],
        sources=[source],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=FakeSink(),
    )

    assert source.corpus == ["src/Controller/C.php", "src/Service/Leaf.php"]


def test_a_source_predating_the_parameter_is_called_without_it():
    # `FakeSource.resolve` has the two-argument signature. Passing a corpus to it would raise
    # TypeError and, because graphify swallows what escapes, lose the whole pass silently.
    old = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(
        per_file_with_leaf(),
        [],
        [],
        sources=[old],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=sink,
    )

    assert record.state == "completed"
    assert sink.added == 1


def test_mixed_vintage_sources_both_run():
    old = FakeSource("phpstan", claims=())
    new = CorpusSource("parser", claims={"send"})
    sink = FakeSink()

    resolver.run(
        per_file_with_leaf(),
        [],
        [],
        sources=[old, new],
        index=FakeIndex({("App\\SendService", "send"): "n_send"}),
        sink=sink,
    )

    assert new.corpus is not None
    assert sink.added == 1


def test_files_seen_counts_the_corpus_not_only_the_files_with_sites():
    record = resolver.run(
        per_file_with_leaf(),
        [],
        [],
        sources=[CorpusSource("parser")],
        index=FakeIndex(),
        sink=FakeSink(),
    )

    assert record.files_seen == 2


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/repo/vendor/acme/Lib.php", True),
        ("vendor/acme/Lib.php", True),
        ("/repo/src/Service/Thing.php", False),
        ("/repo/tests/Foo.php", False),
        ("/repo/src/vendor/Thing.php", False),
        ("/elsewhere/vendor/acme/Lib.php", False),
        ("", False),
    ],
)
def test_is_vendor_file_means_first_segment_under_the_root_is_vendor(path, expected):
    assert resolver.is_vendor_file(path, "/repo") is expected


def test_is_vendor_file_resolves_a_relative_root_against_the_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert resolver.is_vendor_file(str(tmp_path / "vendor/acme/Lib.php"), ".") is True
    assert resolver.is_vendor_file(str(tmp_path / "src/Thing.php"), ".") is False


class FileAwareSource(FakeSource):
    """A fake that also says where the declaring class lives, as the PHPStan source does."""

    def __init__(self, name, claims, declaring_file):
        super().__init__(name, claims)
        self.declaring_file = declaring_file

    def resolve(self, repo_root, sites):
        resolution = super().resolve(repo_root, sites)
        resolution.targets = [
            dataclasses.replace(t, declaring_file=self.declaring_file) for t in resolution.targets
        ]
        return resolution


def _vendor_run(monkeypatch, flag, declaring_file):
    monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, flag)
    source = FileAwareSource(
        "phpstan", {"flush": [("Doctrine\\ORM\\EntityManager", "flush")]}, declaring_file
    )
    nodes, edges, sink = [], [], FakeSink()
    record = resolver.run(
        per_file("flush"),
        nodes,
        edges,
        sources=[source],
        repo_root="/repo",
        index=FakeIndex(),
        sink=sink,
    )
    return record, nodes, edges, sink


def test_without_the_flag_a_vendor_target_stays_not_in_graph(monkeypatch):
    record, nodes, edges, sink = _vendor_run(
        monkeypatch, "0", "/repo/vendor/doctrine/EntityManager.php"
    )

    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1
    assert record.external_stubbed == 0
    assert record.stub_nodes_added == 0
    assert nodes == [] and edges == [] and sink.calls == []


def test_with_the_flag_a_vendor_target_gets_a_stub_and_an_edge(monkeypatch, status_file):
    record, nodes, edges, sink = _vendor_run(
        monkeypatch, "1", "/repo/vendor/doctrine/EntityManager.php"
    )

    assert resolver.REASON_NOT_IN_GRAPH not in record.unresolved
    assert record.external_stubbed == 1
    assert record.stub_nodes_added == 1
    assert record.stub_edges_added == 1
    assert [n["id"] for n in nodes] == ["php_external_doctrine_orm_entitymanager"]
    assert nodes[0]["external"] is True
    assert [(e["source"], e["target"], e["_external_target"]) for e in edges] == [
        (
            "caller_flush",
            "php_external_doctrine_orm_entitymanager",
            "Doctrine\\ORM\\EntityManager::flush",
        )
    ]
    assert sink.calls == [], "a stub edge never goes through the method-level sink"
    written = {r["name"]: r for r in json.loads(status_file.read_text())["resolvers"]}
    assert written[resolver.RESOLVER_NAME]["external_stubbed"] == 1
    assert written[resolver.RESOLVER_NAME]["stub_nodes_added"] == 1
    assert written[resolver.RESOLVER_NAME]["stub_edges_added"] == 1


def test_with_the_flag_a_project_file_outside_vendor_gets_no_stub(monkeypatch):
    record, nodes, edges, sink = _vendor_run(monkeypatch, "1", "/repo/tests/Foo.php")

    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1
    assert record.external_stubbed == 0
    assert nodes == [] and edges == []


def test_with_the_flag_a_target_with_no_declaring_file_gets_no_stub(monkeypatch):
    record, nodes, edges, sink = _vendor_run(monkeypatch, "1", "")

    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1
    assert nodes == [] and edges == []


def _new_bag_call():
    # graphify 0.9.103 records `new Bag(...)` as a non-member call with `Bag` as the callee.
    return {
        "caller_nid": "caller_wrap",
        "callee": "Bag",
        "is_member_call": False,
        "source_file": "src/Service.php",
        "source_location": "L10",
    }


def test_scoped_candidates_are_marked_so_a_source_can_tell_them_from_member_calls():
    files = per_file("send")
    files[0]["raw_calls"].append(_new_bag_call())

    groups = resolver.collect_scoped_groups(files, FakeScopes(["map", "__construct"]))

    assert [(c.method, c.scoped) for g in groups for c in g.candidates] == [
        ("map", True),
        ("__construct", True),
    ]
    assert [s.scoped for s in resolver.collect_sites(files)] == [False]


def test_a_member_call_graphify_flags_php_scoped_is_marked_scoped():
    """0.9.103 sends `Foo::get()` as a member call carrying `php_scoped: true`."""
    files = per_file("send", "get")
    files[0]["raw_calls"][1].update(receiver="Foo", php_scoped=True)

    assert [(s.method, s.scoped) for s in resolver.collect_sites(files)] == [
        ("send", False),
        ("get", True),
    ]


class CallerAwareIndex(FakeIndex):
    def caller_scope(self, caller_nid):
        return (f"Thing::{caller_nid.removeprefix('caller_')}",)


def test_sites_carry_the_callers_class_method_when_the_index_can_name_it():
    source = FakeSource("precise", claims={"send"})

    resolver.run(
        per_file("send"), [], [], sources=[source], index=CallerAwareIndex(), sink=FakeSink()
    )

    assert [s.caller_scope for s in source.offered[0]] == [("Thing::send",)]


def test_sites_say_nothing_about_the_caller_when_the_index_cannot():
    source = FakeSource("precise", claims={"send"})

    resolver.run(per_file("send"), [], [], sources=[source], index=FakeIndex(), sink=FakeSink())

    assert [s.caller_scope for s in source.offered[0]] == [None]


class UnsentSource(FakeSource):
    """Places one `App\\User::getName` record on every anchor that owns its line."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked: list[tuple[list[CallSite], list[CallSite]]] = []

    def unsent_calls(self, repo_root, anchors, members):
        anchors, members = list(anchors), list(members)
        self.asked.append((anchors, members))
        return [
            CallTarget(
                site=a,
                class_fqn="App\\User",
                method="getName",
                confidence=Confidence.EXTRACTED,
                via=self.name,
            )
            for a in anchors
            if a.owns_line
        ]


class LineIndex(CallerAwareIndex):
    def caller_owns_line(self, caller_nid, location):
        return caller_nid == "caller_wrap" and location == "L10"


def test_a_refused_probe_places_records_no_raw_call_carried_and_counts_them_apart(status_file):
    files = per_file("getId")
    files[0]["raw_calls"].append(_new_bag_call())
    source = UnsentSource("precise", claims={"getId"})
    sink = FakeSink()
    index = LineIndex(
        {("App\\User", "getName"): "user_getname", ("App\\GetidService", "getId"): "x"}
    )

    record = resolver.run(
        files, [], [], sources=[source], index=index, sink=sink, scopes=FakeScopes(["map", "go"])
    )

    anchors, members = source.asked[0]
    assert [(a.caller_nid, a.source_location, a.owns_line) for a in anchors] == [
        ("caller_wrap", "L10", True)
    ]
    assert [m.method for m in members] == ["getId"]
    assert [(c, n) for c, n, _t in sink.calls] == [
        ("caller_getId", "x"),
        ("caller_wrap", "user_getname"),
    ]
    # The probe is still refused as a site; the placed record is counted on its own line.
    assert record.resolved_sites == 1
    assert record.unresolved[resolver.REASON_SCOPE_NOT_RECOVERED] == 1
    assert record.unsent_calls_added == 1
    assert json.loads(status_file.read_text())["resolvers"][0]["unsent_calls_added"] == 1


def test_a_scoped_group_that_resolved_still_anchors_its_line():
    # `Foo::make($this->bar->make())`: core bound `->make()` by bare name and never sent it.
    files = per_file()
    files[0]["raw_calls"].append(_new_bag_call())
    source = UnsentSource("precise", claims={"map"})
    scopes = FakeScopes(["map"])
    resolver.run(files, [], [], sources=[source], index=LineIndex(), sink=FakeSink(), scopes=scopes)

    assert [(a.caller_nid, a.method) for a in source.asked[0][0]] == [("caller_wrap", "map")]


def test_a_scoped_member_site_anchors_its_line_and_claims_no_record():
    """0.9.103 sends `Foo::make(...)` as a member call flagged `php_scoped`."""
    files = per_file("getId", "make")
    files[0]["raw_calls"][1].update(receiver="Foo", php_scoped=True, caller_nid="caller_wrap")
    source = UnsentSource("precise", claims={"getId"})

    resolver.run(files, [], [], sources=[source], index=LineIndex(), sink=FakeSink())

    anchors, members = source.asked[0]
    assert [(a.caller_nid, a.method, a.scoped) for a in anchors] == [("caller_wrap", "make", True)]
    assert [m.method for m in members] == ["getId"]


def _graph_with_a_local_cache(root: str):
    """`Thing::run` calls `get`; its own file also declares a project class `Cache`."""
    path = f"{root}/src/Thing.php"
    nodes = [
        {"id": "f", "label": "Thing.php", "source_file": path},
        {"id": "thing", "label": "Thing", "_callable_class": True, "source_file": path},
        {"id": "cache", "label": "Cache", "_callable_class": True, "source_file": path},
        {"id": "run", "label": ".run()", "_callable": True, "source_file": path},
        {"id": "cache_get", "label": ".get()", "_callable": True, "source_file": path},
    ]
    edges = [
        {"source": "f", "target": "thing", "relation": "contains"},
        {"source": "f", "target": "cache", "relation": "contains"},
        {"source": "thing", "target": "run", "relation": "method"},
        {"source": "cache", "target": "cache_get", "relation": "method"},
    ]
    raw = {
        "caller_nid": "run",
        "callee": "get",
        "is_member_call": True,
        "source_file": path,
        "source_location": "L9",
    }
    return nodes, edges, [{"nodes": [], "edges": [], "raw_calls": [raw]}]


@pytest.mark.parametrize("stubs", ["0", "1"])
def test_a_proved_vendor_target_never_binds_to_a_same_named_class_in_the_callers_file(
    tmp_path, monkeypatch, stubs
):
    monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, stubs)
    root = str(tmp_path)
    nodes, edges, files = _graph_with_a_local_cache(root)
    source = FileAwareSource(
        "phpstan", {"get": [("Vendor\\Lib\\Cache", "get")]}, f"{root}/vendor/lib/Cache.php"
    )

    record = resolver.run(files, nodes, edges, sources=[source], repo_root=root)

    calls = [(e["source"], e["target"]) for e in edges if e.get("relation") == "calls"]
    assert ("run", "cache_get") not in calls
    if stubs == "1":
        assert calls == [("run", "php_external_vendor_lib_cache")]
        assert record.external_stubbed == 1
    else:
        assert calls == []
        assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1
    assert record.resolved_sites == 1


class NullsafeSource(FakeSource):
    """Finds the given parser sites and places `App\\User::getName` on each it is handed."""

    def __init__(self, name, found, take=lambda site: True):
        super().__init__(name)
        self.found = list(found)
        self.take = take
        self.handed: list[CallSite] = []

    def nullsafe_calls(self, repo_root, files):
        return list(self.found)

    def unsent_calls(self, repo_root, anchors, members, *, nullsafe=()):
        nullsafe = list(nullsafe)
        self.handed.extend(nullsafe)
        return [
            CallTarget(
                site=s,
                class_fqn="App\\User",
                method=s.method,
                confidence=Confidence.EXTRACTED,
                via=self.name,
            )
            for s in nullsafe
            if self.take(s)
        ]


class CallableIndex(CallerAwareIndex):
    """Callers by start line: `caller_run` starts on L20, nothing else is known."""

    def callable_at(self, source_file, lines, repo_root="."):
        return "caller_run" if 20 in lines else None


def test_a_nullsafe_call_reaches_the_graph_from_the_caller_the_parser_placed(status_file):
    from graphify_php.ports import NullsafeCall

    found = [
        NullsafeCall("src/Service/Thing.php", 22, "getName", (20,)),
        NullsafeCall("src/Service/Thing.php", 23, "getName", (20,)),
        NullsafeCall("src/Service/Thing.php", 24, "getMail", (20,)),
        NullsafeCall("src/Service/Thing.php", 30, "getName", (29,)),
        NullsafeCall("src/Service/Thing.php", 31, "getName", (), "other-call-on-line"),
    ]
    source = NullsafeSource("phpstan", found, take=lambda s: s.source_location != "L24")
    sink = FakeSink()
    index = CallableIndex({("App\\User", "getName"): "user_getname"})

    record = resolver.run(per_file(), [], [], sources=[source], index=index, sink=sink)

    assert [(s.caller_nid, s.source_location, s.caller_scope) for s in source.handed] == [
        ("caller_run", "L22", ("Thing::run",)),
        ("caller_run", "L23", ("Thing::run",)),
        ("caller_run", "L24", ("Thing::run",)),
    ]
    assert [(c, n, t.site.source_location) for c, n, t in sink.calls] == [
        ("caller_run", "user_getname", "L22"),
        ("caller_run", "user_getname", "L23"),
    ]
    assert record.nullsafe_sites_total == 5
    assert record.nullsafe_calls_added == 2
    assert record.unresolved == {
        "nullsafe:phpstan:other-call-on-line": 1,
        "nullsafe:" + resolver.REASON_NULLSAFE_CALLER_NOT_IN_GRAPH: 1,
        "nullsafe:" + resolver.REASON_NULLSAFE_NO_RECORD: 1,
    }
    # Nothing here stands for a raw call, so no site was eligible or resolved.
    assert (record.eligible_sites, record.resolved_sites, record.unsent_calls_added) == (0, 0, 0)
    written = json.loads(status_file.read_text())["resolvers"][0]
    assert (written["nullsafe_sites_total"], written["nullsafe_calls_added"]) == (5, 2)


def test_a_nullsafe_target_missing_from_the_graph_is_counted_under_the_nullsafe_prefix():
    from graphify_php.ports import NullsafeCall

    source = NullsafeSource("phpstan", [NullsafeCall("src/Thing.php", 22, "getName", (20,))])

    record = resolver.run(
        per_file(), [], [], sources=[source], index=CallableIndex(), sink=FakeSink()
    )

    assert record.nullsafe_calls_added == 0
    assert record.unresolved == {"nullsafe:" + resolver.REASON_NOT_IN_GRAPH: 1}


def test_without_a_parser_or_a_caller_lookup_no_nullsafe_site_is_counted():
    from graphify_php.ports import NullsafeCall

    source = NullsafeSource("phpstan", [NullsafeCall("src/Thing.php", 22, "getName", (20,))])

    record = resolver.run(per_file(), [], [], sources=[source], index=FakeIndex(), sink=FakeSink())

    assert (record.nullsafe_sites_total, record.nullsafe_calls_added) == (0, 0)
    assert source.handed == []
