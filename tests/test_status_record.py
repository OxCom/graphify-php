"""The member-call record's schema lives in `status.py`, and the record states its own decisions.

`external_stubs` is decided inside the resolver's process; the build wrapper reads it from the
record, which is the only place both processes share.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from graphify_php import resolver, shrink, status
from graphify_php.ports import Resolution


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "graphify-out" / ".graphify-php-status.json"
    monkeypatch.setenv(status.STATUS_PATH_ENV, str(path))
    return path


class NoSource:
    name = "none"

    def available(self, repo_root):
        return True, ""

    def resolve(self, repo_root, sites):
        return Resolution(targets=[], unresolved={})


def member_record(status_file) -> dict:
    rows = json.loads(status_file.read_text())["resolvers"]
    return next(r for r in rows if r["name"] == resolver.RESOLVER_NAME)


def test_the_member_call_record_is_defined_in_status():
    names = {f.name for f in dataclasses.fields(status.MemberCallStatus)}

    assert issubclass(status.MemberCallStatus, status.ResolverStatus)
    assert {"nullsafe_sites_total", "nullsafe_calls_added", "external_stubs"} <= names
    assert resolver.MemberCallStatus is status.MemberCallStatus


def test_the_stub_vocabulary_is_one_pair_of_constants():
    assert (shrink.STUBS_ON, shrink.STUBS_OFF) == (status.STUBS_ON, status.STUBS_OFF)


@pytest.mark.parametrize(("flag", "expected"), [("1", "on"), ("0", "off"), (None, "off")])
def test_the_record_states_the_stub_decision(monkeypatch, status_file, flag, expected):
    if flag is None:
        monkeypatch.delenv(resolver.EXTERNAL_STUBS_ENV, raising=False)
    else:
        monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, flag)

    record = resolver.run([], [], [], sources=[NoSource()], repo_root="/repo", edges_owned=False)

    assert record.external_stubs == expected
    assert member_record(status_file)["external_stubs"] == expected


class Stubs:
    nodes_added = edges_added = 0


def test_a_stub_sink_handed_in_is_stubs_on(monkeypatch, status_file):
    monkeypatch.delenv(resolver.EXTERNAL_STUBS_ENV, raising=False)

    record = resolver.run(
        [], [], [], sources=[NoSource()], repo_root="/repo", stubs=Stubs(), edges_owned=False
    )

    assert record.external_stubs == status.STUBS_ON


def test_a_skipped_pass_still_states_the_decision(monkeypatch, status_file):
    class Absent(NoSource):
        def available(self, repo_root):
            return False, "missing"

    monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, "1")

    record = resolver.run([], [], [], sources=[Absent()], repo_root="/repo", edges_owned=False)

    assert (record.state, record.external_stubs) == ("skipped", status.STUBS_ON)
