"""A real graphify build over a generated PHP fixture, from registration to edges on disk.

Every other test in this package checks one layer against its own assumptions. This one runs
graphify itself, because the integration bugs found so far were all invisible to layer tests:
graphify re-execs the process for `update`, it overwrites `_origin` on every edge after the
resolvers run, and it records `self::` and `Class::` calls in a shape the member-call filter
never saw.

Skipped when graphify or tree-sitter is missing, so the suite still runs on a bare checkout.
Everything it writes goes to pytest's `tmp_path`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("graphify") is None,
    reason="graphify is not installed in this interpreter",
)

FIXTURE = {
    "src/Emails.php": """
        <?php
        namespace App\\Service;

        class Emails
        {
            public function reminderSubmitWeek(string $user): void { echo $user; }
        }
    """,
    "src/Helper.php": """
        <?php
        namespace App\\Service;

        class Helper
        {
            public static function format(string $raw): string { return trim($raw); }
        }
    """,
    "src/AbstractController.php": """
        <?php
        namespace App\\Controller;

        abstract class AbstractController
        {
            protected function boot(): void { error_log('boot'); }
        }
    """,
    "src/ReminderController.php": """
        <?php
        namespace App\\Controller;

        use App\\Service\\Emails;
        use App\\Service\\Helper;
        use Symfony\\Component\\Routing\\Attribute\\Route;

        #[Route('/reminders')]
        class ReminderController extends AbstractController
        {
            public function __construct(private readonly Emails $emails) {}

            #[Route('/run/{user}', name: 'reminder_run', methods: ['GET', 'POST'])]
            public function run(string $user, object $registry, string $method): void
            {
                $this->emails->reminderSubmitWeek($user);
                self::tidy($user);
                parent::boot();
                Helper::format($user);
                $registry->get('mailer')->flush();
                $registry->$method($user);
            }

            private static function tidy(string $raw): string { return $raw; }
        }
    """,
}

# The build runs in a subprocess: graphify's `update` is a whole-program command that changes
# global state (and would re-exec were PYTHONHASHSEED unset), so running it inside the test
# process would leak into every test after it.
RUNNER = """
import json, sys
sys.path.insert(0, %(src)r)
import graphify_php
graphify_php.register(repo_root=%(repo)r)
sys.argv = ["graphify", "update", %(repo)r, "--no-cluster"]
from graphify.__main__ import main
try:
    main()
except SystemExit as exc:
    if exc.code:
        raise
"""


def _build(tmp_path: Path, register: bool) -> tuple[dict, dict]:
    repo = tmp_path / ("with" if register else "without")
    for name, body in FIXTURE.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())

    out = tmp_path / ("out-with" if register else "out-without")
    status_path = out / ".graphify-php-status.json"
    env = {
        **os.environ,
        "GRAPHIFY_OUT": str(out),
        "GRAPHIFY_PHP_STATUS": str(status_path),
        # Skips graphify's own re-exec (__main__.py:486-525), which would replace this
        # subprocess and lose the registration made a line earlier.
        "PYTHONHASHSEED": "0",
    }
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = (
        RUNNER % {"src": src, "repo": str(repo)}
        if register
        else (
            f"import sys\nsys.argv = ['graphify', 'update', {str(repo)!r}, '--no-cluster']\n"
            "from graphify.__main__ import main\n"
            "try:\n    main()\nexcept SystemExit as exc:\n    pass\n"
        )
    )
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr

    graph = json.loads((out / "graph.json").read_text())
    record = json.loads(status_path.read_text()) if status_path.exists() else {}
    return graph, record


def _calls(graph: dict) -> list[dict]:
    return [e for e in graph["links"] if e["relation"] == "calls"]


def _pairs(edges: list[dict]) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in edges}


# The four calls in the fixture this package can bind, and the type source that proves each.
# Which of them graphify binds itself depends on its version, so the tests below check that
# every one ends up bound exactly once, by either side, rather than who bound it.
FIXTURE_TARGETS = {
    ".reminderSubmitWeek()": "tree_sitter:promoted-parameter",
    ".tidy()": "tree_sitter:self-scope",
    ".boot()": "tree_sitter:parent-scope",
    ".format()": "tree_sitter:class-scope",
}


def _ours(graph: dict) -> list[dict]:
    return [e for e in _calls(graph) if e.get("_via")]


@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    return _build(root, register=False), _build(root, register=True)


def test_registering_adds_call_edges(builds):
    (plain, _), (registered, _) = builds
    ours = _ours(registered)

    # graphify >= 0.9.87 binds the three `::` calls to their methods itself, and 0.9.89 also
    # binds the property-typed call; this package skips a pair graphify already connected.
    assert ours, "registering added no call edge at all"

    pairs = [(e["source"], e["target"]) for e in _calls(registered)]
    assert len(pairs) == len(set(pairs)), "a pair graphify already connected got a second edge"
    assert set(pairs) == _pairs(_calls(plain)) | _pairs(ours)


def test_every_pattern_in_the_fixture_resolves(builds):
    (_, _), (registered, _) = builds
    label = {n["id"]: n["label"] for n in registered["nodes"]}
    ours = {label[e["target"]]: e for e in _ours(registered)}
    bound = [label[e["target"]] for e in _calls(registered)]

    # Every pattern is bound exactly once, whichever side bound it.
    assert all(bound.count(target) == 1 for target in FIXTURE_TARGETS), bound
    assert ours and set(ours) <= set(FIXTURE_TARGETS)
    for target, edge in ours.items():
        assert edge["_via"] == FIXTURE_TARGETS[target]
    assert all(e["confidence"] == "EXTRACTED" for e in ours.values())


def test_our_edges_are_marked_and_graphify_s_are_not(builds):
    from graphify_php.graph_adapter import EDGE_MARKER, EDGE_ORIGIN

    (_, _), (registered, _) = builds
    marked = [e for e in _calls(registered) if e.get(EDGE_MARKER) == EDGE_ORIGIN]

    assert marked and _pairs(marked) == _pairs(_ours(registered))
    # graphify resets `_origin` on every edge after the resolvers run, so the marker must not
    # live there; this is the assertion that catches it if the marker is ever moved back.
    assert all(e["_origin"] == "ast" for e in _calls(registered))


def test_the_method_level_edge_names_the_coarse_edge_it_refines(builds):
    (_, _), (registered, _) = builds
    label = {n["id"]: n["label"] for n in registered["nodes"]}
    fine = next(e for e in _calls(registered) if label[e["target"]] == ".format()")
    coarse = [e for e in _calls(registered) if label[e["target"]] == "Helper"]

    # graphify >= 0.9.87 binds `Helper::format()` to the method and emits no class edge, so
    # there is nothing to refine and the key must stay off.
    if not coarse:
        assert "_refines" not in fine
        return
    # Both edges are published: different endpoints, so neither dedup nor removal applies.
    # `_refines` is what lets a consumer collapse the pair instead of counting the call twice.
    assert fine["_refines"] == coarse[0]["target"]


def test_the_unresolvable_call_stays_unresolved_and_is_counted(builds):
    (_, _), (registered, record) = builds
    label = {n["id"]: n["label"] for n in registered["nodes"]}
    assert ".flush()" not in {label[e["target"]] for e in _calls(registered)}

    entry = record["resolvers"][0]
    assert entry["state"] == "completed"
    assert entry["eligible_sites"] == 7
    assert entry["edges_added"] == len(_ours(registered))
    assert entry["unresolved"]["tree_sitter:chained-call"] == 1
    assert entry["unresolved"]["tree_sitter:dynamic-name"] == 1


def test_the_route_declaration_is_published_as_a_node(builds):
    (plain, _), (registered, _) = builds

    assert not [n for n in plain["nodes"] if n.get("_kind") == "http_route"], (
        "graphify alone knows nothing about routes"
    )

    routes = [n for n in registered["nodes"] if n.get("_kind") == "http_route"]
    assert len(routes) == 1
    route = routes[0]
    assert route["label"] == "/reminders/run/{user}"
    assert route["route_name"] == "reminder_run"
    assert route["http_methods"] == ["GET", "POST"]
    assert route["route_complete"] is True


def test_the_route_node_is_edged_to_the_controller_method(builds):
    from graphify_php.graph_adapter import ROUTE_RELATION

    (_, _), (registered, _) = builds
    label = {n["id"]: n["label"] for n in registered["nodes"]}
    route = next(n for n in registered["nodes"] if n.get("_kind") == "http_route")

    edges = [
        e
        for e in registered["links"]
        if e["relation"] == ROUTE_RELATION and e["source"] == route["id"]
    ]
    assert len(edges) == 1
    # The path is served by the controller method, which is the connection that makes a route
    # answerable by traversal rather than by grep.
    assert label[edges[0]["target"]] == ".run()"


def test_the_route_pass_reports_its_own_status_record(builds):
    (_, _), (_, record) = builds
    routes = next(r for r in record["resolvers"] if r["name"] == "php_routes")

    assert routes["state"] == "completed"
    assert routes["eligible_sites"] == 1
    assert routes["resolved_sites"] == 1
    assert routes["edges_added"] == 1
    assert routes["unresolved"] == {}
