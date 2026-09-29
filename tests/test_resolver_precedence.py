"""Source precedence and the status record, with fake sources only.

Fakes rather than the real type sources on purpose: what is under test here is the ordering
contract and the accounting, and a test that needed a parser would fail for parser reasons.
"""

from __future__ import annotations

import pytest

from graphify_php import resolver, status
from graphify_php.ports import CallSite, CallTarget, Confidence, Resolution


class FakeSource:
    """A `CallTargetSource` that resolves exactly the method names it was told to."""

    def __init__(self, name, claims=(), available=(True, ""), unresolved=None, raises=None):
        self.name = name
        self.claims = set(claims)
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
            CallTarget(site=s, class_fqn=f"App\\{s.method.capitalize()}Service",
                       method=s.method, confidence=Confidence.EXTRACTED, via=self.name)
            for s in sites if s.method in self.claims
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
    return [{
        "nodes": [],
        "edges": [],
        "raw_calls": [
            {"caller_nid": f"caller_{m}", "callee": m, "is_member_call": True,
             "source_file": "src/Service/Thing.php", "source_location": f"L{i + 10}"}
            for i, m in enumerate(methods)
        ],
    }]


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "graphify-out" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


def test_first_source_wins_and_its_sites_are_not_offered_again():
    precise = FakeSource("precise", claims={"send"})
    parser = FakeSource("parser", claims={"send", "store"})
    index = FakeIndex({("App\\SendService", "send"): "n_send",
                       ("App\\StoreService", "store"): "n_store"})
    sink = FakeSink()

    record = resolver.run(per_file("send", "store"), [], [],
                          sources=[precise, parser], index=index, sink=sink)

    assert record.state == "completed"
    assert record.resolved_sites == 2
    assert sink.added == 2
    # `send` was proved by the precise source, so the parser never saw it.
    offered_to_parser = [s.method for s in parser.offered[0]]
    assert offered_to_parser == ["store"]
    assert {c[2].via for c in sink.calls} == {"precise", "parser"}


def test_second_source_is_skipped_entirely_when_the_first_resolves_everything():
    precise = FakeSource("precise", claims={"send"})
    parser = FakeSource("parser", claims={"send"})

    resolver.run(per_file("send"), [], [],
                 sources=[precise, parser],
                 index=FakeIndex({("App\\SendService", "send"): "n_send"}), sink=FakeSink())

    assert parser.offered == []


def test_unavailable_source_is_recorded_and_the_next_one_still_runs():
    absent = FakeSource("phpstan", available=(False, "vendor/bin/phpstan not found"))
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(per_file("send"), [], [], sources=[absent, parser],
                          index=FakeIndex({("App\\SendService", "send"): "n_send"}), sink=sink)

    assert record.state == "completed"
    assert "vendor/bin/phpstan not found" in record.reason
    assert sink.added == 1


def test_no_available_source_is_a_skip_not_a_failure():
    absent = FakeSource("phpstan", available=(False, "vendor/bin/phpstan not found"))

    record = resolver.run(per_file("send"), [], [], sources=[absent],
                          index=FakeIndex(), sink=FakeSink())

    assert record.state == "skipped"
    assert record.reason == "phpstan: vendor/bin/phpstan not found"


def test_target_whose_method_is_not_in_the_index_adds_no_edge():
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(per_file("send"), [], [], sources=[parser],
                          index=FakeIndex(), sink=sink)

    assert sink.calls == []
    assert record.edges_added == 0
    assert record.resolved_sites == 1
    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1


def test_self_call_adds_no_edge():
    parser = FakeSource("parser", claims={"send"})
    sink = FakeSink()

    record = resolver.run(per_file("send"), [], [], sources=[parser],
                          index=FakeIndex({("App\\SendService", "send"): "caller_send"}),
                          sink=sink)

    assert sink.calls == []
    assert record.unresolved[resolver.REASON_SELF_CALL] == 1


def test_unresolved_reasons_are_namespaced_by_source():
    parser = FakeSource("parser", claims=(), unresolved={"chained_call": 3})

    record = resolver.run(per_file("send"), [], [], sources=[parser],
                          index=FakeIndex(), sink=FakeSink())

    assert record.unresolved["parser:chained_call"] == 3
    assert record.unresolved[resolver.REASON_NO_SOURCE] == 1


def test_clean_run_is_written_to_the_status_file(status_file):
    parser = FakeSource("parser", claims={"send"})

    resolver.run(per_file("send"), [], [], sources=[parser],
                 index=FakeIndex({("App\\SendService", "send"): "n_send"}), sink=FakeSink())

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
        resolver.run(per_file("send"), [], [], sources=[boom],
                     index=FakeIndex(), sink=FakeSink())

    # graphify's driver swallows the exception. Without this record on disk the build would
    # publish a short graph and nothing would say why.
    written = status.read()["resolvers"][0]
    assert written["state"] == "failed"
    assert "RuntimeError: tree-sitter blew up" == written["reason"]


def test_only_php_member_calls_are_eligible():
    sites = resolver.collect_sites([{
        "raw_calls": [
            {"caller_nid": "a", "callee": "send", "is_member_call": True,
             "source_file": "src/A.php", "source_location": "L1"},
            {"caller_nid": "b", "callee": "send", "is_member_call": False,
             "source_file": "src/A.php", "source_location": "L2"},
            {"caller_nid": "c", "callee": "send", "is_member_call": True,
             "source_file": "src/a.ts", "source_location": "L3"},
            {"callee": "send", "is_member_call": True,
             "source_file": "src/A.php", "source_location": "L4"},
        ],
    }])

    assert [s.caller_nid for s in sites] == ["a"]
    # graphify's PHP extractor never sets `receiver`; the field is carried as empty, not faked.
    assert sites[0].receiver == ""
