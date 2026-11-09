"""The typed-parameter collector: `run(Emails $m) { $m->send(); }`.

A parameter's declared type binds the local in the method's own scope. The tests here are
mostly about the ways that binding must NOT survive: a second write, a closure that shadows
or captures the name, a type that names no class. A binding that outlives any of them is a
wrong edge, and a wrong edge is worse than a missing one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.ports import CallSite, Confidence  # noqa: E402
from graphify_php.sources.tree_sitter_source import Reason, TreeSitterSource  # noqa: E402
from graphify_php.syntax.collectors import (  # noqa: E402
    COLLECTORS,
    LocalConstructionCollector,
    LocalRebindCollector,
    TypedParameterCollector,
    by_node_kind,
)
from graphify_php.syntax.file_scan import scan_source  # noqa: E402

EMAILS = "App\\Service\\Messages\\Emails"


def only_method(php: str, name: str = "run"):
    facts = scan_source("fake.php", php.encode("utf-8"))
    assert len(facts.classes) == 1
    by_name = {m.name: m for m in facts.classes[0].methods}
    return facts.classes[0], by_name[name]


# --- what binds ------------------------------------------------------------------------


def test_typed_method_parameter_binds_the_local_as_extracted():
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails $m): void { $m->send(); }
}
""")
    fact = run.locals["m"]
    assert fact.fqn == EMAILS
    assert fact.confidence is Confidence.EXTRACTED
    assert fact.via == "typed-parameter"
    assert run.refusals == {}


def test_nullable_parameter_with_a_default_binds():
    """`?Emails $m = null` still reaches `Emails::send` whenever the call happens at all."""
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(?Emails $m = null): void { $m?->send(); }
}
""")
    assert run.locals["m"].fqn == EMAILS


def test_by_reference_parameter_binds():
    """`Emails &$m`: the declared type holds at entry, and any write inside is a revocation."""
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails &$m): void { $m->send(); }
}
""")
    assert run.locals["m"].fqn == EMAILS


def test_parameter_type_is_resolved_through_the_imports():
    _, run = only_method(r"""<?php
namespace App\Cron;
use App\Service\Messages\Emails as Mailer;
class C {
    public function run(Mailer $m, Emails $e, \Other\Thing $t): void {}
}
""")
    assert run.locals["m"].fqn == EMAILS
    # Never imported under that name, so it is relative to this file's namespace.
    assert run.locals["e"].fqn == "App\\Cron\\Emails"
    assert run.locals["t"].fqn == "Other\\Thing"


def test_intersection_parameter_keeps_every_member():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Alpha&Beta $x): void { $x->go(); }
}
""")
    assert run.locals["x"].members == ("App\\Alpha", "App\\Beta")


def test_two_methods_keep_separate_parameter_types():
    scope, _ = only_method(
        r"""<?php
namespace App;
class C {
    public function one(Alpha $x): void { $x->go(); }
    public function two(Beta $x): void { $x->go(); }
}
""",
        "one",
    )
    by_name = {m.name: m for m in scope.methods}
    assert by_name["one"].locals["x"].fqn == "App\\Alpha"
    assert by_name["two"].locals["x"].fqn == "App\\Beta"


def test_plain_constructor_parameter_binds_as_a_local_too():
    """`__construct(Emails $m)` is an ordinary parameter: `$m->init()` in the body is provable."""
    scope, ctor = only_method(
        r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    private $mailer;
    public function __construct(Emails $m) { $this->mailer = $m; $m->init(); }
}
""",
        "__construct",
    )
    assert ctor.locals["m"].fqn == EMAILS
    assert scope.properties["mailer"].fqn == EMAILS


# --- what does not bind ----------------------------------------------------------------


def test_variadic_parameter_records_nothing():
    """`Emails ...$m` is an array inside the method, and an array owns no methods."""
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails ...$m): void {}
}
""")
    assert "m" not in run.locals


def test_union_parameter_is_refused_with_the_union_reason():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Foo|Bar $u): void { $u->go(); }
}
""")
    assert "u" not in run.locals
    assert run.refusals["u"] == "union-type"


def test_scalar_and_relative_parameter_types_are_not_call_targets():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(
        int $i, string $s, array $a, callable $c, iterable $it, object $o, mixed $m,
        self $me, static $st, $untyped
    ): void {}
}
""")
    assert run.locals == {}
    assert run.refusals == {}


def test_promoted_parameter_is_not_bound_as_a_local_a_second_time():
    """The promoted collector owns `private Emails $m`; this one leaves the local alone."""
    scope, ctor = only_method(
        r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function __construct(private Emails $m) { $m->init(); }
}
""",
        "__construct",
    )
    assert scope.properties["m"].via == "promoted-parameter"
    assert "m" not in ctor.locals


# --- revocation: a parameter written again is no longer its declared type ----------------


def test_reassigned_parameter_loses_its_binding():
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails $m): void {
        $m = $this->pick();
        $m->send();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_parameter_rebound_by_new_is_refused_not_retyped():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $m = new Cache();
        $m->get();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_parameter_rebound_by_foreach_is_refused():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m, array $all): void {
        foreach ($all as $m) { $m->get(); }
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_parameter_rebound_by_destructuring_or_catch_is_refused():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $a, Emails $b): void {
        [$a, $other] = $this->pair();
        try {} catch (\Throwable $b) {}
    }
}
""")
    assert run.refusals == {"a": "reassigned-local", "b": "reassigned-local"}
    assert run.locals == {}


def test_parameter_written_by_reference_inside_a_closure_use_is_refused():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $f = function () use (&$m) { $m = null; };
        $m->send();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


# --- closures and arrow functions --------------------------------------------------------


def test_closure_parameter_does_not_bind_in_the_enclosing_method():
    """The closure's `$m` is its own; the method scope that answers the lookup never learns it."""
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(): void {
        $f = function (Emails $m) { $m->send(); };
    }
}
""")
    assert run.locals == {}


def test_arrow_function_parameter_does_not_bind_in_the_enclosing_method():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(): void {
        $f = fn (Emails $m) => $m->send();
    }
}
""")
    assert run.locals == {}


def test_closure_parameter_shadowing_a_method_parameter_revokes_it():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $f = function (Cache $m) { $m->get(); };
        $m->send();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_arrow_function_parameter_shadowing_a_method_parameter_revokes_it():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $f = fn (Cache $m) => $m->get();
        $m->send();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_closure_use_import_revokes_the_parameter():
    """`use ($m)` is a binding in the closure; without closure scopes the method cannot keep it."""
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $f = function () use ($m) { $m->send(); };
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


def test_arrow_function_reading_a_parameter_keeps_the_binding():
    """An arrow function captures by value and cannot write the outer `$m`: the type holds."""
    _, run = only_method(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails $m): void {
        $f = fn () => $m->send();
    }
}
""")
    assert run.locals["m"].fqn == EMAILS


def test_assignment_inside_an_arrow_function_still_revokes():
    _, run = only_method(r"""<?php
namespace App;
class C {
    public function run(Emails $m): void {
        $f = fn () => $m = null;
        $m->send();
    }
}
""")
    assert "m" not in run.locals
    assert run.refusals["m"] == "reassigned-local"


# --- the registry ------------------------------------------------------------------------


def test_typed_parameter_collector_is_registered_on_the_method_declaration():
    registry = by_node_kind()
    kinds = [type(c).__name__ for c in registry["method_declaration"]]
    assert kinds == ["ConstructorAssignmentCollector", "TypedParameterCollector"]
    assert any(isinstance(c, TypedParameterCollector) for c in COLLECTORS)


def test_typed_parameter_binding_is_weaker_than_the_write_that_revokes_it():
    """`refuse_local` drops a fact of equal or weaker rank: a declared type must be droppable
    by a later assignment, which is why a declaration ranks below the `new` collector here."""
    assert TypedParameterCollector.precedence > LocalConstructionCollector.precedence
    assert TypedParameterCollector.precedence > LocalRebindCollector("x").precedence


# --- end to end through the source ------------------------------------------------------


def _declare(tmp_path: Path, namespace: str, classes: dict) -> None:
    body = "\n\n".join(
        "class {}\n{{\n{}\n}}".format(
            cls, "\n".join(f"    public function {m}() {{}}" for m in methods)
        )
        for cls, methods in classes.items()
    )
    (tmp_path / (namespace.replace("\\", "_") + ".php")).write_text(
        f"<?php\n\nnamespace {namespace};\n\n{body}\n", encoding="utf-8"
    )


def _site(php: str, needle: str, receiver: str, method: str) -> CallSite:
    lines = [i + 1 for i, text in enumerate(php.splitlines()) if needle in text]
    assert len(lines) == 1
    return CallSite(
        caller_nid="nid:1",
        receiver=receiver,
        method=method,
        source_file="Subject.php",
        source_location=f"L{lines[0]}",
    )


def _resolve(tmp_path: Path, php: str, *sites: CallSite):
    (tmp_path / "Subject.php").write_text(php, encoding="utf-8")
    return TreeSitterSource().resolve(str(tmp_path), list(sites))


def test_source_resolves_a_call_on_a_typed_parameter(tmp_path):
    _declare(tmp_path, "App\\Service\\Messages", {"Emails": ["send"]})
    php = r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails $m): void {
        $m->send();
    }
}
"""
    result = _resolve(tmp_path, php, _site(php, "$m->send()", "$m", "send"))
    target = result.targets[0]
    assert (target.class_fqn, target.confidence, target.via) == (
        EMAILS,
        Confidence.EXTRACTED,
        "tree_sitter:typed-parameter",
    )


def test_source_refuses_a_parameter_rewritten_before_the_call(tmp_path):
    _declare(tmp_path, "App\\Service\\Messages", {"Emails": ["send"]})
    php = r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(Emails $m): void {
        $m = $this->pick();
        $m->send();
    }
}
"""
    result = _resolve(tmp_path, php, _site(php, "$m->send()", "$m", "send"))
    assert result.targets == []
    assert result.unresolved == {Reason.REASSIGNED_LOCAL: 1}


def test_source_refuses_a_call_inside_a_closure_on_the_closures_own_parameter(tmp_path):
    _declare(tmp_path, "App\\Service\\Messages", {"Emails": ["send"]})
    php = r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(): void {
        $f = function (Emails $m) { $m->send(); };
    }
}
"""
    result = _resolve(tmp_path, php, _site(php, "$m->send()", "$m", "send"))
    assert result.targets == []
    assert result.unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 1}


def test_source_refuses_a_parameter_whose_class_does_not_declare_the_method(tmp_path):
    """The class is in the site's own file, so it is fully read and can be disproved."""
    php = r"""<?php
namespace App;
class Emails { public function send(): void {} }
class C {
    public function run(Emails $m): void {
        $m->get();
    }
}
"""
    result = _resolve(tmp_path, php, _site(php, "$m->get()", "$m", "get"))
    assert result.targets == []
    assert result.unresolved == {Reason.METHOD_NOT_DECLARED: 1}
