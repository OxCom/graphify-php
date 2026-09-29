"""The `CallTargetSource`: what it resolves, and what it refuses under which reason.

Every refusal test asserts two things together — that nothing was emitted, and that the reason
was counted. Asserting only the first would pass for a source that silently dropped the site,
and a silent drop is the failure mode the reason vocabulary exists to make visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.ports import CallSite, CallTargetSource, Confidence   # noqa: E402
from graphify_php.sources.tree_sitter_source import (                   # noqa: E402
    Reason,
    TreeSitterSource,
    classify_receiver,
    line_of,
)

SOURCE = TreeSitterSource()

# graphify's PHP branch never fills `receiver` (engine.py:6152-6157, read at engine.py:6403),
# so the shape that actually arrives in production is an empty one. Every test here runs in
# both modes: a suite that only ever supplied a receiver is what let that go unnoticed.
GRAPHIFY_SHAPE = "receiver-empty"
MODES = ["receiver-supplied", GRAPHIFY_SHAPE]

_send_receiver = True


@pytest.fixture(autouse=True, params=MODES)
def receiver_mode(request):
    """Run every test twice, once with the receiver graphify would send if it filled it in."""
    global _send_receiver
    _send_receiver = request.param != GRAPHIFY_SHAPE
    yield
    _send_receiver = True


def write(tmp_path: Path, php: str, name: str = "Subject.php") -> str:
    path = tmp_path / name
    path.write_text(php, encoding="utf-8")
    return name


def declare(tmp_path: Path, namespace: str, classes: dict, name: str | None = None) -> None:
    """Write a file declaring the target classes a test expects an edge to.

    Needed because a target is only emitted once the resolved class is known to declare the
    method. A test whose target class exists nowhere was asserting an edge to a member that
    does not exist, which is the failure this check was added to stop.
    """
    body = "\n\n".join(
        "class %s\n{\n%s\n}" % (
            cls,
            "\n".join("    public function %s() {}" % m for m in methods) or "",
        )
        for cls, methods in classes.items()
    )
    target = tmp_path / (name or (namespace.replace("\\", "_") + ".php"))
    target.write_text("<?php\n\nnamespace %s;\n\n%s\n" % (namespace, body), encoding="utf-8")


def line_with(php: str, needle: str) -> int:
    lines = php.splitlines()
    matches = [i + 1 for i, text in enumerate(lines) if needle in text]
    assert len(matches) == 1, f"{needle!r} must appear on exactly one line, found {matches}"
    return matches[0]


def call(receiver: str, method: str, line: int, source_file: str = "Subject.php") -> CallSite:
    """A call site in whichever shape this run is exercising.

    `source_location` is written as `L<n>`, the format graphify actually emits
    (`engine.py:6402` builds it as `f"L{node.start_point[0] + 1}"`).
    """
    return CallSite(
        caller_nid="nid:1",
        receiver=receiver if _send_receiver else "",
        method=method,
        source_file=source_file,
        source_location=f"L{line}",
    )


def site(php: str, needle: str, receiver: str, method: str, source_file: str = "Subject.php") -> CallSite:
    """Build a call site whose location is the line `needle` appears on.

    The line is searched for rather than written down so that editing the fixture above a call
    cannot quietly re-point the site at another method.
    """
    return call(receiver, method, line_with(php, needle), source_file)


def resolve(tmp_path: Path, php: str, *sites: CallSite):
    write(tmp_path, php)
    return SOURCE.resolve(str(tmp_path), list(sites))


def refuses(tmp_path: Path, php: str, call: CallSite, reason: str):
    result = resolve(tmp_path, php, call)
    assert result.targets == [], f"expected no edge, got {result.targets}"
    assert result.unresolved == {reason: 1}


# --- the contract ----------------------------------------------------------------------


def test_source_satisfies_the_port():
    assert isinstance(SOURCE, CallTargetSource)
    assert SOURCE.name == "tree_sitter"


def test_available_needs_no_vendor_directory(tmp_path):
    ok, why = SOURCE.available(str(tmp_path))
    assert ok is True and why == ""


# --- the reference case ----------------------------------------------------------------

REFERENCE = r"""<?php
namespace App\Cron\Jobs\TimeTracking\Reminder;

use App\Service\Messages\Emails;

class SubmitWeek extends AbstractReminderCronJob {
    public function __construct(private readonly Emails $emails) {}

    public function run(): void {
        $this->emails->reminderSubmitWeek($user, $date);
    }
}
"""


def test_reference_application_case(tmp_path):
    declare(tmp_path, "App\\Service\\Messages", {"Emails": ["reminderSubmitWeek", "reminderSubmitMonth", "send"]})
    subject = site(REFERENCE, "reminderSubmitWeek", "$this->emails", "reminderSubmitWeek")
    result = resolve(tmp_path, REFERENCE, subject)

    assert result.unresolved == {}
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target.class_fqn == "App\\Service\\Messages\\Emails"
    assert target.method == "reminderSubmitWeek"
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:promoted-parameter"
    assert target.site is subject
    assert result.files_seen == 1


def test_reference_case_does_not_fall_back_to_the_files_own_namespace(tmp_path):
    """`App\\Cron\\...\\Emails` is the answer the import exists to prevent."""
    declare(tmp_path, "App\\Service\\Messages", {"Emails": ["reminderSubmitWeek", "reminderSubmitMonth", "send"]})
    result = resolve(tmp_path, REFERENCE, site(REFERENCE, "reminderSubmitWeek", "$this->emails", "reminderSubmitWeek"))
    assert "Cron" not in result.targets[0].class_fqn


# --- one per type source ---------------------------------------------------------------


def test_typed_property_declaration_resolves_extracted(tmp_path):
    declare(tmp_path, "App\\Service", {"Legacy": ["touch"]})
    php = r"""<?php
namespace App;
use App\Service\Legacy;
class C {
    private readonly Legacy $legacy;
    public function run(): void { $this->legacy->touch(); }
}
"""
    result = resolve(tmp_path, php, site(php, "->touch()", "$this->legacy", "touch"))
    target = result.targets[0]
    assert (target.class_fqn, target.confidence, target.via) == (
        "App\\Service\\Legacy", Confidence.EXTRACTED, "tree_sitter:typed-property",
    )


def test_constructor_assignment_resolves_inferred(tmp_path):
    declare(tmp_path, "App\\Service", {"Legacy": ["touch"]})
    php = r"""<?php
namespace App;
use App\Service\Legacy;
class C {
    private $legacy;
    public function __construct(Legacy $x) { $this->legacy = $x; }
    public function run(): void { $this->legacy->touch(); }
}
"""
    result = resolve(tmp_path, php, site(php, "->touch()", "$this->legacy", "touch"))
    target = result.targets[0]
    assert (target.class_fqn, target.confidence, target.via) == (
        "App\\Service\\Legacy", Confidence.INFERRED, "tree_sitter:constructor-assignment",
    )


def test_new_binding_resolves_inferred(tmp_path):
    declare(tmp_path, "App\\Service\\Messages", {"Emails": ["reminderSubmitWeek", "reminderSubmitMonth", "send"]})
    php = r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(): void {
        $mailer = new Emails();
        $mailer->send();
    }
}
"""
    result = resolve(tmp_path, php, site(php, "$mailer->send()", "$mailer", "send"))
    target = result.targets[0]
    assert (target.class_fqn, target.confidence, target.via) == (
        "App\\Service\\Messages\\Emails", Confidence.INFERRED, "tree_sitter:new-binding",
    )


# --- scoping ---------------------------------------------------------------------------


def test_same_property_name_in_two_classes_resolves_per_class(tmp_path):
    declare(tmp_path, "App", {"Alpha": ["find", "go", "one"], "Beta": ["find", "go", "two"]})
    php = r"""<?php
namespace App;
class A {
    private Alpha $repo;
    public function run(): void { $this->repo->find(); }
}
class B {
    private Beta $repo;
    public function run(): void { $this->repo->find(); }
}
"""
    lines = php.splitlines()
    calls = [
        call("$this->repo", "find", i + 1)
        for i, text in enumerate(lines) if "$this->repo->find()" in text
    ]
    assert len(calls) == 2
    result = resolve(tmp_path, php, *calls)
    assert sorted(t.class_fqn for t in result.targets) == ["App\\Alpha", "App\\Beta"]


def test_same_local_name_in_two_methods_resolves_per_method(tmp_path):
    declare(tmp_path, "App", {"Alpha": ["find", "go", "one"], "Beta": ["find", "go", "two"]})
    php = r"""<?php
namespace App;
class C {
    public function one(): void {
        $service = new Alpha();
        $service->go();
    }
    public function two(): void {
        $service = new Beta();
        $service->go();
    }
}
"""
    lines = php.splitlines()
    calls = [
        call("$service", "go", i + 1)
        for i, text in enumerate(lines) if "$service->go()" in text
    ]
    assert len(calls) == 2
    result = resolve(tmp_path, php, *calls)
    assert sorted(t.class_fqn for t in result.targets) == ["App\\Alpha", "App\\Beta"]


def test_a_local_does_not_leak_out_of_its_method(tmp_path):
    php = r"""<?php
namespace App;
class C {
    public function one(): void { $service = new Alpha(); }
    public function two(): void { $service->go(); }
}
"""
    refuses(
        tmp_path, php,
        site(php, "$service->go()", "$service", "go"),
        Reason.UNKNOWN_RECEIVER_TYPE,
    )


# --- one per refusal reason ------------------------------------------------------------

DYNAMIC = r"""<?php
namespace App;
class C {
    private Emails $emails;
    public function run(): void { $this->$name->send(); }
}
"""


def test_refuses_dynamic_property_name(tmp_path):
    refuses(tmp_path, DYNAMIC, site(DYNAMIC, "$this->$name", "$this->$name", "send"), Reason.DYNAMIC_NAME)


def test_refuses_dynamic_method_name(tmp_path):
    php = r"""<?php
namespace App;
class C {
    private Emails $emails;
    public function run(): void { $this->emails->$method(); }
}
"""
    refuses(tmp_path, php, site(php, "->$method()", "$this->emails", "$method"), Reason.DYNAMIC_NAME)


def test_refuses_a_chained_call(tmp_path):
    php = r"""<?php
namespace App;
class C {
    private Emails $a;
    public function run(): void { $this->a->b()->c(); }
}
"""
    refuses(tmp_path, php, site(php, "->b()->c()", "$this->a->b()", "c"), Reason.CHAINED_CALL)


def test_refuses_nested_property_access(tmp_path):
    php = r"""<?php
namespace App;
class C {
    private Emails $a;
    public function run(): void { $this->a->b->c(); }
}
"""
    refuses(tmp_path, php, site(php, "->b->c()", "$this->a->b", "c"), Reason.NESTED_PROPERTY)


def test_refuses_a_magic_call(tmp_path):
    php = r"""<?php
namespace App;
class C {
    private Emails $emails;
    public function run(): void { $this->emails->__call('x', []); }
}
"""
    refuses(tmp_path, php, site(php, "__call", "$this->emails", "__call"), Reason.INDIRECT_DISPATCH)


def test_refuses_a_service_locator_even_though_its_type_is_known(tmp_path):
    """`$container->get('x')` resolves to a real method and to the wrong edge."""
    php = r"""<?php
namespace App;
use Psr\Container\ContainerInterface;
class C {
    public function __construct(private ContainerInterface $container) {}
    public function run(): void { $this->container->get('mailer'); }
}
"""
    refuses(tmp_path, php, site(php, "->get('mailer')", "$this->container", "get"), Reason.INDIRECT_DISPATCH)


def test_refuses_reflection(tmp_path):
    php = r"""<?php
namespace App;
class C {
    public function run(): void {
        $r = new \ReflectionClass($x);
        $r->newInstance();
    }
}
"""
    refuses(tmp_path, php, site(php, "$r->newInstance()", "$r", "newInstance"), Reason.INDIRECT_DISPATCH)


def test_refuses_a_local_reassigned_after_its_binding(tmp_path):
    php = r"""<?php
namespace App;
class C {
    public function run(): void {
        $m = new Emails();
        $m = $this->pick();
        $m->send();
    }
}
"""
    refuses(tmp_path, php, site(php, "$m->send()", "$m", "send"), Reason.REASSIGNED_LOCAL)


def test_refuses_a_union_typed_property(tmp_path):
    php = r"""<?php
namespace App;
class C {
    private Alpha|Beta $either;
    public function run(): void { $this->either->go(); }
}
"""
    refuses(tmp_path, php, site(php, "->go()", "$this->either", "go"), Reason.UNION_TYPE)


def test_refuses_this_itself_because_the_hierarchy_is_not_known(tmp_path):
    php = r"""<?php
namespace App;
class C extends Base {
    public function run(): void { $this->inherited(); }
}
"""
    refuses(tmp_path, php, site(php, "$this->inherited()", "$this", "inherited"), Reason.UNSUPPORTED_RECEIVER)


def test_refuses_a_property_inherited_from_another_file(tmp_path):
    """The name-only match this package exists to not make."""
    php = r"""<?php
namespace App;
class C extends Base {
    public function run(): void { $this->emails->send(); }
}
"""
    refuses(tmp_path, php, site(php, "->send()", "$this->emails", "send"), Reason.UNKNOWN_RECEIVER_TYPE)


def test_refuses_a_site_it_cannot_place_in_a_class(tmp_path):
    php = r"""<?php
namespace App;
function loose() { $x->go(); }
class C { private Emails $emails; }
"""
    refuses(tmp_path, php, site(php, "$x->go()", "$x", "go"), Reason.UNLOCATABLE_SITE)


def test_refuses_a_site_with_no_line_number(tmp_path):
    write(tmp_path, REFERENCE)
    site_without_line = CallSite("nid:1", "$this->emails", "reminderSubmitWeek", "Subject.php", "")
    result = SOURCE.resolve(str(tmp_path), [site_without_line])
    assert result.targets == []
    assert result.unresolved == {Reason.UNLOCATABLE_SITE: 1}


def test_counts_a_file_it_cannot_read(tmp_path):
    missing = CallSite("nid:1", "$this->emails", "send", "Missing.php", "L3")
    result = SOURCE.resolve(str(tmp_path), [missing])
    assert result.targets == []
    assert result.unresolved == {Reason.UNREADABLE_FILE: 1}
    assert result.files_seen == 0


# --- counting --------------------------------------------------------------------------


def test_reasons_accumulate_per_reason_not_per_site(tmp_path):
    php = r"""<?php
namespace App;
class C extends Base {
    public function run(): void {
        $this->one->go();
        $this->two->go();
    }
}
"""
    lines = php.splitlines()
    calls = [
        call(text.strip().split("->go")[0], "go", i + 1)
        for i, text in enumerate(lines) if "->go();" in text
    ]
    assert len(calls) == 2
    result = resolve(tmp_path, php, *calls)
    assert result.unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 2}


def test_every_reason_constant_is_a_distinct_fixed_string():
    reasons = [v for k, v in vars(Reason).items() if k.isupper()]
    assert len(reasons) == len(set(reasons))
    assert all(isinstance(r, str) and r == r.lower() and " " not in r for r in reasons)


# --- receiver classification and location parsing ---------------------------------------


@pytest.mark.parametrize(
    "receiver, kind, key",
    [
        ("$this->emails", "property", "emails"),
        ("$this -> emails", "property", "emails"),
        ("$mailer", "local", "mailer"),
        ("$this", "self", ""),
    ],
)
def test_classify_accepts_the_two_shapes_it_handles(receiver, kind, key):
    assert classify_receiver(receiver)[:2] == (kind, key)


@pytest.mark.parametrize(
    "receiver, reason",
    [
        ("$this->$prop", Reason.DYNAMIC_NAME),
        ("$$var", Reason.DYNAMIC_NAME),
        ("$this->a->b()", Reason.CHAINED_CALL),
        ("$this->a->b", Reason.NESTED_PROPERTY),
        ("$obj->prop", Reason.NESTED_PROPERTY),
        ("static", Reason.STATIC_BINDING),
        ("static::factory", Reason.STATIC_BINDING),
        ("self::$instance", Reason.UNSUPPORTED_RECEIVER),
        ("Foo::$bar", Reason.UNSUPPORTED_RECEIVER),
        ("", Reason.UNSUPPORTED_RECEIVER),
        ("$array['key']", Reason.UNSUPPORTED_RECEIVER),
    ],
)
def test_classify_refuses_with_the_right_reason(receiver, reason):
    kind, _, why = classify_receiver(receiver)
    assert kind is None and why == reason


@pytest.mark.parametrize(
    "location, expected",
    [
        ("12", 12),
        ("12:4", 12),
        ("src/x.php:12:4", 12),
        ("src/App2/Thing.php:12", 12),
        ("12-30", 12),
        ("", None),
        ("nowhere", None),
    ],
)
def test_line_of_reads_the_shapes_the_field_takes(location, expected):
    assert line_of(location) == expected


# --- the production shape: graphify sends no receiver -------------------------------------


def test_the_shape_graphify_actually_sends(tmp_path):
    """The regression this section exists for.

    `extractors/engine.py:6152-6157` sets `is_member_call` and the callee name for PHP and
    never assigns `member_receiver`, so `engine.py:6403` writes `receiver` as None and
    `engine.py:6402` writes the location as `L<line>`. This is that exact record.
    """
    declare(tmp_path, "App\\Service\\Messages", {"Emails": ["reminderSubmitWeek", "reminderSubmitMonth", "send"]})
    write(tmp_path, REFERENCE)
    raw = CallSite(
        caller_nid="n1",
        receiver="",
        method="reminderSubmitWeek",
        source_file="Subject.php",
        source_location=f"L{line_with(REFERENCE, 'reminderSubmitWeek')}",
    )
    result = SOURCE.resolve(str(tmp_path), [raw])

    assert result.unresolved == {}
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target.class_fqn == "App\\Service\\Messages\\Emails"
    assert target.method == "reminderSubmitWeek"
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:promoted-parameter"


def test_recovered_receiver_reads_a_nullsafe_call(tmp_path):
    declare(tmp_path, "App\\Service\\Messages", {"Emails": ["reminderSubmitWeek", "reminderSubmitMonth", "send"]})
    php = r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function __construct(private Emails $emails) {}
    public function run(): void { $this->emails?->send(); }
}
"""
    result = resolve(tmp_path, php, call("", "send", line_with(php, "?->send()")))
    assert result.targets[0].class_fqn == "App\\Service\\Messages\\Emails"


def test_recovered_receiver_picks_the_right_call_among_different_methods(tmp_path):
    """Two calls on one line, different method names: no ambiguity, and no crossed wires."""
    declare(tmp_path, "App", {"Alpha": ["find", "go", "one"], "Beta": ["find", "go", "two"]})
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    private Beta $b;
    public function run(): void { $this->a->one(); $this->b->two(); }
}
"""
    line = line_with(php, "$this->a->one()")
    result = resolve(tmp_path, php, call("", "one", line), call("", "two", line))
    assert sorted((t.method, t.class_fqn) for t in result.targets) == [
        ("one", "App\\Alpha"), ("two", "App\\Beta"),
    ]


def test_refuses_two_calls_to_the_same_method_on_one_line(tmp_path):
    """`$a->run(); $b->run();` — two receivers, two classes, and `L<line>` cannot separate them."""
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    private Beta $b;
    public function run(): void { $this->a->go(); $this->b->go(); }
}
"""
    result = resolve(tmp_path, php, call("", "go", line_with(php, "$this->a->go()")))
    assert result.targets == []
    assert result.unresolved == {Reason.AMBIGUOUS_SITE: 1}


def test_refuses_a_line_with_no_matching_call(tmp_path):
    """A plain function call is neither a member call nor a scoped one."""
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    public function run(): void { strlen($x); }
}
"""
    result = resolve(tmp_path, php, call("", "strlen", line_with(php, "strlen($x)")))
    assert result.targets == []
    assert result.unresolved == {Reason.CALL_SITE_NOT_FOUND: 1}


def test_a_supplied_receiver_overrides_the_tree(tmp_path):
    """A future graphify that fills the field is used, not second-guessed."""
    declare(tmp_path, "App", {"Alpha": ["find", "go", "one"], "Beta": ["find", "go", "two"]})
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    private Beta $b;
    public function run(): void { $this->a->go(); }
}
"""
    line = line_with(php, "$this->a->go()")
    supplied = CallSite("n1", "$this->b", "go", "Subject.php", f"L{line}")
    write(tmp_path, php)
    result = SOURCE.resolve(str(tmp_path), [supplied])
    assert result.targets[0].class_fqn == "App\\Beta"


def test_override_does_not_mask_ambiguity_it_can_settle(tmp_path):
    """The same two-calls-one-line file resolves once graphify says which receiver it meant."""
    declare(tmp_path, "App", {"Alpha": ["find", "go", "one"], "Beta": ["find", "go", "two"]})
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    private Beta $b;
    public function run(): void { $this->a->go(); $this->b->go(); }
}
"""
    line = line_with(php, "$this->a->go()")
    write(tmp_path, php)
    result = SOURCE.resolve(str(tmp_path), [CallSite("n1", "$this->b", "go", "Subject.php", f"L{line}")])
    assert result.targets[0].class_fqn == "App\\Beta"


@pytest.mark.parametrize("location, expected", [("L6", 6), ("L12:4", 12), ("L1", 1)])
def test_line_of_reads_graphifys_own_location_format(location, expected):
    assert line_of(location) == expected


# --- the method must exist, not just the class -------------------------------------------

# The shape of `eval/fixtures/dynamic.php:39`: the receiver's type resolves correctly and the
# method is answered by `__call`, so there is no declaration to point an edge at. Both classes
# live in one file so the refusal and the positive control share every other condition.
MAGIC_FIXTURE = r"""<?php
namespace Eval\Dynamic;

final class Magic
{
    public function __call(string $name, array $args)
    {
        return null;
    }
}

final class Real
{
    public function knownMethod(string $user): void
    {
    }
}

final class Caller
{
    public function __construct(
        private readonly Magic $magic,
        private readonly Real $real,
    ) {
    }

    public function magicCall(string $user): void
    {
        $this->magic->anythingAtAll($user);
    }

    public function realCall(string $user): void
    {
        $this->real->knownMethod($user);
    }
}
"""


def test_refuses_a_method_reached_through_magic_call(tmp_path):
    """dynamic.php:39 — right class, no such member. The edge must not be emitted."""
    result = resolve(tmp_path, MAGIC_FIXTURE,
                     call("", "anythingAtAll", line_with(MAGIC_FIXTURE, "anythingAtAll($user);")))
    assert result.targets == []
    assert result.unresolved == {Reason.INDIRECT_DISPATCH: 1}


def test_positive_control_on_the_same_fixture_still_resolves(tmp_path):
    """The check must not pass by refusing everything."""
    result = resolve(tmp_path, MAGIC_FIXTURE,
                     call("", "knownMethod", line_with(MAGIC_FIXTURE, "knownMethod($user);")))
    assert result.unresolved == {}
    assert (result.targets[0].class_fqn, result.targets[0].method) == ("Eval\\Dynamic\\Real", "knownMethod")
    assert result.targets[0].confidence is Confidence.EXTRACTED


def test_refuses_a_method_the_class_does_not_declare(tmp_path):
    """No `__call` anywhere: the class is fully read and simply has no such member."""
    php = r"""<?php
namespace App;
class Plain {
    public function actual(): void {}
}
class C {
    public function __construct(private Plain $p) {}
    public function run(): void { $this->p->typoed(); }
}
"""
    result = resolve(tmp_path, php, call("", "typoed", line_with(php, "->typoed()")))
    assert result.targets == []
    assert result.unresolved == {Reason.METHOD_NOT_DECLARED: 1}


def test_emits_for_a_class_it_never_read_rather_than_claiming_absence(tmp_path):
    """Not read is not the same as not there.

    The target is emitted and `GraphNodeIndex.method_node` settles it against the whole
    corpus, adding no edge when the method is nowhere. Refusing here would drop real edges to
    buy nothing, because that lookup already covers the case.
    """
    php = r"""<?php
namespace App;
use Outside\Vendor\Thing;
class C {
    public function __construct(private Thing $t) {}
    public function run(): void { $this->t->whatever(); }
}
"""
    result = resolve(tmp_path, php, call("", "whatever", line_with(php, "->whatever()")))
    assert result.unresolved == {}
    assert (result.targets[0].class_fqn, result.targets[0].method) == ("Outside\\Vendor\\Thing", "whatever")


def test_method_inherited_from_a_parent_resolves(tmp_path):
    php = r"""<?php
namespace App;
class Base { public function inheritedOne(): void {} }
class Child extends Base {}
class C {
    public function __construct(private Child $c) {}
    public function run(): void { $this->c->inheritedOne(); }
}
"""
    result = resolve(tmp_path, php, call("", "inheritedOne", line_with(php, "->inheritedOne()")))
    assert (result.targets[0].class_fqn, result.targets[0].method) == ("App\\Child", "inheritedOne")


def test_method_from_a_trait_resolves(tmp_path):
    php = r"""<?php
namespace App;
trait Sends { public function dispatch(): void {} }
class Campaign { use Sends; }
class C {
    public function __construct(private Campaign $c) {}
    public function run(): void { $this->c->dispatch(); }
}
"""
    result = resolve(tmp_path, php, call("", "dispatch", line_with(php, "->dispatch()")))
    assert (result.targets[0].class_fqn, result.targets[0].method) == ("App\\Campaign", "dispatch")


def test_method_from_an_implemented_interface_resolves(tmp_path):
    php = r"""<?php
namespace App;
interface Mailer { public function deliver(): void; }
class Smtp implements Mailer { public function deliver(): void {} }
class C {
    public function __construct(private Mailer $m) {}
    public function run(): void { $this->m->deliver(); }
}
"""
    result = resolve(tmp_path, php, call("", "deliver", line_with(php, "->deliver()")))
    assert (result.targets[0].class_fqn, result.targets[0].method) == ("App\\Mailer", "deliver")


def test_an_unread_ancestor_is_emitted_not_disproved(tmp_path):
    """A gap anywhere in the ancestry means absence was not established, so the target stands."""
    php = r"""<?php
namespace App;
use Outside\Framework\Base;
class Child extends Base { public function own(): void {} }
class C {
    public function __construct(private Child $c) {}
    public function run(): void { $this->c->fromTheFramework(); }
}
"""
    result = resolve(tmp_path, php, call("", "fromTheFramework", line_with(php, "->fromTheFramework()")))
    assert result.unresolved == {}
    assert result.targets[0].class_fqn == "App\\Child"


# --- scoped calls: self::, parent::, static::, Class:: -------------------------------------

# graphify records the SCOPE as the callee for these (`engine.py:6147-6152`), never the method,
# and `is_member_call` stays false. So a scoped site never carries a receiver from anywhere and
# these fixtures always send an empty one, in both modes.


def scoped(method: str, line: int) -> CallSite:
    return CallSite("nid:1", "", method, "Subject.php", f"L{line}")


SCOPES = r"""<?php
namespace App\Jobs;

use App\Support\Helper;

class Base
{
    public static function inherited(): void {}
}

class Job extends Base
{
    public function run(): void
    {
        self::prepare();
        parent::inherited();
        static::make();
        Helper::format();
    }

    public static function prepare(): void {}

    public static function make(): void {}
}
"""


def test_self_scope_resolves_to_the_enclosing_class(tmp_path):
    result = resolve(tmp_path, SCOPES, scoped("prepare", line_with(SCOPES, "self::prepare()")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Jobs\\Job", "prepare")
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:self-scope"


def test_parent_scope_resolves_to_the_extended_class(tmp_path):
    result = resolve(tmp_path, SCOPES, scoped("inherited", line_with(SCOPES, "parent::inherited()")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Jobs\\Base", "inherited")
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:parent-scope"


def test_parent_in_another_file_is_emitted_not_refused(tmp_path):
    """The parent's body is elsewhere, so the member cannot be checked — the identity can."""
    php = r"""<?php
namespace App\Jobs;
use App\Cron\AbstractReminderCronJob;
class SubmitWeek extends AbstractReminderCronJob {
    public function run(): void { parent::configure(); }
}
"""
    result = resolve(tmp_path, php, scoped("configure", line_with(php, "parent::configure()")))
    assert result.unresolved == {}
    target = result.targets[0]
    assert target.class_fqn == "App\\Cron\\AbstractReminderCronJob"
    assert target.confidence is Confidence.EXTRACTED


def test_static_scope_is_inferred_and_enumerates_no_subclass(tmp_path):
    """One edge to the lexical class, not one per subclass."""
    php = r"""<?php
namespace App;
class Job {
    public function run(): void { static::make(); }
    public static function make(): void {}
}
class SubA extends Job { public static function make(): void {} }
class SubB extends Job { public static function make(): void {} }
"""
    result = resolve(tmp_path, php, scoped("make", line_with(php, "static::make()")))
    assert len(result.targets) == 1, "a subclass was enumerated"
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Job", "make")
    assert target.confidence is Confidence.INFERRED
    assert target.via == "tree_sitter:static-binding"


def test_explicit_class_scope_resolves_through_the_use_alias(tmp_path):
    declare(tmp_path, "App\\Support", {"Helper": ["format"]})
    result = resolve(tmp_path, SCOPES, scoped("format", line_with(SCOPES, "Helper::format()")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Support\\Helper", "format")
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:class-scope"


def test_explicit_class_scope_does_not_fall_back_to_the_files_namespace(tmp_path):
    """`App\\Jobs\\Helper` is the answer the `use` line exists to prevent."""
    result = resolve(tmp_path, SCOPES, scoped("format", line_with(SCOPES, "Helper::format()")))
    assert result.targets[0].class_fqn == "App\\Support\\Helper"


def test_absolute_class_scope_is_already_absolute(tmp_path):
    php = r"""<?php
namespace App\Jobs;
class C { public function run(): void { \Other\Thing::go(); } }
"""
    result = resolve(tmp_path, php, scoped("go", line_with(php, "Thing::go()")))
    assert result.targets[0].class_fqn == "Other\\Thing"


def test_refuses_a_dynamic_scope(tmp_path):
    php = r"""<?php
namespace App;
class C { public function run(): void { $cls::boom(); } }
"""
    result = resolve(tmp_path, php, scoped("boom", line_with(php, "$cls::boom()")))
    assert result.targets == []
    assert result.unresolved == {Reason.DYNAMIC_SCOPE: 1}


def test_refuses_parent_with_no_extends(tmp_path):
    php = r"""<?php
namespace App;
class Orphan { public function run(): void { parent::nothing(); } }
"""
    result = resolve(tmp_path, php, scoped("nothing", line_with(php, "parent::nothing()")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNRESOLVABLE_SCOPE: 1}


def test_self_scope_still_refuses_a_method_the_class_does_not_declare(tmp_path):
    """The existence gate applies to scoped calls too: this class is fully read."""
    php = r"""<?php
namespace App;
class Only { public function run(): void { self::typoed(); } }
"""
    result = resolve(tmp_path, php, scoped("typoed", line_with(php, "self::typoed()")))
    assert result.targets == []
    assert result.unresolved == {Reason.METHOD_NOT_DECLARED: 1}


def test_scoped_positive_control_beside_the_refusals(tmp_path):
    """One fixture, one refusal and one resolution, so refusing everything cannot pass."""
    php = r"""<?php
namespace App;
class Only {
    public function run(): void { self::declared(); $cls::boom(); }
    public static function declared(): void {}
}
"""
    line = line_with(php, "self::declared()")
    result = resolve(tmp_path, php, scoped("declared", line), scoped("boom", line))
    assert [(t.class_fqn, t.method) for t in result.targets] == [("App\\Only", "declared")]
    assert result.unresolved == {Reason.DYNAMIC_SCOPE: 1}


def test_a_member_call_and_a_scoped_call_on_one_line_stay_apart(tmp_path):
    declare(tmp_path, "App", {"Alpha": ["one"], "Helper": ["two"]})
    php = r"""<?php
namespace App;
class C {
    private Alpha $a;
    public function run(): void { $this->a->one(); Helper::two(); }
}
"""
    line = line_with(php, "$this->a->one()")
    result = resolve(tmp_path, php, call("", "one", line), scoped("two", line))
    assert sorted((t.method, t.class_fqn) for t in result.targets) == [
        ("one", "App\\Alpha"), ("two", "App\\Helper"),
    ]


def test_this_resolves_only_to_a_method_the_class_declares_itself(tmp_path):
    """`$this->m()` where the class declares `m`: provable, so it resolves."""
    php = r"""<?php
namespace App;
class Handler {
    public function __invoke(): void { $this->process(); }
    public function process(): void {}
}
"""
    result = resolve(tmp_path, php, call("", "process", line_with(php, "$this->process()")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Handler", "process")
    assert target.confidence is Confidence.EXTRACTED
    assert target.via == "tree_sitter:self-receiver"
