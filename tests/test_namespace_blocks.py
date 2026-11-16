"""`use` imports belong to the namespace block they are written in.

A file with several namespace blocks, braced or not, has one import map per block. An import
written in one block must not resolve a name in another: that is exactly the short-name guess
the scanner refuses everywhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.frameworks import symfony  # noqa: E402
from graphify_php.syntax import nodes  # noqa: E402
from graphify_php.syntax.facts import ClassScope  # noqa: E402
from graphify_php.syntax.file_scan import parse_source, scan_source  # noqa: E402
from graphify_php.syntax.names import NameResolver  # noqa: E402
from graphify_php.syntax.routes import class_constant_name  # noqa: E402


def scan(php: str):
    return scan_source("fake.php", php.encode("utf-8"))


def klass(facts, class_fqn: str) -> ClassScope:
    scope = facts.class_named(class_fqn)
    assert scope is not None, class_fqn
    return scope


def names_of(facts, class_fqn: str) -> NameResolver:
    names = klass(facts, class_fqn).names
    assert names is not None, class_fqn
    return names


def emails_fqn(facts, class_fqn: str) -> str:
    return klass(facts, class_fqn).properties["emails"].fqn


TWO_BRACED = r"""<?php
namespace A {
    use X\Emails;
    class First {
        public function __construct(private Emails $emails) {}
    }
}
namespace B {
    class Second extends Base {
        public function __construct(private Emails $emails) {}
    }
}
"""


def test_braced_import_does_not_reach_the_next_block():
    facts = scan(TWO_BRACED)
    assert emails_fqn(facts, "A\\First") == "X\\Emails"
    assert emails_fqn(facts, "B\\Second") == "B\\Emails"


def test_class_scope_carries_its_own_block():
    facts = scan(TWO_BRACED)
    assert names_of(facts, "A\\First").resolve("Emails") == "X\\Emails"
    assert names_of(facts, "B\\Second").resolve("Emails") == "B\\Emails"
    assert klass(facts, "B\\Second").parent == "B\\Base"


def test_ancestor_import_does_not_reach_the_next_block():
    facts = scan(r"""<?php
namespace A {
    use X\Base;
    class First extends Base {}
}
namespace B {
    class Second extends Base {}
}
""")
    assert klass(facts, "A\\First").parent == "X\\Base"
    assert klass(facts, "B\\Second").parent == "B\\Base"


def test_unbraced_namespace_statements_each_start_empty():
    facts = scan(r"""<?php
namespace A;
use X\Emails;
class First {
    public function __construct(private Emails $emails) {}
}
namespace B;
class Second {
    public function __construct(private Emails $emails) {}
}
""")
    assert emails_fqn(facts, "A\\First") == "X\\Emails"
    assert emails_fqn(facts, "B\\Second") == "B\\Emails"


def test_global_block_has_its_own_imports():
    facts = scan(r"""<?php
namespace A {
    use X\Emails;
    class First {
        public function __construct(private Emails $emails) {}
    }
}
namespace {
    class Global1 {
        public function __construct(private Emails $emails) {}
    }
}
namespace {
    use Y\Emails;
    class Global2 {
        public function __construct(private Emails $emails) {}
    }
}
""")
    assert emails_fqn(facts, "A\\First") == "X\\Emails"
    assert emails_fqn(facts, "Global1") == "Emails"
    assert emails_fqn(facts, "Global2") == "Y\\Emails"


def test_grouped_use_stays_in_its_braced_block():
    facts = scan(r"""<?php
namespace A {
    use X\{Emails, Other as Alias};
    class First {
        public function __construct(private Emails $emails, private Alias $alias) {}
    }
}
namespace B {
    class Second {
        public function __construct(private Emails $emails, private Alias $alias) {}
    }
}
""")
    first = klass(facts, "A\\First")
    second = klass(facts, "B\\Second")
    assert first.properties["emails"].fqn == "X\\Emails"
    assert first.properties["alias"].fqn == "X\\Other"
    assert second.properties["emails"].fqn == "B\\Emails"
    assert second.properties["alias"].fqn == "B\\Alias"


ROUTES = r"""<?php
namespace A {
    use Symfony\Component\Routing\Attribute\Route;
    class First {
        #[Route('/first')]
        public function one() {}
    }
}
namespace B {
    class Second {
        #[Route('/second')]
        public function two() {}
    }
}
"""


def test_attribute_import_does_not_reach_the_next_block():
    facts = scan(ROUTES)
    by_member = {a.member: a.fqcn for a in facts.attributes}
    assert by_member["one"] == "Symfony\\Component\\Routing\\Attribute\\Route"
    assert by_member["two"] == "B\\Route"


def test_route_imported_in_one_block_is_no_route_in_the_next():
    found = symfony.scan("Controller.php", ROUTES.encode("utf-8")).routes
    assert [(r.path, r.controller_class) for r in found] == [("/first", "A\\First")]


def test_class_constant_resolves_in_its_own_block():
    source = r"""<?php
namespace A {
    use X\Handler;
    $a = Handler::class;
}
namespace B {
    $b = Handler::class;
}
"""
    root, names, _ = parse_source("routes.php", source.encode("utf-8"))
    found = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == "class_constant_access_expression":
            found.append((nodes.line_span(current)[0], class_constant_name(current, names)))
        stack.extend(current.children)
    assert sorted(found) == [(4, "X\\Handler"), (7, "B\\Handler")]


def test_single_namespace_file_keeps_its_file_level_names():
    facts = scan(r"""<?php
namespace App;
use X\Emails;
class C {
    public function __construct(private Emails $emails) {}
}
""")
    assert facts.namespace == "App"
    assert facts.imports == {"Emails": "X\\Emails"}
    assert names_of(facts, "App\\C").imports is facts.imports
