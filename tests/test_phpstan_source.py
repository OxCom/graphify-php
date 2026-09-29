"""Unit tests for the PHPStan source.

None of these run PHPStan. The point of the source is what it does when PHPStan is absent,
slow or broken, and a test that needs a working `vendor/` to exercise the absent case would
never run on the nine of twenty repositories that have no PHPStan at all.
"""

from __future__ import annotations

import json
import subprocess
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


def make_repo(tmp_path: Path, *, binary: bool = True, config: bool = True,
              wired: bool = True) -> Path:
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
    """The collector check.

    Without the `services:` block PHPStan exits clean, writes nothing, and the source reported
    `phpstan-no-output` for every site — a full-length run whose silence looked exactly like a
    project of untyped receivers. Measured on one Symfony application: 6803 sites counted under
    that reason, 855 edges instead of 2990.
    """

    def test_unwired_config_is_a_reason_not_a_run(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path, wired=False)))
        assert ok is False
        assert "MethodCallCollector" in reason

    def test_wired_config_is_available(self, tmp_path):
        ok, reason = ps.PhpStanSource().available(str(make_repo(tmp_path)))
        assert (ok, reason) == (True, "")

    def test_rule_without_collector_is_still_unwired(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text(
            "services:\n\t-\n\t\tclass: CallGraph\\CallEdgeRule\n")
        missing = ps.EnvironmentProbe().missing_wiring(root / "phpstan.neon")
        assert missing == ("CallGraph\\MethodCallCollector",)

    def test_registration_in_an_included_file_counts(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "build").mkdir()
        (root / "build" / "call-graph.neon").write_text(WIRING)
        (root / "phpstan.neon").write_text(
            "includes:\n\t- build/call-graph.neon\n\nparameters:\n\tlevel: 5\n")
        ok, _reason = ps.PhpStanSource().available(str(root))
        assert ok is True

    def test_include_cycle_terminates(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "other.neon").write_text("includes:\n\t- phpstan.neon\n")
        (root / "phpstan.neon").write_text("includes:\n\t- other.neon\n")
        ok, _reason = ps.PhpStanSource().available(str(root))
        assert ok is False

    def test_a_missing_include_does_not_raise(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text(
            "includes:\n\t- vendor/nothing/here.neon\n" + WIRING)
        ok, _reason = ps.PhpStanSource().available(str(root))
        assert ok is True

    def test_placeholder_include_is_skipped_not_guessed(self, tmp_path):
        root = make_repo(tmp_path, wired=False)
        (root / "phpstan.neon").write_text("includes:\n\t- %rootDir%/conf/config.neon\n")
        assert ps._includes((root / "phpstan.neon").read_text()) == []

    def test_unwired_repository_never_launches_phpstan(self, tmp_path):
        runner = FakeRunner(ps.RunOutcome(True), [json.dumps(record())])
        source = ps.PhpStanSource(runner=runner)
        result = source.resolve(str(make_repo(tmp_path, wired=False)), [site()])
        assert runner.calls == []
        assert result.targets == []
        assert result.unresolved == {ps.REASON_NOT_WIRED: 1}

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

    def test_interface_target_reaches_the_caller_flagged(self, tmp_path):
        root = make_repo(tmp_path)
        line = json.dumps(
            record(file=str(root / "src/App.php"), calleeClass="App\\Mailer", calleeKind="interface")
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
            json.dumps(record(file=path, line=20, calleeMethod="factory", calleeClass="App\\Controller")),
            json.dumps(record(file=path, line=20, calleeMethod="send")),
        ]
        sites = [site(line="20", method="factory"), site(line="20", method="send")]
        result = source_with(FakeRunner(ps.RunOutcome(True), lines)).resolve(str(root), sites)

        by_method = {t.method: t.class_fqn for t in result.targets}
        assert by_method == {"factory": "App\\Controller", "send": "App\\Emails"}

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
        runner = FakeRunner(ps.RunOutcome(False, ps.REASON_RUN_FAILED, "PHP Fatal error: out of memory"))
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


class TestRunner:
    def test_timeout_expiring_becomes_a_reason(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path)

        def explode(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="phpstan", timeout=1.0)

        monkeypatch.setattr(ps.subprocess, "run", explode)
        outcome = ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert outcome.ok is False
        assert outcome.reason == ps.REASON_TIMEOUT

    def test_unlaunchable_binary_becomes_a_reason(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path)

        def explode(*_args, **_kwargs):
            raise OSError("Exec format error")

        monkeypatch.setattr(ps.subprocess, "run", explode)
        outcome = ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert outcome.ok is False
        assert outcome.reason == ps.REASON_UNAVAILABLE

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

    def test_subprocess_is_given_no_stdin_and_the_output_path(self, tmp_path, monkeypatch):
        root = make_repo(tmp_path)
        seen = {}

        def capture(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(ps.subprocess, "run", capture)
        ps.PhpStanRunner().run(str(root), tmp_path / "out.jsonl")

        assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
        assert seen["kwargs"]["cwd"] == str(root)
        assert seen["kwargs"]["env"]["PHP_CALL_GRAPH_OUT"] == str(tmp_path / "out.jsonl")
        assert seen["kwargs"]["check"] is False


class TestExtensionShipped:
    def test_autoload_file_is_found_beside_the_package(self):
        assert ps.extension_autoload_file() is not None

    def test_php_sources_parse(self):
        php = ps.extension_autoload_file().parent
        for name in ["CallEdge", "CallEdgeWriter", "JsonLinesWriter", "MethodCallCollector", "CallEdgeRule"]:
            assert (php / "src" / "CallGraph" / f"{name}.php").is_file()
