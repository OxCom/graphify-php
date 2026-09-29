"""The scanner: what one file proves about itself, and what it refuses to."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.ports import Confidence                      # noqa: E402
from graphify_php.syntax import nodes                          # noqa: E402
from graphify_php.syntax.collectors import (                   # noqa: E402
    COLLECTORS,
    ConstructorAssignmentCollector,
    LocalConstructionCollector,
    PromotedParameterCollector,
    TypedPropertyCollector,
    by_node_kind,
)
from graphify_php.syntax.file_scan import scan_source          # noqa: E402
from graphify_php.syntax.names import NameResolver             # noqa: E402


def scan(php: str):
    return scan_source("fake.php", php.encode("utf-8"))


def only_class(php: str):
    facts = scan(php)
    assert len(facts.classes) == 1
    return facts.classes[0]


# --- the four type sources -------------------------------------------------------------


def test_promoted_constructor_parameter_is_extracted():
    scope = only_class(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function __construct(private readonly Emails $emails) {}
}
""")
    fact = scope.properties["emails"]
    assert fact.fqn == "App\\Service\\Messages\\Emails"
    assert fact.confidence is Confidence.EXTRACTED
    assert fact.via == "promoted-parameter"


def test_typed_property_declaration_is_extracted():
    scope = only_class(r"""<?php
namespace App;
use App\Service\Legacy;
class C {
    private readonly Legacy $legacy;
    private ?Legacy $maybe;
}
""")
    assert scope.properties["legacy"].fqn == "App\\Service\\Legacy"
    assert scope.properties["legacy"].confidence is Confidence.EXTRACTED
    # `?Legacy` still reaches Legacy::method whenever the call happens at all.
    assert scope.properties["maybe"].fqn == "App\\Service\\Legacy"


def test_multi_element_property_declaration_covers_every_name():
    scope = only_class(r"""<?php
namespace App;
class C { private Legacy $a, $b; }
""")
    assert scope.properties["a"].fqn == "App\\Legacy"
    assert scope.properties["b"].fqn == "App\\Legacy"


def test_constructor_body_assignment_is_inferred():
    scope = only_class(r"""<?php
namespace App;
use App\Service\Legacy;
class C {
    private $legacy;
    public function __construct(Legacy $x) { $this->legacy = $x; }
}
""")
    fact = scope.properties["legacy"]
    assert fact.fqn == "App\\Service\\Legacy"
    assert fact.confidence is Confidence.INFERRED
    assert fact.via == "constructor-assignment"


def test_new_binding_for_a_local_is_inferred():
    scope = only_class(r"""<?php
namespace App;
use App\Service\Messages\Emails;
class C {
    public function run(): void { $mailer = new Emails(); $mailer->send(); }
}
""")
    fact = scope.methods[0].locals["mailer"]
    assert fact.fqn == "App\\Service\\Messages\\Emails"
    assert fact.confidence is Confidence.INFERRED
    assert fact.via == "new-binding"


def test_promoted_parameter_outranks_a_constructor_assignment():
    """Both sources describe `$a`; the declared one wins and stays EXTRACTED."""
    scope = only_class(r"""<?php
namespace App;
class C {
    public function __construct(private Real $a, Other $b) { $this->a = $b; }
}
""")
    assert scope.properties["a"].fqn == "App\\Real"
    assert scope.properties["a"].confidence is Confidence.EXTRACTED


# --- name resolution -------------------------------------------------------------------


def test_leading_backslash_is_already_absolute():
    scope = only_class(r"""<?php
namespace App\Deep;
class C { private \Other\Emails $e; }
""")
    assert scope.properties["e"].fqn == "Other\\Emails"


def test_use_alias_wins_over_the_files_own_namespace():
    r"""The shadowing case: `App\Cron\Emails` exists in name only, the import decides."""
    scope = only_class(r"""<?php
namespace App\Cron;
use App\Service\Messages\Emails;
class C { private Emails $e; }
""")
    assert scope.properties["e"].fqn == "App\\Service\\Messages\\Emails"


def test_renaming_alias_binds_the_new_name_not_the_old_one():
    facts = scan(r"""<?php
namespace App\Cron;
use App\Service\Messages\Emails as Mailer;
class C { private Mailer $m; private Emails $e; }
""")
    scope = facts.classes[0]
    assert scope.properties["m"].fqn == "App\\Service\\Messages\\Emails"
    # `Emails` was never imported under that name, so it is relative to this namespace.
    assert scope.properties["e"].fqn == "App\\Cron\\Emails"


def test_unimported_name_stays_in_the_files_namespace():
    scope = only_class(r"""<?php
namespace App\Cron;
class C { private Emails $e; }
""")
    assert scope.properties["e"].fqn == "App\\Cron\\Emails"


def test_alias_prefix_qualifies_a_sub_path():
    resolver = NameResolver(namespace="App\\Cron", imports={"Messages": "App\\Service\\Messages"})
    assert resolver.resolve("Messages\\Emails") == "App\\Service\\Messages\\Emails"


def test_no_namespace_leaves_the_name_bare():
    assert NameResolver().resolve("Emails") == "Emails"


def test_base_clause_is_resolved_through_the_imports():
    scope = only_class(r"""<?php
namespace App\Cron\Jobs;
use App\Cron\AbstractReminderCronJob;
class SubmitWeek extends AbstractReminderCronJob {}
""")
    assert scope.parent == "App\\Cron\\AbstractReminderCronJob"


# --- scoping ---------------------------------------------------------------------------


def test_two_classes_in_one_file_keep_separate_properties():
    facts = scan(r"""<?php
namespace App;
class A { private Alpha $repo; }
class B { private Beta $repo; }
""")
    assert facts.class_named("App\\A").properties["repo"].fqn == "App\\Alpha"
    assert facts.class_named("App\\B").properties["repo"].fqn == "App\\Beta"


def test_two_methods_in_one_class_keep_separate_locals():
    scope = only_class(r"""<?php
namespace App;
class C {
    public function one(): void { $service = new Alpha(); }
    public function two(): void { $service = new Beta(); }
}
""")
    by_name = {m.name: m for m in scope.methods}
    assert by_name["one"].locals["service"].fqn == "App\\Alpha"
    assert by_name["two"].locals["service"].fqn == "App\\Beta"


def test_class_at_line_finds_the_enclosing_declaration():
    php = r"""<?php
namespace App;
class A { private Alpha $repo; }
class B { private Beta $repo; }
"""
    facts = scan(php)
    assert facts.class_at(3).fqn == "App\\A"
    assert facts.class_at(4).fqn == "App\\B"
    assert facts.class_at(2) is None


def test_anonymous_class_members_do_not_leak_into_the_enclosing_class():
    facts = scan(r"""<?php
namespace App;
class C {
    public function make() { return new class { private Sneaky $x; }; }
}
""")
    assert facts.classes[0].properties == {}


# --- refusals recorded while scanning ---------------------------------------------------


def test_union_typed_property_is_refused_not_halved():
    scope = only_class(r"""<?php
namespace App;
class C { private Foo|Bar $u; }
""")
    assert "u" not in scope.properties
    assert scope.refusals["u"] == "union-type"


def test_union_typed_constructor_parameter_is_refused():
    scope = only_class(r"""<?php
namespace App;
class C {
    private $u;
    public function __construct(Foo|Bar $x) { $this->u = $x; }
}
""")
    assert "u" not in scope.properties
    assert scope.refusals["u"] == "union-type"


def test_reassigned_local_loses_its_binding():
    scope = only_class(r"""<?php
namespace App;
class C {
    public function run(): void {
        $m = new Emails();
        $m = $this->somethingElse();
        $m->send();
    }
}
""")
    method = scope.methods[0]
    assert "m" not in method.locals
    assert method.refusals["m"] == "reassigned-local"


def test_binding_after_an_earlier_write_is_also_refused():
    scope = only_class(r"""<?php
namespace App;
class C {
    public function run(): void {
        $m = $this->pick();
        $m = new Emails();
    }
}
""")
    method = scope.methods[0]
    assert "m" not in method.locals
    assert method.refusals["m"] == "reassigned-local"


def test_dynamic_property_assignment_records_nothing():
    scope = only_class(r"""<?php
namespace App;
class C {
    public function __construct(Emails $x) { $this->$name = $x; }
}
""")
    assert scope.properties == {}


def test_new_with_a_dynamic_class_records_nothing():
    scope = only_class(r"""<?php
namespace App;
class C { public function run(): void { $m = new $cls(); } }
""")
    assert scope.methods[0].locals == {}


def test_primitive_and_relative_types_are_not_call_targets():
    scope = only_class(r"""<?php
namespace App;
class C {
    private int $n;
    private string $s;
    private array $a;
    private self $me;
    private static $legacy;
}
""")
    assert scope.properties == {}


def test_property_aliased_into_a_local_is_not_tracked():
    """`$e = $this->emails;` needs flow analysis this version does not do."""
    scope = only_class(r"""<?php
namespace App;
class C {
    private Emails $emails;
    public function run(): void { $e = $this->emails; $e->send(); }
}
""")
    assert scope.methods[0].locals == {}


# --- the registry ----------------------------------------------------------------------


def test_registry_groups_collectors_by_node_kind():
    registry = by_node_kind()
    assert registry["property_promotion_parameter"] == [COLLECTORS[0]]
    assert registry["property_declaration"] == [COLLECTORS[1]]
    assert registry["method_declaration"] == [COLLECTORS[2]]
    assert registry["assignment_expression"] == [COLLECTORS[3]]


def test_precedence_is_strictly_ordered_across_the_four_sources():
    order = [
        PromotedParameterCollector.precedence,
        TypedPropertyCollector.precedence,
        ConstructorAssignmentCollector.precedence,
        LocalConstructionCollector.precedence,
    ]
    assert order == sorted(order) and len(set(order)) == 4


def test_a_new_type_source_plugs_in_without_touching_the_walker():
    """The SOLID claim, asserted rather than described."""

    class ConstantCollector:
        node_kind = "class_declaration"
        via = "test-source"
        confidence = Confidence.INFERRED
        precedence = 5

        def collect(self, node, ctx):
            from graphify_php.syntax.facts import TypeFact

            ctx.klass.record_property(
                "injected",
                TypeFact("App\\Injected", self.confidence, self.via, self.precedence),
            )

    facts = scan_source("fake.php", b"<?php namespace App; class C {}", [ConstantCollector()])
    assert facts.classes[0].properties["injected"].via == "test-source"


# --- primitives ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration, expected",
    [
        ("private Foo $x;", "Foo"),
        (r"private \A\Foo $x;", r"\A\Foo"),
        ("private ?Foo $x;", "Foo"),
        ("private Foo|Bar $x;", None),
        ("private int $x;", None),
        ("private self $x;", None),
    ],
)
def test_type_name_reads_one_class_or_nothing(declaration, expected):
    tree = nodes.parse(("<?php class C { %s }" % declaration).encode())
    prop = _find(tree.root_node, "property_declaration")
    assert nodes.type_name(nodes.type_node(prop)) == expected


def test_is_union_separates_two_types_from_no_type():
    tree = nodes.parse(b"<?php class C { private Foo|Bar $u; private int $n; }")
    union, primitive = _find_all(tree.root_node, "property_declaration")
    assert nodes.is_union(nodes.type_node(union)) is True
    assert nodes.is_union(nodes.type_node(primitive)) is False


def _find(node, kind):
    return _find_all(node, kind)[0]


def _find_all(node, kind):
    found = [node] if node.type == kind else []
    for child in node.children:
        found.extend(_find_all(child, kind))
    return found


# --- the call-site index ----------------------------------------------------------------


def test_call_index_reads_receivers_graphify_does_not_send():
    facts = scan(r"""<?php
namespace App;
class C {
    public function run(): void {
        $this->emails->send();
        $local->go();
        $a?->nullsafe();
    }
}
""")
    assert facts.calls.receiver_for(5, "send") == ("$this->emails", 1)
    assert facts.calls.receiver_for(6, "go") == ("$local", 1)
    assert facts.calls.receiver_for(7, "nullsafe") == ("$a", 1)


def test_call_index_keeps_a_chain_apart_from_its_own_receiver():
    facts = scan(r"""<?php
namespace App;
class C { public function run(): void { $this->a->b()->c(); } }
""")
    assert facts.calls.receiver_for(3, "b") == ("$this->a", 1)
    assert facts.calls.receiver_for(3, "c") == ("$this->a->b()", 1)


def test_call_index_counts_two_calls_to_one_method_on_a_line():
    facts = scan(r"""<?php
namespace App;
class C { public function run(): void { $a->go(); $b->go(); } }
""")
    assert facts.calls.receiver_for(3, "go") == (None, 2)


def test_call_index_reports_a_line_with_no_such_call():
    facts = scan(r"""<?php
namespace App;
class C { public function run(): void { Helper::format(); } }
""")
    assert facts.calls.receiver_for(3, "format") == (None, 0)


# --- the class index --------------------------------------------------------------------


def test_class_index_separates_declared_absent_and_unknown():
    from graphify_php.syntax.class_index import (
        DECLARED, MAGIC, NOT_DECLARED, UNKNOWN, ClassIndex, index_from_facts,
    )

    index = ClassIndex()
    index_from_facts(index, scan(r"""<?php
namespace App;
class Plain { public function actual(): void {} }
class Magic { public function __call($n, $a) {} }
class Child extends Unread { public function own(): void {} }
"""))
    assert index.declares("App\\Plain", "actual") == DECLARED
    assert index.declares("App\\Plain", "missing") == NOT_DECLARED
    assert index.declares("App\\Magic", "anything") == MAGIC
    assert index.declares("App\\Child", "own") == DECLARED
    assert index.declares("App\\Child", "fromParent") == UNKNOWN
    assert index.declares("App\\NeverSeen", "x") == UNKNOWN


def test_class_index_walks_traits_and_interfaces():
    from graphify_php.syntax.class_index import DECLARED, ClassIndex, index_from_facts

    index = ClassIndex()
    index_from_facts(index, scan(r"""<?php
namespace App;
trait Sends { public function dispatch(): void {} }
interface Mailer { public function deliver(): void; }
class Campaign implements Mailer { use Sends; public function deliver(): void {} }
"""))
    assert index.declares("App\\Campaign", "dispatch") == DECLARED
    assert index.declares("App\\Campaign", "deliver") == DECLARED
    assert index.declares("App\\Mailer", "deliver") == DECLARED


def test_class_index_survives_a_cycle_in_the_ancestry():
    from graphify_php.syntax.class_index import NOT_DECLARED, ClassIndex, ClassRecord

    index = ClassIndex()
    index.add(ClassRecord("A", frozenset(), ("B",)))
    index.add(ClassRecord("B", frozenset(), ("A",)))
    assert index.declares("A", "anything") == NOT_DECLARED
