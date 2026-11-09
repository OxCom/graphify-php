"""Unit tests for the PHPStan source.

None of these run PHPStan. The point of the source is what it does when PHPStan is absent,
slow or broken, and a test that needs a working `vendor/` to exercise the absent case would
never run on the nine of twenty repositories that have no PHPStan at all.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from graphify_php.ports import CallSite, Confidence
from graphify_php.sources import phpstan_source as ps

WIRING = """
services:
	-
		class: CallGraph\\JsonLinesWriter
	-
		class: CallGraph\\MethodCallCollector
		tags:
			- phpstan.collector
	-
		class: CallGraph\\CallEdgeRule
		tags:
			- phpstan.rules.rule
"""


def make_repo(
    tmp_path: Path, *, binary: bool = True, config: bool = True, wired: bool = True
) -> Path:
    root = tmp_path / "repo"
    (root / "vendor" / "bin").mkdir(parents=True)
    if binary:
        (root / "vendor" / "bin" / "phpstan").write_text("#!/bin/sh\nexit 0\n")
    if config:
        text = "parameters:\n\tlevel: 5\n"
        if wired:
            text += WIRING
        (root / "phpstan.neon").write_text(text)
    return root


def site(line: str = "16", method: str = "send", file: str = "src/App.php") -> CallSite:
    return CallSite(
        caller_nid="n1",
        receiver="$this->mailer",
        method=method,
        source_file=file,
        source_location=line,
    )


def record(**over) -> dict:
    base = {
        "callerClass": "App\\Controller",
        "callerMethod": "run",
        "calleeClass": "App\\Emails",
        "calleeMethod": "send",
        "calleeKind": "class",
        "line": 16,
        "file": "/repo/src/App.php",
    }
    base.update(over)
    return base


class TestAvailability:
    def test_available_when_binary_and_config_present(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path)))
        assert ok is True
        assert reason == ""

    def test_missing_binary_is_a_reason_not_an_exception(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path, binary=False)))
        assert ok is False
        assert "vendor/bin/phpstan" in reason

    def test_missing_config_is_a_reason(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path, config=False)))
        assert ok is False
        assert "phpstan.neon" in reason

    def test_missing_php_is_a_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps.shutil, "which", lambda _name: None)
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path)))
        assert ok is False
        assert "php executable" in reason

    def test_nonexistent_repo_is_a_reason(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(tmp_path / "nothing-here"))
        assert ok is False
        assert reason != ""

    @pytest.mark.parametrize("name", ["phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon"])
    def test_every_config_name_phpstan_itself_accepts(self, tmp_path, name):
        root = make_repo(tmp_path, config=False)
        (root / name).write_text("parameters:\n" + WIRING)
        assert ps.EnvironmentProbe().config_path(str(root)).name == name


class TestWiring:
    """Where the collector registration comes from.

    PHPStan builds its rules from the config alone, and the `CallGraph` classes reach it only
    through our `--autoload-file` — so the block cannot live in the project's committed config:
    a plain `vendor/bin/phpstan` would die on "Class 'CallGraph\\JsonLinesWriter' not found"
    before analysing anything. The source therefore supplies the registration itself, through a
    wrapper config that `includes:` the project's own.
    """

    def test_unwired_project_is_still_available(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path, wired=False)))
        assert (ok, reason) == (True, "")

    def test_unwired_project_gets_a_generated_wrapper(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        workdir = tmp_path / "work"
        workdir.mkdir()
        config = ps.PhpStanRunner().config_for(str(root), workdir)
        assert config.parent == workdir
        text = config.read_text()
        assert str((root / "phpstan.neon").resolve()) in text
        assert "CallGraph\\MethodCallCollector" in text
        assert "CallGraph\\CallEdgeRule" in text

    def test_wrapper_never_lands_in_the_repository(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        workdir = tmp_path / "work"
        workdir.mkdir()
        ps.PhpStanRunner().config_for(str(root), workdir)
        assert list(root.glob("*.neon")) == [root / "phpstan.neon"]

    def test_a_project_that_registers_them_itself_runs_on_its_own_config(self, tmp_path):
        root = make_repo(tmp_path)
        workdir = tmp_path / "work"
        workdir.mkdir()
        config = ps.PhpStanRunner().config_for(str(root), workdir)
        # Registering twice would attach two collectors and duplicate every record.
        assert config == root / "phpstan.neon"

    def test_rule_without_collector_still_counts_as_unwired(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text("services:\n\t-\n\t\tclass: CallGraph\\CallEdgeRule\n")
        missing = ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon")
        assert missing == ("CallGraph\\MethodCallCollector",)

    def test_a_commented_out_registration_does_not_count(self, tmp_path):
        """A `#` line is what a project leaves behind after trying the block and backing it out."""
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text(
            "services:\n"
            "\t# - class: CallGraph\\MethodCallCollector\n"
            "\t-\n\t\tclass: CallGraph\\CallEdgeRule # CallGraph\\MethodCallCollector later\n"
        )
        missing = ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon")
        assert missing == ("CallGraph\\MethodCallCollector",)

    def test_registration_in_an_included_file_counts(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "build").mkdir()
        (root / "build" / "call-graph.neon").write_text(WIRING)
        (root / "phpstan.neon").write_text(
            "includes:\n\t- build/call-graph.neon\n\nparameters:\n\tlevel: 5\n"
        )
        assert ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon") == ()

    def test_include_cycle_terminates(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "other.neon").write_text("includes:\n\t- phpstan.neon\n")
        (root / "phpstan.neon").write_text("includes:\n\t- other.neon\n")
        assert len(ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon")) == 2

    def test_a_missing_include_does_not_raise(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text("includes:\n\t- vendor/nothing/here.neon\n" + WIRING)
        assert ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon") == ()

    def test_placeholder_include_is_skipped_not_guessed(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text("includes:\n\t- %rootDir%/conf/config.neon\n")
        assert ps._includes((root / "phpstan.neon").read_text()) == []

    def test_wrapper_quotes_the_include_so_neon_syntax_in_the_path_is_literal(self, tmp_path):
        """`%` is parameter syntax in NEON; a path holding one must arrive as the path."""
        root = tmp_path / "100% done" / "repo"
        (root / "vendor" / "bin").mkdir(parents=True)
        (root / "vendor" / "bin" / "phpstan").write_text("#!/bin/sh\nexit 0\n")
        (root / "phpstan.neon").write_text("parameters:\n\tlevel: 5\n")
        workdir = tmp_path / "work"
        workdir.mkdir()
        text = ps.PhpStanRunner().config_for(str(root), workdir).read_text()
        include = next(line for line in text.splitlines() if line.strip().startswith("-"))
        expected = str((root / "phpstan.neon").resolve()).replace("%", "%%")
        assert include.strip() == f'- "{expected}"'

    def test_neon_string_escapes_a_quote_and_a_backslash(self):
        assert ps._neon_string('a"b\\c%d') == '"a\\"b\\\\c%%d"'

    def test_a_repository_without_a_config_cannot_be_wrapped(self, tmp_path):
        root = make_repo(tmp_path, config=False)
        workdir = tmp_path / "work"
        workdir.mkdir()
        assert ps.PhpStanRunner().config_for(str(root), workdir) is None

    def test_missing_binary_still_counts_as_unavailable(self, tmp_path):
        runner = FakeRunner(ps.RunOutcome(True), [])
        source = ps.PhpStanSource(runner=runner)
        result = source.resolve(str(make_repo(tmp_path, binary=False)), [site()])
        assert result.unresolved == {ps.REASON_UNAVAILABLE: 1}


class TestRecordParsing:
    def test_reads_a_well_formed_record(self):
        call = ps.PhpStanCall.from_json(record())
        assert call.callee_class == "App\\Emails"
        assert call.callee_is_interface is False
        assert call.via == ps.VIA_CLASS

    def test_interface_receiver_is_flagged(self):
        call = ps.PhpStanCall.from_json(record(calleeClass="App\\Mailer", calleeKind="interface"))
        assert call.callee_is_interface is True
        assert call.via == ps.VIA_INTERFACE

    def test_call_position_is_read_when_present(self):
        assert ps.PhpStanCall.from_json(record(callPos=412)).call_pos == 412

    @pytest.mark.parametrize("over", [{}, {"callPos": None}, {"callPos": "412"}])
    def test_call_position_is_none_when_absent_or_not_an_int(self, over):
        assert ps.PhpStanCall.from_json(record(**over)).call_pos is None

    def test_callee_file_is_read_when_present(self):
        call = ps.PhpStanCall.from_json(record(calleeFile="/repo/vendor/acme/Lib.php"))
        assert call.callee_file == "/repo/vendor/acme/Lib.php"

    @pytest.mark.parametrize("over", [{}, {"calleeFile": None}])
    def test_callee_file_defaults_to_empty(self, over):
        assert ps.PhpStanCall.from_json(record(**over)).callee_file == ""

    def test_caller_file_is_read_when_present(self):
        call = ps.PhpStanCall.from_json(record(callerFile="/repo/src/Plain.php"))
        assert call.caller_file == "/repo/src/Plain.php"

    @pytest.mark.parametrize("over", [{}, {"callerFile": None}])
    def test_caller_file_defaults_to_empty(self, over):
        assert ps.PhpStanCall.from_json(record(**over)).caller_file == ""

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-dict",
            {"calleeMethod": "send", "file": "a", "line": 1},
            {"calleeClass": "", "calleeMethod": "send", "file": "a", "line": 1},
            {"calleeClass": "C", "calleeMethod": "", "file": "a", "line": 1},
            {"calleeClass": "C", "calleeMethod": "send", "file": "a", "line": "not-a-line"},
        ],
    )
    def test_incomplete_record_is_rejected_rather_than_guessed(self, raw):
        assert ps.PhpStanCall.from_json(raw) is None

    def test_malformed_line_is_counted_and_the_rest_survive(self):
        text = "\n".join([json.dumps(record()), "{ truncated", "", json.dumps(record(line=20))])
        calls, malformed = ps._parse(text)
        assert len(calls) == 2
        assert malformed == 1

    def test_a_record_that_is_valid_json_but_not_a_call_counts_as_malformed(self):
        calls, malformed = ps._parse(json.dumps({"unexpected": True}))
        assert calls == []
        assert malformed == 1


class TestLineParsing:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [("16", 16), ("16:8", 16), ("16-24", 16), ("line 16", 16), ("", 0), ("none", 0)],
    )
    def test_leading_integer_is_the_line(self, location, expected):
        assert ps._line_of(location) == expected


class FakeRunner:
    """Stands in for the subprocess: writes what PHPStan would have written."""

    def __init__(self, outcome: ps.RunOutcome, lines: list[str] | None = None) -> None:
        self.outcome = outcome
        self.lines = lines or []
        self.calls: list[Path] = []

    def run(self, repo_root: str, output_path: Path) -> ps.RunOutcome:
        self.calls.append(output_path)
        if self.lines:
            output_path.write_text("\n".join(self.lines) + "\n")
        return self.outcome


def source_with(runner: FakeRunner) -> ps.PhpStanSource:
    return ps.PhpStanSource(runner=runner)


class TestResolve:
    def test_matches_a_site_to_its_record(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(record(file=str(root / "src/App.php")))
        result = source_with(FakeRunner(ps.RunOutcome(True), [line])).resolve(str(root), [site()])

        assert len(result.targets) == 1
        target = result.targets[0]
        assert target.class_fqn == "App\\Emails"
        assert target.method == "send"
        assert target.confidence is Confidence.EXTRACTED
        assert target.via == ps.VIA_CLASS
        assert result.files_seen == 1
        assert ps.REASON_NOT_TYPED not in result.unresolved

    def test_declaring_file_reaches_the_target(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(
            record(file=str(root / "src/App.php"), calleeFile=str(root / "vendor/acme/Lib.php"))
        )
        result = source_with(FakeRunner(ps.RunOutcome(True), [line])).resolve(str(root), [site()])

        assert result.targets[0].declaring_file == str(root / "vendor/acme/Lib.php")

    def test_declaring_file_is_empty_when_the_record_has_none(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(record(file=str(root / "src/App.php")))
        result = source_with(FakeRunner(ps.RunOutcome(True), [line])).resolve(str(root), [site()])

        assert result.targets[0].declaring_file == ""

    def test_interface_target_reaches_the_caller_flagged(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(
            record(
                file=str(root / "src/App.php"), calleeClass="App\\Mailer", calleeKind="interface"
            )
        )
        result = source_with(FakeRunner(ps.RunOutcome(True), [line])).resolve(str(root), [site()])

        assert result.targets[0].class_fqn == "App\\Mailer"
        assert result.targets[0].via == ps.VIA_INTERFACE

    def test_a_site_phpstan_did_not_report_is_counted_not_guessed(self, tmp_path):
        root = make_repo(tmp_path)
        reported = json.dumps(record(file=str(root / "src/App.php")))
        sites = [site(), site(line="99", method="reminderSubmitWeek")]
        result = source_with(FakeRunner(ps.RunOutcome(True), [reported])).resolve(str(root), sites)

        assert len(result.targets) == 1
        assert result.unresolved[ps.REASON_NOT_TYPED] == 1

    def test_two_calls_on_one_line_stay_apart(self, tmp_path):
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(
                record(file=path, line=20, calleeMethod="factory", calleeClass="App\\Controller")
            ),
            json.dumps(record(file=path, line=20, calleeMethod="send")),
        ]
        sites = [site(line="20", method="factory"), site(line="20", method="send")]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(str(root), sites)

        by_method = {t.method: t.class_fqn for t in result.targets}
        assert by_method == {"factory": "App\\Controller", "send": "App\\Emails"}

    def test_union_arms_of_one_call_are_all_emitted(self, tmp_path):
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(
                record(file=path, calleeMethod="get", calleeClass="App\\Emails", callPos=40)
            ),
            json.dumps(record(file=path, calleeMethod="get", calleeClass="App\\Cache", callPos=40)),
        ]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [site(method="get")]
        )

        assert {t.class_fqn for t in result.targets} == {"App\\Emails", "App\\Cache"}
        assert ps.REASON_SAME_LINE_COLLISION not in result.unresolved
        assert ps.REASON_NOT_TYPED not in result.unresolved

    def test_two_calls_of_one_method_on_one_line_are_refused_not_merged(self, tmp_path):
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(
                record(file=path, calleeMethod="get", calleeClass="App\\Emails", callPos=40)
            ),
            json.dumps(record(file=path, calleeMethod="get", calleeClass="App\\Cache", callPos=58)),
        ]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [site(method="get")]
        )

        assert result.targets == []
        assert result.unresolved[ps.REASON_SAME_LINE_COLLISION] == 1

    def test_two_sites_sharing_one_record_group_are_both_refused(self, tmp_path):
        """`$a->get() ?? $b->get()` with `$b` untyped.

        One record, two sites, no way to say whose.
        """
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(record(file=path, calleeMethod="get", calleeClass="App\\Emails", callPos=40))
        ]
        first = site(method="get")
        second = dataclasses.replace(first, caller_nid="n2")
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [first, second]
        )

        assert result.targets == []
        assert result.unresolved == {ps.REASON_SAME_LINE_COLLISION: 2}

    def test_two_sites_with_a_record_group_each_both_resolve(self, tmp_path):
        """The control: one group per site, so each site has a record to stand on."""
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(
                record(file=path, calleeMethod="get", calleeClass="App\\Emails", callPos=40)
            ),
            json.dumps(
                record(file=path, calleeMethod="get", calleeClass="App\\Emails", callPos=58)
            ),
        ]
        first = site(method="get")
        second = dataclasses.replace(first, caller_nid="n2")
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [first, second]
        )

        assert {t.site.caller_nid for t in result.targets} == {"n1", "n2"}
        assert ps.REASON_SAME_LINE_COLLISION not in result.unresolved

    def test_two_calls_on_one_line_proving_the_same_target_keep_it(self, tmp_path):
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(
                record(file=path, calleeMethod="getId", calleeClass="App\\User", callPos=40)
            ),
            json.dumps(
                record(file=path, calleeMethod="getId", calleeClass="App\\User", callPos=58)
            ),
        ]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [site(method="getId")]
        )

        assert [t.class_fqn for t in result.targets] == ["App\\User"]
        assert ps.REASON_SAME_LINE_COLLISION not in result.unresolved

    def test_records_without_call_position_still_yield_every_distinct_target(self, tmp_path):
        root = make_repo(tmp_path)
        path = str(root / "src/App.php")
        lines = [
            json.dumps(record(file=path, calleeMethod="get", calleeClass="App\\Emails")),
            json.dumps(record(file=path, calleeMethod="get", calleeClass="App\\Cache")),
        ]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(
            str(root), [site(method="get")]
        )

        assert {t.class_fqn for t in result.targets} == {"App\\Emails", "App\\Cache"}

    def test_identical_duplicate_records_yield_one_target(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(record(file=str(root / "src/App.php"), callPos=40))
        result = source_with(FakeRunner(ps.RunOutcome(True), [line, line])).resolve(
            str(root), [site()]
        )

        assert len(result.targets) == 1

    def test_relative_and_absolute_site_paths_agree(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(record(file=str(root / "src/App.php")))
        absolute = site(file=str(root / "src" / "App.php"))
        result = source_with(FakeRunner(ps.RunOutcome(True), [line])).resolve(str(root), [absolute])

        assert len(result.targets) == 1

    def test_malformed_line_is_reported_alongside_the_good_targets(self, tmp_path):
        root = make_repo(tmp_path)
        lines = [json.dumps(record(file=str(root / "src/App.php"))), "{ truncated"]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(str(root), [site()])

        assert len(result.targets) == 1
        assert result.unresolved[ps.REASON_MALFORMED] == 1


def probe(method: str, line: int, scope: str, caller: tuple[str, ...] | None) -> CallSite:
    """A scoped-probe candidate as `resolver.collect_scoped_groups` builds it."""
    return CallSite(
        caller_nid="n1",
        receiver=scope,
        method=method,
        source_file="src/App.php",
        source_location=f"L{line}",
        scoped=True,
        caller_scope=caller,
    )


def member(method: str, line: int, caller: tuple[str, ...] | None) -> CallSite:
    return CallSite(
        caller_nid="n1",
        receiver="",
        method=method,
        source_file="src/App.php",
        source_location=f"L{line}",
        caller_scope=caller,
    )


class TestCallerAndCallKind:
    """The collector records only `MethodCall`, so a record is evidence for a method call alone.

    The three shapes below produced wrong EXTRACTED edges on graphify 0.9.103, where the scoped
    probes come from `new X(...)`: each probe was matched to a different call on its line.
    """

    def run(self, root: Path, sites: list[CallSite], records: list[dict]):
        path = str(root / "src/App.php")
        lines = [json.dumps(record(file=path, **r)) for r in records]
        runner = FakeRunner(ps.RunOutcome(True), lines)
        return runner, source_with(runner).resolve(str(root), sites)

    def test_realistic_new_wrapping_an_arrow_fn_call_is_not_that_call(self, tmp_path):
        # return new Bag(array_map(fn (Item $i) => $i->map(), $items));
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\Service", callerMethod="wrap", calleeClass="App\\Item")
            | dict(calleeMethod="map", line=10, callPos=40)
        ]
        _runner, result = self.run(root, [probe("map", 10, "Bag", ("Service::wrap",))], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_NOT_A_METHOD_CALL: 1}

    def test_probes_alone_never_launch_phpstan(self, tmp_path):
        root = make_repo(tmp_path)
        runner, result = self.run(root, [probe("map", 10, "Bag", ("Service::wrap",))], [])

        assert runner.calls == []
        assert result.unresolved == {ps.REASON_NOT_A_METHOD_CALL: 1}

    def test_closure_on_a_new_line_keeps_the_closure_edge_and_refuses_the_probe(self, tmp_path):
        # $f = fn (Bar $b) => $b->make(); return Foo::make($f(new Bar()));
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\Outer", callerMethod="viaClosure", calleeClass="App\\Bar")
            | dict(calleeMethod="make", line=16, callPos=30)
        ]
        closure_site = member("make", 16, ("Outer::viaClosure",))
        sites = [probe("make", 16, "Bar", ("Outer::viaClosure",)), closure_site]
        _runner, result = self.run(root, sites, records)

        assert [(t.site, t.class_fqn) for t in result.targets] == [(closure_site, "App\\Bar")]
        assert result.unresolved == {ps.REASON_NOT_A_METHOD_CALL: 1}

    def test_one_line_class_record_never_crosses_into_another_method(self, tmp_path):
        # class OneLine { __construct() { $this->bar = new Bar(); } first() { $this->bar->get() } }
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\OneLine", callerMethod="first", calleeClass="App\\Bar")
            | dict(calleeMethod="get", line=5, callPos=70)
        ]
        in_ctor = member("get", 5, ("OneLine::__construct",))
        in_first = member("get", 5, ("OneLine::first",))
        sites = [probe("get", 5, "Bar", ("OneLine::__construct",)), in_ctor, in_first]
        _runner, result = self.run(root, sites, records)

        assert [t.site for t in result.targets] == [in_first]
        assert result.unresolved == {
            ps.REASON_NOT_A_METHOD_CALL: 1,
            ps.REASON_CALLER_MISMATCH: 1,
        }

    def test_a_caller_no_class_method_encloses_is_refused(self, tmp_path):
        root = make_repo(tmp_path)
        records = [dict(callerClass="App\\Controller", callerMethod="run")]
        _runner, result = self.run(root, [member("send", 16, ())], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_UNNAMED: 1}

    def test_a_trait_method_matches_the_class_phpstan_analysed_it_in(self, tmp_path):
        root = make_repo(tmp_path)
        records = [dict(callerClass="App\\Command\\RolesAddCommand", callerMethod="grant")]
        caller = ("UserRolesTrait::grant", "RolesAddCommand::grant")
        _runner, result = self.run(root, [member("send", 16, caller)], records)

        assert [t.class_fqn for t in result.targets] == ["App\\Emails"]
        assert result.unresolved == {}

    def test_caller_comparison_ignores_case_as_php_does(self, tmp_path):
        root = make_repo(tmp_path)
        records = [dict(callerClass="App\\controller", callerMethod="RUN")]
        _runner, result = self.run(root, [member("send", 16, ("Controller::run",))], records)

        assert len(result.targets) == 1

    def test_two_callers_on_one_line_each_keep_their_own_record(self, tmp_path):
        root = make_repo(tmp_path)
        records = [
            dict(callerMethod="first", calleeClass="App\\Bar", calleeMethod="get", callPos=10),
            dict(callerMethod="second", calleeClass="App\\Baz", calleeMethod="get", callPos=20),
        ]
        first = member("get", 16, ("Controller::first",))
        second = member("get", 16, ("Controller::second",))
        _runner, result = self.run(root, [first, second], records)

        assert sorted((t.site.caller_scope, t.class_fqn) for t in result.targets) == [
            (("Controller::first",), "App\\Bar"),
            (("Controller::second",), "App\\Baz"),
        ]
        assert result.unresolved == {}

    def test_a_site_with_no_caller_evidence_is_matched_as_before(self, tmp_path):
        root = make_repo(tmp_path)
        _runner, result = self.run(root, [member("send", 16, None)], [dict()])

        assert len(result.targets) == 1

    def test_no_caller_evidence_takes_a_record_whatever_caller_made_it(self, tmp_path):
        root = make_repo(tmp_path)
        records = [dict(callerClass="App\\Elsewhere", callerMethod="other")]
        _runner, result = self.run(root, [member("send", 16, None)], records)

        assert [t.class_fqn for t in result.targets] == ["App\\Emails"]
        assert result.unresolved == {}

    def test_an_unnamed_caller_with_no_record_on_its_line_is_counted_as_unnamed(self, tmp_path):
        root = make_repo(tmp_path)
        records = [dict(line=99)]
        _runner, result = self.run(root, [member("send", 16, ())], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_UNNAMED: 1}

    def test_a_closure_the_graph_could_not_place_takes_no_record_from_its_line(self, tmp_path):
        # `return fn () => $x->send(); } public function second() {` -- the adapter gives `()`.
        root = make_repo(tmp_path)
        records = [dict(callerMethod="first")]
        _runner, result = self.run(root, [member("send", 16, ())], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_UNNAMED: 1}

    def test_a_method_whose_class_the_graph_lost_takes_its_records(self, tmp_path):
        root = make_repo(tmp_path)
        here = str(root / "src/App.php")
        records = [
            dict(callerClass="Tests\\BrokenTest", callerMethod="testFoo", callerFile=here),
            dict(callerClass="Tests\\BrokenTest", callerMethod="testFoo", line=30),
        ]
        _runner, result = self.run(root, [member("send", 16, ("::testfoo",))], records)

        assert [t.class_fqn for t in result.targets] == ["App\\Emails"]
        assert result.unresolved == {}

    @pytest.mark.parametrize("context", ["src/Plain2.php", None])
    def test_a_lost_class_method_takes_no_record_made_in_another_files_context(
        self, tmp_path, context
    ):
        # A trait body analysed in `Plain2`: its line is the trait's, the class is Plain2's.
        # Without `callerFile` nothing shows the record was made where the site is.
        root = make_repo(tmp_path)
        extra = {} if context is None else {"callerFile": str(root / context)}
        records = [dict(callerClass="App\\Plain2", callerMethod="run", **extra)]
        _runner, result = self.run(root, [member("send", 16, ("::run",))], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_OTHER_FILE: 1}

    def test_a_trait_body_record_matches_the_trait_site_from_the_using_class_context(
        self, tmp_path
    ):
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\Plain", callerMethod="run", callerFile=str(root / "P.php")),
            dict(callerClass="App\\Plain2", callerMethod="run", callerFile=str(root / "Q.php")),
        ]
        caller = ("T::run", "Plain::run", "Plain2::run")
        _runner, result = self.run(root, [member("send", 16, caller)], records)

        assert [t.class_fqn for t in result.targets] == ["App\\Emails"]
        assert result.unresolved == {}

    def test_a_lost_class_method_is_refused_when_two_classes_in_the_file_share_its_name(
        self, tmp_path
    ):
        root = make_repo(tmp_path)
        here = str(root / "src/App.php")
        records = [
            dict(callerClass="Tests\\BrokenTest", callerMethod="testFoo", callerFile=here),
            dict(callerClass="Tests\\Helper", callerMethod="testFoo", line=40, callerFile=here),
        ]
        _runner, result = self.run(root, [member("send", 16, ("::testFoo",))], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_AMBIGUOUS: 1}

    def test_a_short_class_name_two_namespaces_share_in_the_file_is_refused(self, tmp_path):
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\A\\Controller", callerMethod="run"),
            dict(callerClass="\\App\\B\\Controller", callerMethod="other", line=30),
        ]
        _runner, result = self.run(root, [member("send", 16, ("Controller::run",))], records)

        assert result.targets == []
        assert result.unresolved == {ps.REASON_CALLER_AMBIGUOUS: 1}

    def test_one_class_spelled_with_and_without_a_leading_backslash_is_one_class(self, tmp_path):
        root = make_repo(tmp_path)
        records = [
            dict(callerClass="App\\Controller", callerMethod="run"),
            dict(callerClass="\\app\\controller", callerMethod="other", line=30),
        ]
        _runner, result = self.run(root, [member("send", 16, ("Controller::run",))], records)

        assert len(result.targets) == 1


def anchor(line: int, caller: tuple[str, ...] | None, owns_line: bool = True) -> CallSite:
    """A refused `new X(...)` probe, as the resolver hands it over to place unsent records."""
    site = probe("getName", line, "SyncUserMessage", caller)
    return dataclasses.replace(site, owns_line=owns_line)


class TestUnsentCalls:
    """graphify core resolves some member calls by bare name and never sends them as raw calls.

    `$task = new SyncUserMessage($legacyUser->getName(), $batch->getId());` arrives as the
    `new` probe and the `getId` member site only. The `getName` record is still on that line.
    """

    SYNC = ("SyncUsers::syncUsers",)

    def records(self, root: Path, *extra: dict) -> list[str]:
        base = dict(callerClass="App\\SyncUsers", callerMethod="syncUsers", line=74)
        rows = [
            base | dict(calleeClass="App\\User", calleeMethod="getName", callPos=10),
            base | dict(calleeClass="App\\CronBatch", calleeMethod="getId", callPos=30),
            *(base | row for row in extra),
        ]
        return [json.dumps(record(file=str(root / "src/App.php"), **row)) for row in rows]

    def run(self, root: Path, anchors, members, *extra: dict, outcome=None, tail=None):
        lines = self.records(root, *extra) + ([tail] if tail else [])
        source = source_with(FakeRunner(outcome or ps.RunOutcome(True), lines))
        source.resolve(str(root), members)
        return source.unsent_calls(str(root), anchors, members)

    def test_a_record_no_member_site_claims_is_edged_from_the_anchors_caller(self, tmp_path):
        root = make_repo(tmp_path)
        place = anchor(74, self.SYNC)
        found = self.run(root, [place], [member("getId", 74, self.SYNC)])

        assert [(t.site, t.class_fqn, t.method, t.via) for t in found] == [
            (place, "App\\User", "getName", ps.VIA_CLASS)
        ]

    def test_a_record_a_member_site_names_stays_with_that_site(self, tmp_path):
        root = make_repo(tmp_path)
        members = [member("getId", 74, self.SYNC), member("GETNAME", 74, ("Other::x",))]

        assert self.run(root, [anchor(74, self.SYNC)], members) == []

    def test_a_scoped_site_of_the_same_name_claims_no_record(self, tmp_path):
        # `Foo::getName($user->getName())`: the static call has no record of its own.
        root = make_repo(tmp_path)
        scoped = dataclasses.replace(member("getName", 74, self.SYNC), scoped=True)
        found = self.run(root, [anchor(74, self.SYNC)], [member("getId", 74, self.SYNC), scoped])

        assert [t.method for t in found] == ["getName"]

    def test_an_anchor_that_may_share_its_line_with_a_nested_callable_gets_nothing(self, tmp_path):
        root = make_repo(tmp_path)
        found = self.run(
            root, [anchor(74, self.SYNC, owns_line=False)], [member("getId", 74, self.SYNC)]
        )

        assert found == []

    def test_a_record_made_in_another_method_is_not_the_anchors(self, tmp_path):
        root = make_repo(tmp_path)
        found = self.run(
            root, [anchor(74, ("SyncUsers::syncLeads",))], [member("getId", 74, self.SYNC)]
        )

        assert found == []

    @pytest.mark.parametrize("caller", [None, ()])
    def test_an_anchor_with_no_caller_evidence_gets_nothing(self, tmp_path, caller):
        root = make_repo(tmp_path)

        assert self.run(root, [anchor(74, caller)], [member("getId", 74, self.SYNC)]) == []

    def test_two_anchors_of_one_caller_on_one_line_yield_the_record_once(self, tmp_path):
        root = make_repo(tmp_path)
        first = anchor(74, self.SYNC)
        second = dataclasses.replace(first, method="x")
        found = self.run(root, [first, second], [member("getId", 74, self.SYNC)])

        assert [(t.class_fqn, t.method) for t in found] == [("App\\User", "getName")]

    def test_no_phpstan_run_means_nothing_to_place(self, tmp_path):
        root = make_repo(tmp_path)
        source = source_with(FakeRunner(ps.RunOutcome(True), self.records(root)))

        assert source.unsent_calls(str(root), [anchor(74, self.SYNC)], []) == []

    def test_a_lost_class_method_anchor_uses_the_same_caller_rule(self, tmp_path):
        root = make_repo(tmp_path)
        here = dict(callerFile=str(root / "src/App.php"))
        found = self.run(
            root,
            [anchor(74, ("::syncUsers",))],
            [member("getId", 74, self.SYNC)],
            here | dict(calleeClass="App\\Batch", calleeMethod="getSize", callPos=50),
        )

        assert [t.method for t in found] == ["getSize"]

    def test_a_lost_class_method_anchor_takes_no_record_made_in_another_files_context(
        self, tmp_path
    ):
        root = make_repo(tmp_path)
        elsewhere = dict(callerFile=str(root / "src/Plain2.php"))
        found = self.run(
            root,
            [anchor(74, ("::syncUsers",))],
            [member("getId", 74, self.SYNC)],
            elsewhere | dict(calleeClass="App\\Batch", calleeMethod="getSize", callPos=50),
        )

        assert found == []

    @pytest.mark.parametrize(
        "outcome",
        [
            ps.RunOutcome(False, ps.REASON_TIMEOUT),
            ps.RunOutcome(False, ps.REASON_RUN_FAILED, exit_code=255),
            ps.RunOutcome(False, ps.REASON_RUN_FAILED),
        ],
    )
    def test_records_of_a_run_that_did_not_finish_place_nothing(self, tmp_path, outcome):
        root = make_repo(tmp_path)
        found = self.run(
            root, [anchor(74, self.SYNC)], [member("getId", 74, self.SYNC)], outcome=outcome
        )

        assert found == []

    def test_records_of_a_run_that_reported_analysis_errors_are_placed(self, tmp_path):
        root = make_repo(tmp_path)
        outcome = ps.RunOutcome(False, ps.REASON_RUN_FAILED, exit_code=1)
        found = self.run(
            root, [anchor(74, self.SYNC)], [member("getId", 74, self.SYNC)], outcome=outcome
        )

        assert [t.method for t in found] == ["getName"]

    def test_a_truncated_output_places_nothing(self, tmp_path):
        root = make_repo(tmp_path)
        found = self.run(
            root, [anchor(74, self.SYNC)], [member("getId", 74, self.SYNC)], tail='{"calleeCl'
        )

        assert found == []

    def test_a_failed_run_forgets_the_records_of_the_run_before_it(self, tmp_path):
        root = make_repo(tmp_path)
        members = [member("getId", 74, self.SYNC)]
        source = source_with(FakeRunner(ps.RunOutcome(True), self.records(root)))
        source.resolve(str(root), members)
        source._runner = FakeRunner(ps.RunOutcome(False, ps.REASON_TIMEOUT))
        source.resolve(str(root), members)

        assert source.unsent_calls(str(root), [anchor(74, self.SYNC)], members) == []

    def test_a_probes_only_ask_that_runs_nothing_keeps_the_last_runs_records(self, tmp_path):
        # The resolver asks again for scoped groups; that ask never launches PHPStan.
        root = make_repo(tmp_path)
        members = [member("getId", 74, self.SYNC)]
        source = source_with(FakeRunner(ps.RunOutcome(True), self.records(root)))
        source.resolve(str(root), members)
        source.resolve(str(root), [anchor(74, self.SYNC)])

        found = source.unsent_calls(str(root), [anchor(74, self.SYNC)], members)
        assert [t.method for t in found] == ["getName"]


def nullsafe(method: str, line: int, caller: tuple[str, ...] | None) -> CallSite:
    """A `$x?->m()` the parser placed in a caller node; it needs no anchor and owns no line."""
    return member(method, line, caller)


class TestNullsafeCalls:
    """`$user?->getName()` reaches no raw call, so the parser's site is offered instead.

    The parser vouched for the caller and for the line holding only nullsafe calls of that
    name; this side takes the records of that name made inside that caller.
    """

    SYNC = TestUnsentCalls.SYNC

    def run(self, root: Path, sites, *extra: dict):
        lines = TestUnsentCalls().records(root, *extra)
        source = source_with(FakeRunner(ps.RunOutcome(True), lines))
        source.resolve(str(root), [member("x", 1, self.SYNC)])
        return source.unsent_calls(str(root), [], [], nullsafe=sites)

    def test_a_record_of_the_sites_name_in_its_caller_is_its_target(self, tmp_path):
        root = make_repo(tmp_path)
        site = nullsafe("GETNAME", 74, self.SYNC)
        found = self.run(root, [site])

        assert [(t.site, t.class_fqn, t.method, t.confidence) for t in found] == [
            (site, "App\\User", "getName", Confidence.EXTRACTED)
        ]

    def test_two_records_of_one_call_are_its_union_arms(self, tmp_path):
        root = make_repo(tmp_path)
        arm = dict(calleeClass="App\\Admin", calleeMethod="getName", callPos=10)
        found = self.run(root, [nullsafe("getName", 74, self.SYNC)], arm)

        assert [t.class_fqn for t in found] == ["App\\User", "App\\Admin"]

    def test_a_record_made_in_another_method_is_not_the_sites(self, tmp_path):
        root = make_repo(tmp_path)

        assert self.run(root, [nullsafe("getName", 74, ("SyncUsers::other",))]) == []

    @pytest.mark.parametrize("caller", [None, ()])
    def test_a_site_with_no_caller_evidence_gets_nothing(self, tmp_path, caller):
        root = make_repo(tmp_path)

        assert self.run(root, [nullsafe("getName", 74, caller)]) == []

    def test_a_site_with_no_record_on_its_line_gets_nothing(self, tmp_path):
        root = make_repo(tmp_path)

        assert self.run(root, [nullsafe("getName", 75, self.SYNC)]) == []

    def test_no_phpstan_run_means_nothing_to_take(self, tmp_path):
        root = make_repo(tmp_path)
        source = source_with(FakeRunner(ps.RunOutcome(True)))

        assert source.unsent_calls(str(root), [], [], nullsafe=[nullsafe("x", 74, self.SYNC)]) == []


class TestLineProofs:
    """Records on one line for one method name, made inside the site's caller.

    The resolver checks graphify's name-bound engine edges against these, so the caller rule is
    the one `resolve` uses and a caller the graph cannot name gets no proof at all.
    """

    RUN = ("Controller::run",)

    def proofs(self, root: Path, sites, *rows: dict):
        lines = [json.dumps(record(file=str(root / "src/App.php"), **row)) for row in rows]
        source = source_with(FakeRunner(ps.RunOutcome(True), lines))
        source.resolve(str(root), [member("send", 16, self.RUN)])
        return source.line_proofs(str(root), sites)

    def test_records_of_the_caller_are_the_proof_and_their_calls_are_counted(self, tmp_path):
        root = make_repo(tmp_path)
        at = member("get", 16, self.RUN)
        found = self.proofs(
            root,
            [at],
            dict(calleeClass="App\\Cache", calleeMethod="get", callPos=1, calleeFile="/x/C.php"),
            dict(calleeClass="App\\Store", calleeMethod="GET", callPos=9),
            dict(calleeClass="App\\Store", calleeMethod="get", callPos=9),
            dict(calleeClass="App\\Cache", calleeMethod="get", callerMethod="other", callPos=4),
        )[at]

        assert [(t.class_fqn, t.declaring_file, t.site) for t in found.targets] == [
            ("App\\Cache", "/x/C.php", at),
            ("App\\Store", "", at),
        ]
        assert found.calls == 2

    def test_an_interface_record_says_so(self, tmp_path):
        root = make_repo(tmp_path)
        at = member("get", 16, self.RUN)
        row = dict(calleeClass="App\\CacheInterface", calleeMethod="get", calleeKind="interface")
        (target,) = self.proofs(root, [at], row)[at].targets

        assert target.interface_target is True
        assert target.via == ps.VIA_INTERFACE

    @pytest.mark.parametrize("caller", [None, ()])
    def test_a_caller_with_no_scope_gets_no_proof(self, tmp_path, caller):
        root = make_repo(tmp_path)
        at = member("get", 16, caller)
        found = self.proofs(root, [at], dict(calleeClass="App\\Cache", calleeMethod="get"))[at]

        assert (found.targets, found.calls) == ((), 0)

    def test_no_finished_run_means_no_proofs(self, tmp_path):
        root = make_repo(tmp_path)

        assert source_with(FakeRunner(ps.RunOutcome(True))).line_proofs(str(root), []) is None


class TestFailureModes:
    def test_unavailable_repository_yields_a_skip_not_an_exception(self, tmp_path):
        root = make_repo(tmp_path, binary=False)
        result = ps.PhpStanSource().resolve(str(root), [site(), site(line="20")])

        assert result.targets == []
        assert result.unresolved == {ps.REASON_UNAVAILABLE: 2}

    def test_timeout_is_a_counted_reason(self, tmp_path):
        root = make_repo(tmp_path)
        runner = FakeRunner(ps.RunOutcome(False, ps.REASON_TIMEOUT))
        result = source_with(runner).resolve(str(root), [site()])

        assert result.targets == []
        assert result.unresolved == {ps.REASON_TIMEOUT: 1}

    def test_nonzero_exit_with_no_records_is_unavailable_for_this_build(self, tmp_path):
        root = make_repo(tmp_path)
        runner = FakeRunner(
            ps.RunOutcome(False, ps.REASON_RUN_FAILED, "PHP Fatal error: out of memory")
        )
        result = source_with(runner).resolve(str(root), [site()])

        assert result.targets == []
        assert result.unresolved[ps.REASON_RUN_FAILED] == 1
        assert ps.STDERR_PREFIX + "PHP Fatal error: out of memory" in result.unresolved

    def test_nonzero_exit_with_records_still_yields_them(self, tmp_path):
        """PHPStan exits 1 on any project that has analysis errors of its own."""
        root = make_repo(tmp_path)
        line = json.dumps(record(file=str(root / "src/App.php")))
        runner = FakeRunner(ps.RunOutcome(False, ps.REASON_RUN_FAILED), [line])
        result = source_with(runner).resolve(str(root), [site()])

        assert len(result.targets) == 1
        assert ps.REASON_RUN_FAILED not in result.unresolved

    def test_clean_exit_with_no_output_says_so(self, tmp_path):
        root = make_repo(tmp_path)
        result = source_with(FakeRunner(ps.RunOutcome(True))).resolve(str(root), [site()])

        assert result.unresolved == {ps.REASON_NO_OUTPUT: 1}

    def test_no_sites_does_not_launch_phpstan(self, tmp_path):
        root = make_repo(tmp_path)
        runner = FakeRunner(ps.RunOutcome(True))
        result = source_with(runner).resolve(str(root), [])

        assert runner.calls == []
        assert result.unresolved == {}


def gone(pid: int, within_s: float = 3.0) -> bool:
    """True once the process has exited (a zombie that is not yet reaped still counts)."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


class TestRunner:
    def test_timeout_kills_the_processes_phpstan_spawned_not_only_the_phar(
        self, tmp_path, monkeypatch
    ):
        """PHPStan's phar re-executes itself and forks workers; a kill of the child alone
        leaves them running to completion, which was observed on the reference application."""
        root = make_repo(tmp_path)
        pid_file = tmp_path / "grandchild.pid"
        binary = root / "vendor" / "bin" / "phpstan"
        binary.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > '{pid_file}'\nwait\n")
        binary.chmod(0o755)
        monkeypatch.setenv(ps._TIMEOUT_ENV, "1")

        outcome = ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert outcome.ok is False
        assert outcome.reason == ps.REASON_TIMEOUT
        grandchild = int(pid_file.read_text())
        assert gone(grandchild), f"sleep {grandchild} survived the timeout"

    def test_unlaunchable_binary_becomes_a_reason(self, tmp_path):
        root = make_repo(tmp_path)
        # `make_repo` writes the script without the execute bit, so launching it is refused.
        outcome = ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert outcome.ok is False
        assert outcome.reason == ps.REASON_UNAVAILABLE

    @pytest.mark.parametrize("code", [0, 1, 255])
    def test_the_exit_code_is_kept(self, tmp_path, code):
        root = make_repo(tmp_path)
        binary = root / "vendor" / "bin" / "phpstan"
        binary.write_text(f"#!/bin/sh\nexit {code}\n")
        binary.chmod(0o755)

        assert ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl").exit_code == code

    def test_timeout_is_overridable_and_falls_back_on_nonsense(self, monkeypatch):
        monkeypatch.setenv(ps._TIMEOUT_ENV, "12.5")
        assert ps.PhpStanRunner().timeout_s() == 12.5
        monkeypatch.setenv(ps._TIMEOUT_ENV, "soon")
        assert ps.PhpStanRunner().timeout_s() == ps._DEFAULT_TIMEOUT_S

    def test_command_is_safe_to_run_unattended(self, tmp_path):
        root = make_repo(tmp_path)
        argv = ps.PhpStanRunner().command(str(root), root / "phpstan.neon")

        assert argv[1] == "analyse"
        assert "--no-progress" in argv
        assert "--no-interaction" in argv
        assert any(arg.startswith("--memory-limit=") for arg in argv)
        assert "--autoload-file" in argv

    def test_subprocess_is_given_no_stdin_its_own_session_and_the_output_path(
        self, tmp_path, monkeypatch
    ):
        root = make_repo(tmp_path)
        seen = {}

        class Launched:
            returncode = 0

            def __init__(self, argv, **kwargs):
                seen["argv"] = argv
                seen["kwargs"] = kwargs

            def communicate(self, timeout=None):
                return "", ""

        monkeypatch.setattr(ps.subprocess, "Popen", Launched)
        ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
        assert seen["kwargs"]["cwd"] == str(root)
        assert seen["kwargs"]["env"]["PHP_CALL_GRAPH_OUT"] == str(tmp_path / "out.jsonl")
        assert seen["kwargs"]["start_new_session"] is True

    def test_stderr_secrets_never_reach_the_record(self, tmp_path):
        """PHPStan runs project bootstrap code, and a fatal there can print a DSN."""
        root = make_repo(tmp_path)
        binary = root / "vendor" / "bin" / "phpstan"
        binary.write_text(
            "#!/bin/sh\n"
            "echo 'PHP Fatal error: connect password=hunter2 DB_TOKEN: abc dsn=mysql://u:p@h/db'"
            " 'and https://user:pw@host/x' >&2\nexit 255\n"
        )
        binary.chmod(0o755)

        result = ps.PhpStanSource().resolve(str(root), [site()])

        keys = "\n".join(result.unresolved)
        assert ps.STDERR_PREFIX + "PHP Fatal error" in keys
        for secret in ("hunter2", "abc", "u:p@", "user:pw@"):
            assert secret not in keys, keys
        assert "mysql://<redacted>@h/db" not in keys or "dsn=<redacted>" in keys


class TestExtensionShipped:
    def test_autoload_file_is_found_beside_the_package(self):
        assert ps.extension_autoload_file() is not None

    def test_php_sources_parse(self):
        php = ps.extension_autoload_file().parent
        for name in [
            "CallEdge",
            "CallEdgeWriter",
            "JsonLinesWriter",
            "MethodCallCollector",
            "CallEdgeRule",
        ]:
            assert (php / "src" / "CallGraph" / f"{name}.php").is_file()
