"""One end-to-end run against a real PHPStan, skipped unless one is on this machine.

Kept out of the default run on purpose: it costs a full analysis, and the unit tests already
cover every branch of the source. What it covers that they cannot is the half that is not
Python — that the collector still compiles against the installed PHPStan, that the neon snippet
in `php/README.md` still wires it, and that an interface receiver still arrives flagged.

It skips itself when no PHPStan is installed, so it is safe in the default run; name this file
to run it alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from graphify_php.ports import CallSite, Confidence
from graphify_php.sources import phpstan_source as ps

REFERENCE_PHPSTAN = Path("/var/www/www.cem.dev.lo/vendor/bin/phpstan")

pytestmark = [
    pytest.mark.skipif(not REFERENCE_PHPSTAN.is_file(), reason="no PHPStan on this machine"),
    pytest.mark.skipif(shutil.which("php") is None, reason="no php on PATH"),
]

FIXTURE_PHP = """<?php
namespace App;

interface Mailer { public function send(string $to): void; }

class Emails implements Mailer {
    public function send(string $to): void {}
    public function reminderSubmitWeek(string $user): void {}
}

class Controller {
    /** @var Emails */
    private $emails;

    public function __construct(private readonly Mailer $mailer, Emails $emails) {
        $this->emails = $emails;
    }

    public function run(string $user): void {
        $this->emails->reminderSubmitWeek($user);
        $this->mailer->send($user);
    }
}
"""

NEON = """parameters:
\tlevel: 5
\tpaths:
\t\t- src

services:
\t-
\t\tclass: CallGraph\\JsonLinesWriter
\t-
\t\tclass: CallGraph\\MethodCallCollector
\t\ttags:
\t\t\t- phpstan.collector
\t-
\t\tclass: CallGraph\\CallEdgeRule
\t\ttags:
\t\t\t- phpstan.rules.rule
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.php").write_text(FIXTURE_PHP)
    (root / "phpstan.neon").write_text(NEON)
    # A symlink rather than a copy: the test must exercise the binary this machine actually has,
    # and copying a 28 MB phar into every temp directory is slower than the analysis.
    (root / "vendor" / "bin").mkdir(parents=True)
    (root / "vendor" / "bin" / "phpstan").symlink_to(REFERENCE_PHPSTAN)
    return root


def line_of(needle: str) -> str:
    """Line numbers are derived from the fixture, never written down twice."""
    for number, text in enumerate(FIXTURE_PHP.splitlines(), start=1):
        if needle in text:
            return str(number)
    raise AssertionError(f"{needle} is not in the fixture")


def test_a_real_run_resolves_both_a_class_and_an_interface_receiver(project, monkeypatch):
    monkeypatch.setenv(ps._TIMEOUT_ENV, "300")

    path = str(project / "src/App.php")
    sites = [
        CallSite("n1", "$this->emails", "reminderSubmitWeek", path, line_of("->reminderSubmitWeek(")),
        CallSite("n2", "$this->mailer", "send", path, line_of("$this->mailer->send(")),
    ]
    result = ps.PhpStanSource().resolve(str(project), sites)

    by_method = {t.method: t for t in result.targets}
    assert set(by_method) == {"reminderSubmitWeek", "send"}
    assert by_method["reminderSubmitWeek"].class_fqn == "App\\Emails"
    assert by_method["reminderSubmitWeek"].via == ps.VIA_CLASS
    assert by_method["send"].class_fqn == "App\\Mailer"
    assert by_method["send"].via == ps.VIA_INTERFACE
    assert all(t.confidence is Confidence.EXTRACTED for t in result.targets)


def test_the_collector_emits_nothing_for_a_receiver_it_cannot_type(project, tmp_path):
    """The refusal is the contract: no class reflection, no record, no edge."""
    (project / "src" / "App.php").write_text(
        FIXTURE_PHP.replace("    /** @var Emails */\n", "")
    )
    output = tmp_path / "calls.jsonl"

    env = dict(os.environ, PHP_CALL_GRAPH_OUT=str(output))
    argv = ps.PhpStanRunner().command(str(project), project / "phpstan.neon")
    subprocess.run(argv, cwd=project, env=env, capture_output=True, timeout=300, check=False)

    records = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    methods = {r["calleeMethod"] for r in records}
    assert "send" in methods, "the typed interface property must still resolve"
    assert "reminderSubmitWeek" not in methods, "an untyped property is mixed, so no target"
