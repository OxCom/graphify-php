"""Writes that are not a plain `$x = ...` must still revoke a local's type binding.

Each construct is tried against both local bindings (a `new` local and a typed parameter). A
binding that survives one of these writes yields a wrong call edge, which is worse than none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.syntax.file_scan import scan_source  # noqa: E402

REASSIGNED = "reassigned-local"
VARIABLE_VARIABLES = "variable-variables"

# (id, statement written after the binding, reason on `$x`)
WRITES = [
    ("coalesce-assign", "$x ??= new Other();", REASSIGNED),
    ("concat-assign", "$x .= 'a';", REASSIGNED),
    ("add-assign", "$x += 1;", REASSIGNED),
    ("global", "global $x;", REASSIGNED),
    ("static", "static $x;", REASSIGNED),
    ("static-init", "static $x = null;", REASSIGNED),
    ("unset", "unset($x);", REASSIGNED),
    ("unset-many", "unset($y, $x);", REASSIGNED),
    ("ref-assign-left", "$x = &$y;", REASSIGNED),
    ("ref-assign-right", "$y = &$x;", REASSIGNED),
    ("postfix-inc", "$x++;", REASSIGNED),
    ("prefix-dec", "--$x;", REASSIGNED),
    ("list-call", "list($x, $y) = $z;", REASSIGNED),
    ("list-short", "[$x, $y] = $z;", REASSIGNED),
    ("list-keyed", "['k' => $x] = $z;", REASSIGNED),
    ("foreach", "foreach ($z as $x) {}", REASSIGNED),
    ("foreach-ref", "foreach ($z as &$x) {}", REASSIGNED),
    ("catch", "try {} catch (\\Exception $x) {}", REASSIGNED),
    ("closure-use-ref", "$f = function () use (&$x) {};", REASSIGNED),
    ("closure-param", "$f = function (Other $x) {};", REASSIGNED),
    ("parse-str-out", "parse_str($s, $x);", REASSIGNED),
    ("dynamic-assign", "$$n = 1;", VARIABLE_VARIABLES),
    ("dynamic-braced", "${'x'} = 1;", VARIABLE_VARIABLES),
    ("dynamic-augmented", "$$n .= 'a';", VARIABLE_VARIABLES),
    ("extract", "extract($arr);", VARIABLE_VARIABLES),
    ("extract-qualified", "\\extract($arr);", VARIABLE_VARIABLES),
    ("extract-flags", "extract($arr, EXTR_OVERWRITE);", VARIABLE_VARIABLES),
    ("parse-str-one-arg", "parse_str($s);", VARIABLE_VARIABLES),
]

# Not writes: reading the name, or calls that merely resemble the constructs above.
NOT_WRITES = [
    ("read-compact", "$a = compact('x');"),
    ("member-extract", "$this->extract($arr);"),
    ("static-extract", "Util::extract($arr);"),
    ("dynamic-read", "$a = $$n;"),
    ("unset-other", "unset($y);"),
    ("augmented-other", "$y ??= 1;"),
    ("global-other", "global $y;"),
    ("parse-str-other-out", "parse_str($s, $y);"),
]


def _new_local(stmt: str) -> str:
    return f"""<?php
namespace App;
class C {{
    public function run(): void {{
        $x = new Foo();
        {stmt}
        $x->m();
    }}
}}
"""


def _typed_param(stmt: str) -> str:
    return f"""<?php
namespace App;
class C {{
    public function run(Foo $x): void {{
        {stmt}
        $x->m();
    }}
}}
"""


SHAPES = [("new-local", _new_local), ("typed-param", _typed_param)]


def _run(php: str):
    facts = scan_source("fake.php", php.encode("utf-8"))
    return {m.name: m for m in facts.classes[0].methods}["run"]


@pytest.mark.parametrize("shape", [s[0] for s in SHAPES])
@pytest.mark.parametrize("case", WRITES, ids=[w[0] for w in WRITES])
def test_write_revokes_the_binding(shape, case):
    build = dict(SHAPES)[shape]
    method = _run(build(case[1]))
    assert "x" not in method.locals
    assert method.refusals["x"] == case[2]


@pytest.mark.parametrize("shape", [s[0] for s in SHAPES])
@pytest.mark.parametrize("case", NOT_WRITES, ids=[w[0] for w in NOT_WRITES])
def test_non_write_keeps_the_binding(shape, case):
    build = dict(SHAPES)[shape]
    method = _run(build(case[1]))
    assert method.locals["x"].fqn == "App\\Foo"
    assert "x" not in method.refusals


def test_extract_before_a_new_binding_still_revokes_it():
    method = _run(r"""<?php
namespace App;
class C {
    public function run(): void {
        extract($arr);
        $x = new Foo();
        $x->m();
    }
}
""")
    assert "x" not in method.locals
    assert method.refusals["x"] == VARIABLE_VARIABLES


def test_variable_variables_revoke_every_local_of_the_method_only():
    facts = scan_source(
        "fake.php",
        rb"""<?php
namespace App;
class C {
    public function run(Foo $p): void {
        $a = new Foo();
        $b = new Bar();
        extract($arr);
    }
    public function other(): void {
        $a = new Foo();
    }
}
""",
    )
    methods = {m.name: m for m in facts.classes[0].methods}
    run, other = methods["run"], methods["other"]
    assert run.locals == {}
    assert {n: run.refusals[n] for n in ("p", "a", "b")} == {
        "p": VARIABLE_VARIABLES,
        "a": VARIABLE_VARIABLES,
        "b": VARIABLE_VARIABLES,
    }
    assert other.locals["a"].fqn == "App\\Foo"
    assert other.refusals == {}


def test_extract_inside_a_closure_revokes_the_enclosing_method_locals():
    method = _run(r"""<?php
namespace App;
class C {
    public function run(): void {
        $x = new Foo();
        $f = function () { extract($arr); };
        $x->m();
    }
}
""")
    assert "x" not in method.locals
