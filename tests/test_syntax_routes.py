"""Route declarations read from PHP attributes.

The composition rules under test are Symfony's own, read from
`vendor/symfony/routing/Loader/AttributeClassLoader.php` rather than assumed — in particular
`methods` is a UNION of the class-level and method-level lists, not an override, which is the
rule most likely to be guessed wrong.

These tests are not run under the receiver-supplied / receiver-empty fixture used by the
call-target tests. That parametrisation varies `CallSite.receiver`; a route declaration has no
call site and no receiver, so running each case twice would duplicate work without covering
anything. The forms that do vary here — positional against named `path`, `methods` as a string
against a list against absent, class-level prefix against none — are parametrised instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.sources.route_source import (              # noqa: E402
    RouteDeclarationSource,
    TreeSitterRouteSource,
)
from graphify_php.syntax import routes as routes_module      # noqa: E402
from graphify_php.syntax.file_scan import scan_source        # noqa: E402

SOURCE = TreeSitterRouteSource()


def scan(php: str, path: str = "Controller.php"):
    return scan_source(path, php.encode("utf-8")).routes


def one(php: str):
    found = scan(php)
    assert len(found) == 1, f"expected one route, got {[(r.path, r.controller_method) for r in found]}"
    return found[0]


# --- the forms that actually occur ---------------------------------------------------------


def test_positional_path():
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route('/api/v1/storage/upload/small/{id}', name: 'api-storage-upload-small', methods: ['POST'])]
    public function upload() {}
}
""")
    assert route.path == "/api/v1/storage/upload/small/{id}"
    assert route.name == "api-storage-upload-small"
    assert route.methods == ("POST",)
    assert route.controller_class == "App\\Controller\\C"
    assert route.controller_method == "upload"
    assert route.resolved is True


def test_named_path_argument():
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route(path: '/health', name: 'health')]
    public function health() {}
}
""")
    assert route.path == "/health"
    assert route.name == "health"


@pytest.mark.parametrize(
    "written, expected",
    [
        ("methods: ['GET', 'POST']", ("GET", "POST")),
        ("methods: 'GET'", ("GET",)),        # a bare string, as written in this codebase
        ('methods: ["PATCH"]', ("PATCH",)),
        ("", ()),                             # absent: every method, and nothing to record
    ],
)
def test_methods_forms(written, expected):
    extra = ", " + written if written else ""
    route = one("""<?php
namespace App\\Controller;
class C {
    #[Route('/x', name: 'x'%s)]
    public function x() {}
}
""" % extra)
    assert route.methods == expected


def test_double_quoted_strings_read_the_same():
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route("/x/{id}", name: "x", methods: ["GET"])]
    public function x() {}
}
""")
    assert (route.path, route.name, route.methods) == ("/x/{id}", "x", ("GET",))


def test_route_attribute_is_found_among_other_attributes():
    """Real controllers carry OpenAPI and security attributes around the route."""
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[OA\Get(path: '/decoy', summary: 'not a route')]
    #[IsGranted('ROLE_USER')]
    #[Route('/real', name: 'real', methods: ['GET'])]
    #[OA\Response(response: 200, description: 'ok')]
    public function real() {}
}
""")
    assert route.path == "/real"


def test_fully_qualified_attribute_name_is_matched():
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[\Symfony\Component\Routing\Attribute\Route('/q', name: 'q')]
    public function q() {}
}
""")
    assert route.path == "/q"


# --- composition ---------------------------------------------------------------------------


def test_class_prefix_composes_with_the_method_path():
    route = one(r"""<?php
namespace App\Controller;
#[Route('/api/v1/storage', name: 'api-storage-')]
class C {
    #[Route('/upload/{id}', name: 'upload', methods: ['POST'])]
    public function upload() {}
}
""")
    assert route.path == "/api/v1/storage/upload/{id}"
    assert route.name == "api-storage-upload"
    assert route.class_path == "/api/v1/storage"
    assert route.method_path == "/upload/{id}"


def test_methods_are_a_union_of_class_and_method_levels():
    """Symfony does `array_unique(array_merge(...))`; the method level does not override."""
    route = one(r"""<?php
namespace App\Controller;
#[Route('/api', methods: ['GET'])]
class C {
    #[Route('/x', name: 'x', methods: ['POST'])]
    public function x() {}
}
""")
    assert route.methods == ("GET", "POST")


def test_class_level_methods_apply_when_the_method_declares_none():
    route = one(r"""<?php
namespace App\Controller;
#[Route('/api', methods: ['GET'])]
class C {
    #[Route('/x', name: 'x')]
    public function x() {}
}
""")
    assert route.methods == ("GET",)


def test_invokable_class_takes_its_route_from_the_class_attribute():
    """`resetGlobals()` in the loader: the class attribute is the route, not a prefix."""
    route = one(r"""<?php
namespace App\Controller;
#[Route('/webhook/stripe', name: 'webhook-stripe', methods: ['POST'])]
class StripeWebhook {
    public function __invoke() {}
}
""")
    assert route.path == "/webhook/stripe"
    assert route.name == "webhook-stripe"
    assert route.methods == ("POST",)
    assert route.controller_method == "__invoke"


def test_invokable_class_with_a_method_route_does_not_double_count():
    """The class attribute is a prefix once any method declares a route."""
    found = scan(r"""<?php
namespace App\Controller;
#[Route('/hooks', name: 'hooks-')]
class H {
    #[Route('/stripe', name: 'stripe')]
    public function __invoke() {}
}
""")
    assert [(r.path, r.name) for r in found] == [("/hooks/stripe", "hooks-stripe")]


def test_abstract_class_declares_nothing():
    """Symfony's loader refuses to read attributes from an abstract class."""
    assert scan(r"""<?php
namespace App\Controller;
#[Route('/base', name: 'base-')]
abstract class BaseController {
    #[Route('/x', name: 'x')]
    public function x() {}
}
""") == []


# --- identity ------------------------------------------------------------------------------


def test_two_classes_declaring_the_same_path_are_two_declarations():
    """A route is owned by where it is declared, so `/health` twice is two nodes."""
    found = scan(r"""<?php
namespace App\Controller;
class Alpha {
    #[Route('/health', name: 'alpha-health')]
    public function health() {}
}
class Beta {
    #[Route('/health', name: 'beta-health')]
    public function health() {}
}
""")
    assert len(found) == 2
    assert len({r.node_key for r in found}) == 2
    assert {r.controller_class for r in found} == {"App\\Controller\\Alpha", "App\\Controller\\Beta"}


def test_node_key_separates_two_paths_on_one_controller():
    found = scan(r"""<?php
namespace App\Controller;
class C {
    #[Route('/a', name: 'a')]
    public function a() {}
    #[Route('/b', name: 'b')]
    public function b() {}
}
""")
    assert len({r.node_key for r in found}) == 2


# --- the negative: an attribute that cannot be evaluated ------------------------------------


def test_non_literal_path_emits_the_parts_and_refuses_the_effective_route():
    """A concatenation is not evaluated. Reading it as the literal half invents a path."""
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route(self::PREFIX . '/x', name: 'x')]
    public function x() {}
}
""")
    assert route.path is None
    assert route.resolved is False
    assert route.reason == routes_module.PATH_NOT_LITERAL
    assert route.name == "x"          # the parts that were literal survive
    assert route.controller_method == "x"


def test_non_literal_class_prefix_refuses_the_effective_route_but_keeps_the_method_path():
    route = one(r"""<?php
namespace App\Controller;
#[Route(self::BASE)]
class C {
    #[Route('/x', name: 'x')]
    public function x() {}
}
""")
    assert route.path is None
    assert route.reason == routes_module.PREFIX_NOT_LITERAL
    assert route.method_path == "/x"


def test_a_route_with_no_path_argument_is_refused():
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route(name: 'named-only')]
    public function x() {}
}
""")
    assert route.path is None
    assert route.reason == routes_module.NO_PATH
    assert route.name == "named-only"


def test_a_class_without_route_attributes_declares_nothing():
    assert scan(r"""<?php
namespace App\Controller;
class C {
    public function notARoute() {}
}
""") == []


# --- the source ----------------------------------------------------------------------------


def test_source_satisfies_its_own_port():
    assert isinstance(SOURCE, RouteDeclarationSource)
    assert SOURCE.name == "tree_sitter_routes"


def test_available_needs_nothing(tmp_path):
    assert SOURCE.available(str(tmp_path)) == (True, "")


def test_source_collects_across_files_and_counts_refusals(tmp_path):
    (tmp_path / "A.php").write_text("""<?php
namespace App;
class A {
    #[Route('/a', name: 'a', methods: ['GET'])]
    public function a() {}
}
""", encoding="utf-8")
    (tmp_path / "B.php").write_text("""<?php
namespace App;
class B {
    #[Route(self::P . '/b', name: 'b')]
    public function b() {}
}
""", encoding="utf-8")
    result = SOURCE.declarations(str(tmp_path), ["A.php", "B.php"])
    assert result.files_seen == 2
    assert len(result.declarations) == 2
    assert [d.path for d in result.resolved] == ["/a"]
    assert result.unresolved == {routes_module.PATH_NOT_LITERAL: 1}


def test_source_counts_a_file_it_cannot_read(tmp_path):
    result = SOURCE.declarations(str(tmp_path), ["Gone.php"])
    assert result.declarations == []
    assert result.unresolved == {"unreadable-file": 1}
    assert result.files_seen == 0


def test_source_reads_each_file_once(tmp_path):
    (tmp_path / "A.php").write_text("""<?php
namespace App;
class A {
    #[Route('/a', name: 'a')]
    public function a() {}
}
""", encoding="utf-8")
    result = SOURCE.declarations(str(tmp_path), ["A.php", "A.php", str(tmp_path / "A.php")])
    assert len(result.declarations) == 1
    assert result.files_seen == 1


def test_only_the_named_fields_are_stored(tmp_path):
    """Nothing from the attribute beyond path, name and methods reaches the declaration."""
    route = one(r"""<?php
namespace App\Controller;
class C {
    #[Route('/x', name: 'x', methods: ['GET'], requirements: ['id' => 'SECRETPATTERN'],
            defaults: ['token' => 'SECRETDEFAULT'], condition: "request.headers.get('X') == 'SECRETCOND'")]
    public function x() {}
}
""")
    blob = repr(route)
    for leaked in ("SECRETPATTERN", "SECRETDEFAULT", "SECRETCOND", "requirements", "condition"):
        assert leaked not in blob, f"{leaked} reached the declaration"


def test_route_output_leaks_no_planted_secret(tmp_path):
    """The canary check, kept as a test so it is enforced rather than performed once.

    `eval/canaries.py` plants fake credentials in the fixtures and asserts they reach no
    artifact. A route declaration stores only the path template, the name and the methods, so
    a secret in a `defaults:` or `condition:` argument has nowhere to land — this pins that.
    """
    import json

    from eval import canaries

    php = """<?php
namespace App\\Controller;
class C {
    #[Route('/x', name: 'x', defaults: ['token' => '%s'], condition: "%s")]
    public function x() {}
}
""" % (canaries.canary_values()[0], canaries.canary_values()[0])
    (tmp_path / "C.php").write_text(php, encoding="utf-8")
    result = SOURCE.declarations(str(tmp_path), ["C.php"])

    artifact = json.dumps([
        {"key": d.node_key, "path": d.path, "name": d.name, "methods": list(d.methods),
         "class": d.controller_class, "method": d.controller_method,
         "file": d.source_file, "line": d.line, "reason": d.reason}
        for d in result.declarations
    ]) + json.dumps(result.unresolved)

    assert result.declarations, "fixture must produce a declaration, or this proves nothing"
    canaries.assert_no_leaks({"routes": artifact})
