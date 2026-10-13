"""One end-to-end run against a real PHPStan, skipped unless one is on this machine.

Kept out of the default run on purpose: it costs a full analysis, and the unit tests already
cover every branch of the source. What it covers that they cannot is the half that is not
Python — that the collector still compiles against the installed PHPStan, that the neon snippet
in `php/README.md` still wires it, and that an interface receiver still arrives flagged.

It skips itself unless `GRAPHIFY_PHP_REFERENCE_PROJECT` names a project root with a
composer-installed PHPStan at `vendor/bin/phpstan`, so it is safe in the default run; export the
variable and name this file to run it alone.
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

REFERENCE_PROJECT_ENV = "GRAPHIFY_PHP_REFERENCE_PROJECT"
REFERENCE_PHPSTAN = Path(os.environ.get(REFERENCE_PROJECT_ENV, ""), "vendor/bin/phpstan")

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get(REFERENCE_PROJECT_ENV),
        reason=f"{REFERENCE_PROJECT_ENV} is not set",
    ),
    pytest.mark.skipif(
        not REFERENCE_PHPSTAN.is_file(), reason=f"no PHPStan at {REFERENCE_PHPSTAN}"
    ),
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
        CallSite(
            "n1", "$this->emails", "reminderSubmitWeek", path, line_of("->reminderSubmitWeek(")
        ),
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
    (project / "src" / "App.php").write_text(FIXTURE_PHP.replace("    /** @var Emails */\n", ""))
    output = tmp_path / "calls.jsonl"

    env = dict(os.environ, PHP_CALL_GRAPH_OUT=str(output))
    argv = ps.PhpStanRunner().command(str(project), project / "phpstan.neon")
    subprocess.run(argv, cwd=project, env=env, capture_output=True, timeout=300, check=False)

    records = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    methods = {r["calleeMethod"] for r in records}
    assert "send" in methods, "the typed interface property must still resolve"
    assert "reminderSubmitWeek" not in methods, "an untyped property is mixed, so no target"


INTERSECTION_PHP = """<?php
namespace App;

interface A { public function get(string $k): string; }
interface B { public function get(string $k): string; }
interface Only { public function log(string $k): void; }

class Both implements A, B, Only {
    public function get(string $k): string { return $k; }
    public function log(string $k): void {}
}

class Emails { public function get(string $k): string { return $k; } }
class Cache { public function get(string $k): string { return $k; } }

class Controller {
    public function __construct(
        private readonly A&B $both,
        private readonly A&Only $oneDeclares,
        private readonly Emails|Cache $either,
        private readonly (A&B)|Cache $dnf,
        private readonly (A&B)|null $dnfOrNull = null,
    ) {}

    public function intersection(string $k): void {
        $this->both->get($k);
    }

    public function intersectionOneDeclares(string $k): void {
        $this->oneDeclares->get($k);
    }

    public function union(string $k): void {
        $this->either->get($k);
    }

    public function dnf(string $k): void {
        $this->dnf->get($k);
    }

    public function dnfOrNull(string $k): void {
        $this->dnfOrNull->get($k);
    }
}
"""


def test_an_intersection_both_constituents_declare_is_refused_while_a_union_keeps_both_arms(
    project, monkeypatch
):
    """The collector's decision is only visible through a real run.

    `A&B` is one object and two declarations, so no record and no edge; `A|B` is one of two
    objects, so one record per arm under the same call position. A unit test over a fixture
    record set cannot tell these apart, because the refusal happens before any record exists.

    The decision is per arm: `(A&B)|Cache` keeps `Cache::get` and drops the ambiguous
    intersection, and `(A&B)|null` has no arm left to keep. Decided on the receiver as a whole
    it would have emitted A, B and Cache under one call position — the arms of a union that
    is not in the code.
    """
    monkeypatch.setenv(ps._TIMEOUT_ENV, "300")
    (project / "src" / "App.php").write_text(INTERSECTION_PHP)

    def line_in_fixture(needle: str) -> str:
        for number, text in enumerate(INTERSECTION_PHP.splitlines(), start=1):
            if needle in text:
                return str(number)
        raise AssertionError(f"{needle} is not in the fixture")

    path = str(project / "src/App.php")
    sites = [
        CallSite("n1", "$this->both", "get", path, line_in_fixture("$this->both->get(")),
        CallSite(
            "n2", "$this->oneDeclares", "get", path, line_in_fixture("$this->oneDeclares->get(")
        ),
        CallSite("n3", "$this->either", "get", path, line_in_fixture("$this->either->get(")),
        CallSite("n4", "$this->dnf", "get", path, line_in_fixture("$this->dnf->get(")),
        CallSite("n5", "$this->dnfOrNull", "get", path, line_in_fixture("$this->dnfOrNull->get(")),
    ]
    result = ps.PhpStanSource().resolve(str(project), sites)

    by_site = {}
    for target in result.targets:
        by_site.setdefault(target.site.caller_nid, set()).add(target.class_fqn)
    assert "n1" not in by_site, "A&B with both declaring get(): no edge, not two"
    assert by_site["n2"] == {"App\\A"}, "A&Only: only A declares get(), so A is proven"
    assert by_site["n3"] == {"App\\Emails", "App\\Cache"}, "Emails|Cache keeps every arm"
    assert by_site["n4"] == {"App\\Cache"}, (
        "(A&B)|Cache: the intersection arm is dropped, Cache stands"
    )
    assert "n5" not in by_site, "(A&B)|null: no arm survives, so no edge"
    # No record is written for a refused site, so the Python side sees it as untyped and
    # leaves it for the parser source, which refuses it under its own reason. The count is
    # visible there, not here.
    assert result.unresolved == {ps.REASON_NOT_TYPED: 2}


TRAIT_PHP = """<?php
namespace App;

trait Sends {
    public function deliver(string $to): void {}
}

class Campaign {
    use Sends;
}

class Controller {
    public function __construct(private readonly Campaign $campaign) {}

    public function run(string $to): void {
        $this->campaign->deliver($to);
    }
}
"""


def test_a_trait_method_is_recorded_against_the_trait_that_holds_its_body(project, monkeypatch):
    """PHPStan's declaring class for a trait method is the class using it, whose file holds no
    such body; the adapter finds no node there and the edge is lost. The trait is the owner."""
    monkeypatch.setenv(ps._TIMEOUT_ENV, "300")
    (project / "src" / "App.php").write_text(TRAIT_PHP)
    line = next(str(n) for n, text in enumerate(TRAIT_PHP.splitlines(), 1) if "->deliver(" in text)

    path = str(project / "src/App.php")
    result = ps.PhpStanSource().resolve(
        str(project), [CallSite("n1", "$this->campaign", "deliver", path, line)]
    )

    assert [(t.class_fqn, t.method, t.via) for t in result.targets] == [
        ("App\\Sends", "deliver", ps.VIA_CLASS)
    ]


VENDOR_LIB_PHP = """<?php
namespace Acme;

class Lib {
    public function doIt(string $k): string { return $k; }
}
"""

VENDOR_CALLER_PHP = """<?php
namespace App;

use Acme\\Lib;

class Emails { public function send(string $to): void {} }

class Controller {
    public function __construct(private readonly Lib $lib, private readonly Emails $emails) {}

    public function run(string $k): void {
        $this->lib->doIt($k);
        $this->emails->send($k);
    }
}
"""


def test_a_vendor_target_becomes_one_stub_node_and_one_edge_when_the_flag_is_on(
    project, tmp_path, monkeypatch
):
    """`Acme\\Lib` lives under `vendor/`, `App\\Emails` is project code outside the graph.

    Only the first is stubbed: the second stays `target_not_in_graph`, which is the rule that
    keeps a `Tests\\` class excluded by `.graphifyignore` from turning into a phantom node.
    """
    from graphify_php import resolver

    monkeypatch.setenv(ps._TIMEOUT_ENV, "300")
    monkeypatch.setenv(resolver.EXTERNAL_STUBS_ENV, "1")
    monkeypatch.setenv("GRAPHIFY_PHP_STATUS", str(tmp_path / "status.json"))
    (project / "vendor" / "acme").mkdir(parents=True)
    (project / "vendor" / "acme" / "Lib.php").write_text(VENDOR_LIB_PHP)
    (project / "src" / "App.php").write_text(VENDOR_CALLER_PHP)
    # PHPStan only analyses `src`; the vendor class has to be discoverable without composer.
    (project / "phpstan.neon").write_text(
        NEON.replace("\t\t- src\n", "\t\t- src\n\tscanDirectories:\n\t\t- vendor/acme\n")
    )

    def line_in_fixture(needle: str) -> str:
        for number, text in enumerate(VENDOR_CALLER_PHP.splitlines(), start=1):
            if needle in text:
                return f"L{number}"
        raise AssertionError(f"{needle} is not in the fixture")

    path = str(project / "src/App.php")
    per_file = [
        {
            "nodes": [],
            "edges": [],
            "raw_calls": [
                {
                    "caller_nid": "controller_run",
                    "callee": "doIt",
                    "is_member_call": True,
                    "source_file": path,
                    "source_location": line_in_fixture("$this->lib->doIt("),
                },
                {
                    "caller_nid": "controller_run",
                    "callee": "send",
                    "is_member_call": True,
                    "source_file": path,
                    "source_location": line_in_fixture("$this->emails->send("),
                },
            ],
        }
    ]
    nodes, edges = [], []

    record = resolver.run(
        per_file, nodes, edges, sources=[ps.PhpStanSource()], repo_root=str(project)
    )

    assert record.state == "completed"
    assert record.resolved_sites == 2
    assert record.external_stubbed == 1
    assert record.stub_nodes_added == 1
    assert record.stub_edges_added == 1
    assert record.unresolved[resolver.REASON_NOT_IN_GRAPH] == 1, "App\\Emails is not vendor"
    assert [n["label"] for n in nodes if n.get("external")] == ["Acme\\Lib"]
    assert [(e["source"], e["target"], e["_external_target"]) for e in edges] == [
        ("controller_run", "php_external_acme_lib", "Acme\\Lib::doIt")
    ]
