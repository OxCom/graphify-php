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


# --- properties declared up the ancestry --------------------------------------------------


def test_property_declared_in_a_parent_resolves(tmp_path):
    declare(tmp_path, "App\\Mail", {"Emails": ["reminderSubmitMonth"]})
    php = r"""<?php
namespace App\Notify;
use App\Mail\Emails;
abstract class BaseNotifier {
    protected readonly Emails $emails;
    public function __construct(Emails $emails) { $this->emails = $emails; }
}
final class WeeklyNotifier extends BaseNotifier {
    public function notify(string $user): void { $this->emails->reminderSubmitMonth($user); }
}
"""
    result = resolve(tmp_path, php, call("", "reminderSubmitMonth", line_with(php, "reminderSubmitMonth($user);")))
    assert result.unresolved == {}
    assert result.targets[0].class_fqn == "App\\Mail\\Emails"


def test_property_declared_in_a_trait_resolves(tmp_path):
    declare(tmp_path, "App\\Mail", {"Emails": ["send"]})
    php = r"""<?php
namespace App\Notify;
use App\Mail\Emails;
trait SendsMail { protected readonly Emails $emails; }
final class Campaign {
    use SendsMail;
    public function run(): void { $this->emails->send(); }
}
"""
    result = resolve(tmp_path, php, call("", "send", line_with(php, "$this->emails->send()")))
    assert result.targets[0].class_fqn == "App\\Mail\\Emails"


def test_the_nearest_declaration_wins_over_the_ancestors(tmp_path):
    """A subclass narrowing the type must beat the parent's, not depend on walk order."""
    declare(tmp_path, "App", {"Wide": ["go"], "Narrow": ["go"]})
    php = r"""<?php
namespace App;
class Base { protected Wide $client; }
class Child extends Base {
    protected Narrow $client;
    public function run(): void { $this->client->go(); }
}
"""
    result = resolve(tmp_path, php, call("", "go", line_with(php, "$this->client->go()")))
    assert result.targets[0].class_fqn == "App\\Narrow"


def test_a_parent_in_an_unread_file_leaves_the_type_unknown(tmp_path):
    """Not read is not disproved, and it is not a licence to guess either."""
    php = r"""<?php
namespace App;
use Outside\Framework\Base;
class Child extends Base {
    public function run(): void { $this->inheritedProperty->go(); }
}
"""
    result = resolve(tmp_path, php, call("", "go", line_with(php, "->go()")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 1}


def test_a_refusal_declared_in_an_ancestor_is_honoured(tmp_path):
    """The parent's union is still a union when the subclass reads it."""
    php = r"""<?php
namespace App;
class Base { protected Alpha|Beta $either; }
class Child extends Base {
    public function run(): void { $this->either->go(); }
}
"""
    result = resolve(tmp_path, php, call("", "go", line_with(php, "$this->either->go()")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNION_TYPE: 1}


def test_this_method_from_a_trait_resolves(tmp_path):
    """`$this->dispatch()` where the trait supplies `dispatch`."""
    php = r"""<?php
namespace App;
trait SendsMail { public function dispatch(string $u): void {} }
final class Campaign {
    use SendsMail;
    public function run(string $u): void { $this->dispatch($u); }
}
"""
    result = resolve(tmp_path, php, call("", "dispatch", line_with(php, "$this->dispatch($u)")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Campaign", "dispatch")
    assert target.via == "tree_sitter:self-receiver"


# --- intersection types -------------------------------------------------------------------

# `A&B` is not `A|B`. A union is one of two objects and so two candidate targets, which is why
# it is refused. An intersection is one object that is both, so a method declared in only one
# constituent names its target without ambiguity.

INTERSECTION = r"""<?php
namespace App;

class Emails
{
    public function reminderSubmitWeek(string $u): void {}
}

class Loggable
{
    public function log(string $u): void {}
}

class Caller
{
    public function __construct(private readonly Emails&Loggable $both) {}

    public function run(string $u): void
    {
        $this->both->reminderSubmitWeek($u);
    }
}
"""

# One file on purpose. A constituent is only weighed when its declaration was actually read,
# and the index covers the files this run parses for call sites — so a constituent living in a
# file with no call sites is unread, and the last test in this group pins that outcome.


def test_intersection_resolves_to_the_constituent_that_declares_the_method(tmp_path):
    result = resolve(tmp_path, INTERSECTION,
                     call("", "reminderSubmitWeek", line_with(INTERSECTION, "reminderSubmitWeek($u);")))
    assert result.unresolved == {}
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Emails", "reminderSubmitWeek")
    assert target.confidence is Confidence.EXTRACTED
    assert "intersection" in target.via


def test_intersection_picks_the_other_constituent_for_its_own_method(tmp_path):
    """The choice follows the method, so it cannot be a fixed preference for the first name."""
    php = INTERSECTION.replace("$this->both->reminderSubmitWeek($u);", "$this->both->log($u);")
    result = resolve(tmp_path, php, call("", "log", line_with(php, "$this->both->log($u);")))
    assert result.targets[0].class_fqn == "App\\Loggable"


def test_intersection_refuses_when_both_constituents_declare_it(tmp_path):
    """One object, two declarations: the type does not say which body runs."""
    php = INTERSECTION.replace("public function log(string $u): void {}",
                               "public function reminderSubmitWeek(string $u): void {}")
    result = resolve(tmp_path, php,
                     call("", "reminderSubmitWeek", line_with(php, "$this->both->reminderSubmitWeek($u);")))
    assert result.targets == []
    assert result.unresolved == {Reason.AMBIGUOUS_INTERSECTION: 1}


def test_intersection_refuses_a_method_neither_constituent_declares(tmp_path):
    php = INTERSECTION.replace("$this->both->reminderSubmitWeek($u);", "$this->both->typoed($u);")
    result = resolve(tmp_path, php, call("", "typoed", line_with(php, "$this->both->typoed($u);")))
    assert result.targets == []
    assert result.unresolved == {Reason.METHOD_NOT_DECLARED: 1}


def test_intersection_resolves_when_only_the_read_constituent_declares_it(tmp_path):
    """The second constituent is in a file this run never read; the read one settles it."""
    php = INTERSECTION.replace("""class Loggable
{
    public function log(string $u): void {}
}

""", "").replace("Emails&Loggable", "Emails&\\Vendor\\Loggable")
    result = resolve(tmp_path, php,
                     call("", "reminderSubmitWeek", line_with(php, "$this->both->reminderSubmitWeek($u);")))
    assert result.unresolved == {}
    assert result.targets[0].class_fqn == "App\\Emails"


def test_intersection_stays_unresolved_when_no_read_constituent_declares_it(tmp_path):
    """Neither read nor disproved: leave it rather than pick a constituent."""
    php = INTERSECTION.replace("Emails&Loggable", "\\Vendor\\A&\\Vendor\\B")
    result = resolve(tmp_path, php,
                     call("", "reminderSubmitWeek", line_with(php, "$this->both->reminderSubmitWeek($u);")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 1}


def test_a_union_is_still_refused_and_not_treated_as_an_intersection(tmp_path):
    php = INTERSECTION.replace("Emails&Loggable", "Emails|Loggable")
    result = resolve(tmp_path, php,
                     call("", "reminderSubmitWeek", line_with(php, "$this->both->reminderSubmitWeek($u);")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNION_TYPE: 1}


def test_disjunctive_normal_form_type_is_refused_as_a_union(tmp_path):
    """`(A&B)|C` is an alternation at the top, whatever its branches are."""
    php = INTERSECTION.replace("Emails&Loggable", "(Emails&Loggable)|Other")
    result = resolve(tmp_path, php,
                     call("", "reminderSubmitWeek", line_with(php, "$this->both->reminderSubmitWeek($u);")))
    assert result.targets == []
    assert result.unresolved == {Reason.UNION_TYPE: 1}


# --- PHP 8.x shapes that already work, pinned so a later change cannot break them silently --


def test_nullsafe_call_resolves(tmp_path):
    declare(tmp_path, "App\\Service", {"Emails": ["reminderSubmitWeek"]})
    php = r"""<?php
namespace App;
use App\Service\Emails;
class C {
    public function __construct(private readonly ?Emails $emails) {}
    public function run(string $u): void { $this->emails?->reminderSubmitWeek($u); }
}
"""
    result = resolve(tmp_path, php, call("", "reminderSubmitWeek", line_with(php, "?->reminderSubmitWeek")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Service\\Emails", "reminderSubmitWeek")
    assert target.confidence is Confidence.EXTRACTED


def test_enum_method_call_resolves(tmp_path):
    php = r"""<?php
namespace App;
enum Status: string {
    case Draft = 'draft';
    public function label(): string { return 'x'; }
}
class C {
    public function __construct(private readonly Status $status) {}
    public function run(): void { $this->status->label(); }
}
"""
    result = resolve(tmp_path, php, call("", "label", line_with(php, "$this->status->label()")))
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Status", "label")
    assert target.confidence is Confidence.EXTRACTED


def test_extension_class_keeps_its_root_namespace(tmp_path):
    """`\\PDO` is PDO, never `App\\PDO`. The graph drops it later for want of a node."""
    php = r"""<?php
namespace App;
class C {
    public function __construct(private readonly \PDO $db) {}
    public function run(string $sql): void { $this->db->prepare($sql); }
}
"""
    result = resolve(tmp_path, php, call("", "prepare", line_with(php, "$this->db->prepare($sql)")))
    target = result.targets[0]
    assert target.class_fqn == "PDO"
    assert "App" not in target.class_fqn
    assert target.confidence is Confidence.EXTRACTED


def test_first_class_callable_syntax_resolves(tmp_path):
    """`$x->m(...)` makes a Closure; the edge is emitted as a call by choice, see the source."""
    declare(tmp_path, "App\\Service", {"Emails": ["reminderSubmitWeek"]})
    php = r"""<?php
namespace App;
use App\Service\Emails;
class C {
    public function __construct(private readonly Emails $emails) {}
    public function run(): callable { return $this->emails->reminderSubmitWeek(...); }
}
"""
    result = resolve(tmp_path, php, call("", "reminderSubmitWeek", line_with(php, "(...)")))
    assert (result.targets[0].class_fqn, result.targets[0].method) == (
        "App\\Service\\Emails", "reminderSubmitWeek")


def test_named_arguments_resolve(tmp_path):
    declare(tmp_path, "App\\Service", {"Emails": ["reminderSubmitWeek"]})
    php = r"""<?php
namespace App;
use App\Service\Emails;
class C {
    public function __construct(private readonly Emails $emails) {}
    public function run($u, $d): void { $this->emails->reminderSubmitWeek(user: $u, date: $d); }
}
"""
    result = resolve(tmp_path, php, call("", "reminderSubmitWeek", line_with(php, "user: $u")))
    assert (result.targets[0].class_fqn, result.targets[0].method) == (
        "App\\Service\\Emails", "reminderSubmitWeek")


# --- the corpus: declarations from files that hold no call site ----------------------------

# A leaf service class has nothing unresolved in it, so it never appears among `sites`. Without
# a corpus, whether its methods are known depends on whether some unrelated file happens to
# carry a site — which is what made the same intersection resolve or not in the coordinator's
# probe. `corpus` is the files the build already parsed; it is never a filesystem walk.


def write_file(tmp_path: Path, name: str, php: str) -> str:
    (tmp_path / name).write_text(php, encoding="utf-8")
    return name


CONSTITUENT_EMAILS = """<?php

namespace App\\Service;

class Emails
{
    public function reminderSubmitWeek(string $u): void {}
}
"""

CONSTITUENT_LOGGABLE = """<?php

namespace App\\Log;

interface Loggable
{
    public function log(string $m): void;
}
"""

CALLER_WITH_INTERSECTION = """<?php

namespace App;

use App\\Service\\Emails;
use App\\Log\\Loggable;

class Caller
{
    public function __construct(private readonly Emails&Loggable $both) {}

    public function run(string $u): void
    {
        $this->both->reminderSubmitWeek($u);
    }
}
"""


def intersection_site(tmp_path: Path):
    write_file(tmp_path, "Emails.php", CONSTITUENT_EMAILS)
    write_file(tmp_path, "Loggable.php", CONSTITUENT_LOGGABLE)
    write_file(tmp_path, "Caller.php", CALLER_WITH_INTERSECTION)
    line = line_with(CALLER_WITH_INTERSECTION, "$this->both->reminderSubmitWeek($u);")
    return CallSite("nid:1", "", "reminderSubmitWeek", "Caller.php", f"L{line}")


def test_intersection_in_declaration_only_files_resolves_with_a_corpus(tmp_path):
    """The shape that failed the probe: constituents in files with no call sites of their own."""
    site = intersection_site(tmp_path)
    result = SOURCE.resolve(str(tmp_path), [site], corpus=["Emails.php", "Loggable.php"])
    assert result.unresolved == {}
    target = result.targets[0]
    assert (target.class_fqn, target.method) == ("App\\Service\\Emails", "reminderSubmitWeek")
    assert target.confidence is Confidence.EXTRACTED
    assert "intersection" in target.via


def test_the_same_intersection_is_unresolved_without_a_corpus(tmp_path):
    """Pins the difference the corpus makes, so it cannot regress unnoticed."""
    site = intersection_site(tmp_path)
    result = SOURCE.resolve(str(tmp_path), [site])
    assert result.targets == []
    assert result.unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 1}


def test_corpus_turns_an_unknown_owner_into_a_disproof(tmp_path):
    """A wider corpus means more classes are read, so absence becomes provable."""
    write_file(tmp_path, "Plain.php", "<?php\nnamespace App;\nclass Plain { public function actual(): void {} }\n")
    php = r"""<?php
namespace App;
class C {
    public function __construct(private Plain $p) {}
    public function run(): void { $this->p->typoed(); }
}
"""
    write_file(tmp_path, "Subject.php", php)
    site = CallSite("nid:1", "", "typoed", "Subject.php", f"L{line_with(php, '->typoed()')}")

    without = SOURCE.resolve(str(tmp_path), [site])
    assert [t.class_fqn for t in without.targets] == ["App\\Plain"], "unread class is emitted"

    with_corpus = SOURCE.resolve(str(tmp_path), [site], corpus=["Plain.php"])
    assert with_corpus.targets == []
    assert with_corpus.unresolved == {Reason.METHOD_NOT_DECLARED: 1}


def test_corpus_never_makes_it_refuse_a_class_it_did_not_read(tmp_path):
    """The disproof rule is unchanged: a corpus that omits the owner still emits."""
    write_file(tmp_path, "Plain.php", "<?php\nnamespace App;\nclass Plain { public function actual(): void {} }\n")
    php = r"""<?php
namespace App;
use Elsewhere\Thing;
class C {
    public function __construct(private Thing $t) {}
    public function run(): void { $this->t->whatever(); }
}
"""
    write_file(tmp_path, "Subject.php", php)
    site = CallSite("nid:1", "", "whatever", "Subject.php", f"L{line_with(php, '->whatever()')}")
    result = SOURCE.resolve(str(tmp_path), [site], corpus=["Plain.php"])
    assert result.unresolved == {}
    assert result.targets[0].class_fqn == "Elsewhere\\Thing"


def test_a_file_in_both_the_corpus_and_the_sites_is_parsed_once(tmp_path, monkeypatch):
    import graphify_php.sources.tree_sitter_source as module

    parsed: list[str] = []
    original = module.scan_source

    def counting(path, source, *args, **kwargs):
        parsed.append(path)
        return original(path, source, *args, **kwargs)

    monkeypatch.setattr(module, "scan_source", counting)

    site = intersection_site(tmp_path)
    module.TreeSitterSource().resolve(
        str(tmp_path), [site], corpus=["Emails.php", "Loggable.php", "Caller.php"]
    )
    assert sorted(parsed) == ["Caller.php", "Emails.php", "Loggable.php"]


def test_a_corpus_entry_that_cannot_be_read_is_skipped_not_fatal(tmp_path):
    site = intersection_site(tmp_path)
    result = SOURCE.resolve(str(tmp_path), [site], corpus=["Emails.php", "Gone.php", "Loggable.php"])
    assert result.targets[0].class_fqn == "App\\Service\\Emails"
    # The missing file is not a site, so it is not counted as an unreadable one either.
    assert result.unresolved == {}


def test_an_empty_corpus_behaves_like_no_corpus(tmp_path):
    site = intersection_site(tmp_path)
    assert SOURCE.resolve(str(tmp_path), [site], corpus=[]).unresolved == {Reason.UNKNOWN_RECEIVER_TYPE: 1}
