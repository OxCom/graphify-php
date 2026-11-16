"""The segment procedure of the `access_control` path matcher.

A route is edged to an `access_control` rule only when the rule provably applies to every URL
of the route, so a wrong MATCH is a false "covered" state. These tests pin the verdict per rule
shape and then check every decided answer against Python's `re.search` over concrete URLs
generated from each template's requirements.
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graphify_php.frameworks import symfony  # noqa: E402
from graphify_php.sources.route_source import TreeSitterRouteSource  # noqa: E402
from graphify_php.syntax import access_control as ac  # noqa: E402

M, D, U = ac.MATCH, ac.DISJOINT, ac.UNDECIDED
DIGITS = r"\d+"

R_USER_TRACKER = r"^/api/v1/users/\d+/time-tracker"
R_REVIEW = r"^/api/v1/teams/\d+/appraisals/\d+/participants/\d+/review(/submit)?$"
R_PEER = r"^/api/v1/appraisals/\d+/users/\d+/peer-feedback"
R_LOGIN = "^/users/login"
R_TEAM_TRACKER = r"^/teams/time-tracker/\d+"
R_TEAM_APPRAISAL = r"^/teams/appraisals/\d+/users/\d+(/review|/feedback)?$"

ORDERED = [
    R_USER_TRACKER,
    R_REVIEW,
    R_PEER,
    R_LOGIN,
    R_TEAM_TRACKER,
    R_TEAM_APPRAISAL,
    "^/api/v1/teams",
    "^/teams",
    "^/api/v1",
    "^/",
]
RULES = [ac.AccessRule.from_config(i, {"path": p, "roles": ["R"]}) for i, p in enumerate(ORDERED)]

PARTICIPANT = "/api/v1/teams/{t}/appraisals/{a}/participants/{p}"
NUMBERED = {"t": DIGITS, "a": DIGITS, "p": DIGITS, "u": DIGITS, "id": DIGITS}


def facts(requirements: dict | None = None, defaults=(), unread=False) -> ac.PlaceholderFacts:
    return ac.PlaceholderFacts(
        requirements=tuple((requirements or {}).items()), defaults=tuple(defaults), unread=unread
    )


def verdict(template: str, regex: str, requirements=None, defaults=()) -> str:
    return ac.path_verdict(template, regex, facts(requirements, defaults))


def test_digit_segments_and_literal_tail_by_prefix():
    tracker = "/api/v1/users/{id}/time-tracker"
    assert verdict(tracker, R_USER_TRACKER, NUMBERED) == M
    assert verdict(tracker + "/{day}", R_USER_TRACKER, NUMBERED) == M
    # No `$`: the last literal matches by prefix, so a longer word is still covered.
    assert verdict(tracker + "s", R_USER_TRACKER, NUMBERED) == M
    # The default requirement `[^/]+` holds digits and non-digits.
    assert verdict(tracker, R_USER_TRACKER) == U
    assert verdict("/api/v1/users/{id}", R_USER_TRACKER, NUMBERED) == D
    assert verdict("/api/v1/users/{id}/time", R_USER_TRACKER, NUMBERED) == D
    assert verdict(tracker, R_USER_TRACKER, {"id": "me|all"}) == D


def test_anchored_rule_with_optional_literal_group():
    assert verdict(PARTICIPANT + "/review", R_REVIEW, NUMBERED) == M
    assert verdict(PARTICIPANT + "/review/submit", R_REVIEW, NUMBERED) == M
    assert verdict(PARTICIPANT + "/review/{x}", R_REVIEW, {**NUMBERED, "x": "submit"}) == M
    assert verdict(PARTICIPANT + "/review/{x}", R_REVIEW, {**NUMBERED, "x": "submit|undo"}) == U
    assert verdict(PARTICIPANT + "/review/{x}", R_REVIEW, {**NUMBERED, "x": "undo|close"}) == D
    # `$`: a URL longer than every branch is never matched.
    assert verdict(PARTICIPANT + "/review/submit/{x}", R_REVIEW, NUMBERED) == D
    assert verdict(PARTICIPANT + "/reviews", R_REVIEW, NUMBERED) == D
    assert verdict(PARTICIPANT + "/{s}", R_REVIEW, {**NUMBERED, "s": "review|summary"}) == U
    assert verdict(PARTICIPANT + "/review", R_REVIEW) == U


def test_peer_feedback_prefix():
    base = "/api/v1/appraisals/{a}/users/{u}"
    assert verdict(base + "/peer-feedback", R_PEER, NUMBERED) == M
    assert verdict(base + "/peer-feedback/{id}", R_PEER, NUMBERED) == M
    kinds = {**NUMBERED, "k": "peer-feedback|peer-feedbacks"}
    assert verdict(base + "/{k}", R_PEER, kinds) == M
    assert verdict(base + "/{k}", R_PEER, {**NUMBERED, "k": "self-review|manager"}) == D
    assert verdict(base + "/{k}", R_PEER, {**NUMBERED, "k": "peer|peer-feedback"}) == U


def test_placeholder_that_could_be_login():
    assert verdict("/users/{action}", R_LOGIN) == U
    assert verdict("/users/{action}", R_LOGIN, {"action": DIGITS}) == D
    assert verdict("/users/{action}", R_LOGIN, {"action": "login|logout"}) == U
    assert verdict("/users/{action}", R_LOGIN, {"action": "login|loginx"}) == M
    assert verdict("/users/{action}", R_LOGIN, {"action": "logout|register"}) == D
    assert verdict("/users/login/{token}", R_LOGIN) == M


def test_team_rules():
    assert verdict("/teams/time-tracker/{id}", R_TEAM_TRACKER, NUMBERED) == M
    assert verdict("/teams/time-tracker/{id}", R_TEAM_TRACKER) == U
    assert verdict("/teams/time-tracker/{d}/{id}", R_TEAM_TRACKER, {"d": DIGITS}) == M
    assert verdict("/teams/time-tracker/{k}", R_TEAM_TRACKER, {"k": "day|week"}) == D
    pair = "/teams/appraisals/{a}/users/{u}"
    assert verdict(pair, R_TEAM_APPRAISAL, NUMBERED) == M
    assert verdict(pair, R_TEAM_APPRAISAL) == U
    assert verdict(pair + "/{x}", R_TEAM_APPRAISAL, {**NUMBERED, "x": "review|feedback"}) == M
    assert verdict(pair + "/{x}", R_TEAM_APPRAISAL, {**NUMBERED, "x": "review|summary"}) == U
    assert verdict(pair + "/review/{x}", R_TEAM_APPRAISAL, NUMBERED) == D
    assert verdict("/teams/{team}", R_TEAM_APPRAISAL) == D


def test_any_segment_pattern_and_end_anchor():
    assert verdict("/files/{id}", r"^/files/[^/]+$") == M
    assert verdict("/files/{id}/raw", r"^/files/[^/]+$") == D
    assert verdict("/files/{id}", r"^/files/\d+$") == U
    assert verdict("/files/{id}", r"^/files/\d+$", {"id": DIGITS}) == M
    # `$` also accepts a final newline, and a `[^/]+` value may be just `\n` (`%0A`).
    assert verdict("/files/{id}", r"^/files/$") == U
    assert re.search(r"^/files/$", "/files/\n")
    assert verdict("/files/{id}", r"^/files/$", {"id": DIGITS}) == D
    assert verdict("/files/{id}/x", r"^/files/$") == D


def test_defaults_and_optional_placeholders_stay_undecided():
    assert verdict("/teams/time-tracker/{id}", R_TEAM_TRACKER, NUMBERED, ("id",)) == U
    assert verdict("/teams/appraisals/{a}/users/{u}", R_TEAM_APPRAISAL, NUMBERED, ("u",)) == U
    assert verdict("/teams/time-tracker/{id<\\d+>?1}", R_TEAM_TRACKER) == U
    # The old head comparison already proves this one: every URL starts with `/blog`.
    assert verdict("/blog/{page}", "^/blog", defaults=("page",)) == M
    assert verdict("/blog/{page}", "^/blog/", defaults=("page",)) == U
    assert verdict("/blog/{page}", "^/blog/$", {"page": DIGITS}, ("page",)) == U


def test_inline_requirements():
    assert verdict("/teams/time-tracker/{id<\\d+>}", R_TEAM_TRACKER) == M
    assert verdict("/teams/time-tracker/{id<\\d+>}", R_TEAM_TRACKER, {"id": DIGITS}) == M
    # Inline and option requirement differ: which one Symfony keeps is not read.
    assert verdict("/teams/time-tracker/{id<\\d+>}", R_TEAM_TRACKER, {"id": "[0-9]+"}) == U


def test_what_the_procedure_refuses():
    tracker = "/api/v1/users/{id}/time-tracker"
    assert verdict(tracker, R_USER_TRACKER, {"id": ".+"}) == U
    assert verdict(tracker, R_USER_TRACKER, {"id": "[0-9]+"}) == U
    assert verdict(tracker, R_USER_TRACKER, {"id": None}) == U
    assert ac.path_verdict(tracker, R_USER_TRACKER, facts(NUMBERED, unread=True)) == U
    assert ac.path_verdict(tracker, R_USER_TRACKER) == U
    assert verdict("/{_locale}/users/login", "^/en/users/login") == U
    assert verdict("/api/{_format}/x", "^/api/json/x") == U
    # Another segment still decides: the second segment is `users`, never `login...`.
    assert verdict("/{_locale}/users/login", R_LOGIN) == D
    assert verdict("/teams/time-tracker/{id}-x", R_TEAM_TRACKER, NUMBERED) == U
    assert verdict("/teams/time-tracker/{id}/{id}", R_TEAM_TRACKER, NUMBERED) == U
    for rule in (
        r"^/teams/time-tracker/\d{2}",
        r"^/teams/time-tracker/\d+x",
        r"^/teams/time-tracker/(\d+|x)",
        "^/teams/time-tracker/(/a)",
        "^/teams/(time-tracker)/x",
        "^/teams/tim.-tracker/x",
        "^/teams/[a-z]+/x",
        "/teams/time-tracker/x",
        "^/teams/time-tracker/%x%",
    ):
        assert verdict("/teams/time-tracker/{id}", rule, NUMBERED) == U, rule


def test_first_rule_walks_the_ordered_list():
    def first(template, requirements=None, defaults=()):
        rule, reason = ac.first_rule(template, RULES, facts(requirements, defaults))
        return (rule.path if rule else None), reason

    assert first("/teams/appraisals/{a}/users/{u}", NUMBERED) == (R_TEAM_APPRAISAL, "")
    assert first("/teams/{team}") == ("^/teams", "")
    assert first("/teams/appraisals/{a}/users/{u}") == (None, ac.REASON_UNDECIDED)
    assert first("/api/v1/teams/{t}", NUMBERED) == ("^/api/v1/teams", "")
    assert first("/users/{id}", NUMBERED) == ("^/", "")
    # Without facts only the literal head is compared.
    assert ac.first_rule("/teams/{team}", RULES) == (None, ac.REASON_UNDECIDED)


# The differential check. Each template carries its requirements and defaults; concrete URLs
# are built from candidate values that satisfy each placeholder's requirement.
WORDS = [
    "login",
    "logout",
    "review",
    "feedback",
    "submit",
    "summary",
    "time-tracker",
    "peer-feedback",
    "users",
    "teams",
    "appraisals",
    "participants",
    "api",
    "v1",
    "self-review",
    "manager",
    "day",
    "week",
    "cancel",
    "register",
]
CANDIDATES = ["1", "42", "007", "abc", "x", "Z", "1x", "x1", "-"] + WORDS + [w + "x" for w in WORDS]

TEMPLATES: list[tuple[str, dict, tuple]] = [
    ("/api/v1/users/{id}/time-tracker", NUMBERED, ()),
    ("/api/v1/users/{id}/time-tracker", {}, ()),
    ("/api/v1/users/{id}/time-tracker/{day}", NUMBERED, ()),
    ("/api/v1/users/{id}/{what}", {"id": DIGITS, "what": "time-tracker|time-trackers"}, ()),
    ("/api/v1/users/{id}", NUMBERED, ()),
    ("/api/v1/users/{id}", {}, ()),
    (PARTICIPANT + "/review", NUMBERED, ()),
    (PARTICIPANT + "/review", {}, ()),
    (PARTICIPANT + "/review/submit", NUMBERED, ()),
    (PARTICIPANT + "/review/{x}", {**NUMBERED, "x": "submit|cancel"}, ()),
    (PARTICIPANT + "/review/{x}", {**NUMBERED, "x": "submit"}, ()),
    (PARTICIPANT + "/{s}", {**NUMBERED, "s": "review|summary"}, ()),
    (PARTICIPANT + "/review/submit/{x}", NUMBERED, ()),
    ("/api/v1/teams/{t}", NUMBERED, ()),
    ("/api/v1/teams/{t}", {}, ()),
    ("/api/v1/appraisals/{a}/users/{u}/peer-feedback", NUMBERED, ()),
    ("/api/v1/appraisals/{a}/users/{u}/peer-feedback/{id}", NUMBERED, ()),
    ("/api/v1/appraisals/{a}/users/{u}/{k}", {**NUMBERED, "k": "peer-feedback|self-review"}, ()),
    ("/api/v1/appraisals/{a}/users/{u}/{k}", {**NUMBERED, "k": "self-review|manager"}, ()),
    ("/api/v1/{page}", {}, ()),
    ("/users/{action}", {}, ()),
    ("/users/{action}", {"action": DIGITS}, ()),
    ("/users/{action}", {"action": "login|logout"}, ()),
    ("/users/{action}", {"action": "login|loginx"}, ()),
    ("/users/{action}", {"action": "logout|register"}, ()),
    ("/users/login/{token}", {}, ()),
    ("/users/{id}", NUMBERED, ()),
    ("/teams/time-tracker/{id}", NUMBERED, ()),
    ("/teams/time-tracker/{id}", {}, ()),
    ("/teams/time-tracker/{id}", NUMBERED, ("id",)),
    ("/teams/time-tracker/{d}/{id}", {"d": DIGITS}, ()),
    ("/teams/time-tracker/{k}", {"k": "day|week"}, ()),
    ("/teams/time-tracker/{id<\\d+>}", {}, ()),
    ("/teams/appraisals/{a}/users/{u}", NUMBERED, ()),
    ("/teams/appraisals/{a}/users/{u}", {}, ()),
    ("/teams/appraisals/{a}/users/{u}", NUMBERED, ("u",)),
    ("/teams/appraisals/{a}/users/{u}/{x}", {**NUMBERED, "x": "review|feedback"}, ()),
    ("/teams/appraisals/{a}/users/{u}/{x}", {**NUMBERED, "x": "review|summary"}, ()),
    ("/teams/appraisals/{a}/users/{u}/review/{x}", NUMBERED, ()),
    ("/teams/{team}", {}, ()),
    ("/teams/{team}/time-tracker", {}, ()),
    ("/teams/{team}", NUMBERED, ("team",)),
    ("/blog/{page}", {"page": DIGITS}, ("page",)),
    ("/{x}/users/login", {"x": "api|teams"}, ()),
    ("/{x}", {"x": "users|teams"}, ()),
]

_SLOT = re.compile(r"\{(\w+)(<.*?>)?(\?[^}]*)?\}")


def _urls(template: str, requirements: dict, defaults: tuple) -> list[str]:
    """Every URL of the template from the candidate values; a trailing default may be absent."""
    slots = list(_SLOT.finditer(template))
    choices = []
    for k, slot in enumerate(slots):
        name = slot.group(1)
        requirement = slot.group(2)[1:-1] if slot.group(2) else requirements.get(name, "[^/]+")
        values: list[str | None] = [v for v in CANDIDATES if re.fullmatch(requirement, v)]
        last = k == len(slots) - 1 and slot.end() == len(template)
        if last and (name in defaults or slot.group(3)):
            values.append(None)
        choices.append(values)
    urls = []
    for combo in itertools.product(*choices):
        url, end = "", 0
        for slot, value in zip(slots, combo, strict=True):
            head = template[end : slot.start()]
            url += head[:-1] if value is None else head + value
            end = slot.end()
        urls.append(url + template[end:])
    return urls


def _first_match(url: str) -> int | None:
    return next((i for i, p in enumerate(ORDERED) if re.search(p, url, re.ASCII)), None)


def _differential(template: str, urls: list[str], known: ac.PlaceholderFacts) -> tuple:
    """`(disagreements, decided_by_segments, decided_route)` for one template's facts."""
    disagreements: list[tuple] = []
    decided_by_segments = 0
    for regex in ORDERED:
        got = ac.path_verdict(template, regex, known)
        hits = {bool(re.search(regex, url, re.ASCII)) for url in urls}
        if (got == M and hits != {True}) or (got == D and hits != {False}):
            disagreements.append((template, regex, got, hits))
        old = ac.path_verdict(template, regex)
        new = ac.segment_verdict(template, regex, known)
        if U not in (old, new) and old != new:
            disagreements.append((template, regex, "old", old, "new", new))
        if old == U and got != U:
            decided_by_segments += 1
    rule, reason = ac.first_rule(template, RULES, known)
    if rule is not None:
        wrong = [u for u in urls if _first_match(u) != rule.index]
        if wrong:
            disagreements.append((template, rule.path, wrong[:3]))
    elif reason == ac.REASON_NO_RULE:
        disagreements.append((template, "no-rule", [u for u in urls if _first_match(u)][:3]))
    return disagreements, decided_by_segments, rule is not None


def test_differential_against_re_search():
    disagreements: list[tuple] = []
    decided_by_segments = 0
    decided_routes = 0
    for template, requirements, defaults in TEMPLATES:
        urls = _urls(template, requirements, defaults)
        assert urls, template
        found, segments, decided = _differential(template, urls, facts(requirements, defaults))
        disagreements += found
        decided_by_segments += segments
        decided_routes += decided
    assert disagreements == []
    # Not vacuous: the segment procedure decides what the head comparison could not.
    assert decided_by_segments >= 20
    assert decided_routes >= 20


def test_symfony_declarations_carry_placeholder_facts(tmp_path):
    php = tmp_path / "TeamController.php"
    php.write_text(
        "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route;\n"
        "#[Route('/teams', requirements: ['team' => '\\d+'])]\n"
        "class TeamController {\n"
        "    const REQ = '\\d+';\n"
        "    #[Route('/time-tracker/{id}', requirements: ['id' => '\\d+'])]\n"
        "    public function tracker(int $id) {}\n"
        "    #[Route('/time-tracker/{id}', requirements: ['id' => '\\d+'])]\n"
        "    public function optional(int $id = 1) {}\n"
        "    #[Route('/{team}')]\n"
        "    public function team($team) {}\n"
        "    #[Route('/appraisals/{a}/users/{u}',"
        " requirements: ['a' => '\\d+', 'u' => static::REQ])]\n"
        "    public function unread() {}\n"
        "    #[Route('/appraisals/{a}/users/{u}',"
        " requirements: ['a' => '\\d+', 'u' => self::REQ])]\n"
        "    public function constant() {}\n"
        "    #[Route('/appraisals/{a}/users/{u}', requirements: ['a' => '\\d+', 'u' => '\\d+'],"
        " defaults: ['u' => 1])]\n"
        "    public function defaulted() {}\n"
        "}\n",
        encoding="utf-8",
    )
    security = tmp_path / "config" / "packages" / "security.yaml"
    security.parent.mkdir(parents=True)
    security.write_text(
        "security:\n  access_control:\n"
        + "".join(f"    - {{ path: '{p}', roles: [R] }}\n" for p in ORDERED),
        encoding="utf-8",
    )
    resolution = TreeSitterRouteSource(providers=(symfony.PROVIDER,)).declarations(
        str(tmp_path), ["TeamController.php"]
    )
    by_method = {d.controller_method: d for d in resolution.declarations}
    known = {m: d.placeholder_facts for m, d in by_method.items()}
    assert all(isinstance(f, ac.PlaceholderFacts) for f in known.values())
    assert known["tracker"] == ac.PlaceholderFacts(
        requirements=(("team", DIGITS), ("id", DIGITS)), defaults=(), unread=False
    )
    assert getattr(known["optional"], "defaults", None) == ("id",)
    assert ("u", None) in getattr(known["unread"], "requirements", ())
    assert getattr(known["constant"], "requirements", None) == (
        ("team", DIGITS),
        ("a", DIGITS),
        ("u", DIGITS),
    )
    assert getattr(known["defaulted"], "defaults", None) == ("u",)

    guarded = {g.route_key: g.node_key for g in resolution.route_guards}
    rule_of = {m: guarded.get(d.node_key) for m, d in by_method.items()}
    assert rule_of == {
        "tracker": f"access_control#{ORDERED.index(R_TEAM_TRACKER)}",
        "optional": None,
        "team": f"access_control#{ORDERED.index('^/teams')}",
        "unread": None,
        "constant": f"access_control#{ORDERED.index(R_TEAM_APPRAISAL)}",
        "defaulted": None,
    }
    assert by_method["optional"].node_key in resolution.route_undecided
    assert ac.REASON_SEGMENT_FACTS_UNREAD not in resolution.pass_reasons

    # An import with `requirements:` may override the attribute's: only the head is compared.
    routes = tmp_path / "config" / "routes.yaml"
    routes.write_text(
        "c: {resource: ../src/, type: attribute, requirements: {id: '.+'}}\n", encoding="utf-8"
    )
    assert ac.segment_facts_blocked(str(tmp_path))
    blocked = TreeSitterRouteSource(providers=(symfony.PROVIDER,)).declarations(
        str(tmp_path), ["TeamController.php"]
    )
    assert blocked.route_guards == []
    assert blocked.pass_reasons[ac.REASON_SEGMENT_FACTS_UNREAD] == 1
    routes.write_text("c: {resource: ../src/, type: attribute}\n", encoding="utf-8")
    assert not ac.segment_facts_blocked(str(tmp_path))


def _routes_of(tmp_path, source: str):
    """Declarations of one controller file, with `ORDERED` as the `access_control` list."""
    (tmp_path / "Controller.php").write_text(
        "<?php namespace App; use Symfony\\Component\\Routing\\Attribute\\Route;\n" + source,
        encoding="utf-8",
    )
    security = tmp_path / "config" / "packages" / "security.yaml"
    security.parent.mkdir(parents=True)
    security.write_text(
        "security:\n  access_control:\n"
        + "".join(f"    - {{ path: '{p}', roles: [R] }}\n" for p in ORDERED),
        encoding="utf-8",
    )
    return TreeSitterRouteSource(providers=(symfony.PROVIDER,)).declarations(
        str(tmp_path), ["Controller.php"]
    )


def _route(method: str, template: str, requirements: dict[str, str]) -> str:
    entries = ", ".join(f"'{k}' => {v}" for k, v in requirements.items())
    return (
        f"    #[Route('{template}', requirements: [{entries}])]\n"
        f"    public function {method}() {{}}\n"
    )


# Templates whose requirements are written as same-class `self::` constants, the value each one
# must read as, for the differential check.
REQUIREMENT_CONSTANTS = {
    "NUM": DIGITS,
    "HEAD": r"\d",
    "PLUS": "+",
    "WORD": "time-tracker",
    "SUBMIT": "submit",
    "KINDS": "self-review|manager",
    "ACTIONS": "logout|register",
}
SELF_NUM = {"t": "self::NUM", "a": "self::NUM", "p": "self::NUM"}
CONSTANT_TEMPLATES: list[tuple[str, dict[str, str], dict[str, str]]] = [
    ("/api/v1/users/{id}/time-tracker", {"id": "self::NUM"}, {"id": DIGITS}),
    (
        "/api/v1/users/{id}/{what}",
        {"id": "self::HEAD . '+'", "what": "self::WORD . '|' . self::WORD . 's'"},
        {"id": DIGITS, "what": "time-tracker|time-trackers"},
    ),
    (
        PARTICIPANT + "/review/{x}",
        {**SELF_NUM, "x": "self::SUBMIT"},
        {"t": DIGITS, "a": DIGITS, "p": DIGITS, "x": "submit"},
    ),
    (
        "/api/v1/appraisals/{a}/users/{u}/{k}",
        {"a": "self::NUM", "u": "self::HEAD . self::PLUS", "k": "self::KINDS"},
        {"a": DIGITS, "u": DIGITS, "k": "self-review|manager"},
    ),
    ("/users/{action}", {"action": "self::ACTIONS"}, {"action": "logout|register"}),
    ("/users/{action}", {"action": "self::NUM"}, {"action": DIGITS}),
    (
        "/teams/appraisals/{a}/users/{u}",
        {"a": "self::NUM", "u": "self::NUM"},
        {"a": DIGITS, "u": DIGITS},
    ),
    ("/teams/time-tracker/{id}", {"id": "self::NUM"}, {"id": DIGITS}),
]

# Same-class array constants, written into the same class as `REQUIREMENT_CONSTANTS`.
REQUIREMENT_ARRAYS = {
    "IDS": "['id' => self::NUM]",
    "PARTICIPANT_REQS": "['t' => self::NUM, 'a' => '\\d+', 'p' => self::HEAD . self::PLUS]",
    "REVIEW_REQS": "[...self::PARTICIPANT_REQS, 'x' => self::SUBMIT]",
    "LOOSE_USER": "['a' => '\\d+', 'u' => '[^/]+']",
    "APPRAISAL_REQS": "[...self::LOOSE_USER, 'u' => self::NUM]",
    "ACTION_OVERRIDE": "['action' => self::ACTIONS]",
    "ACTIONS_REQS": "['action' => 'register', ...self::ACTION_OVERRIDE]",
    "TRACKER": "[...self::IDS, ...self::IDS]",
}
# `requirements: <expr>` written whole, and the requirements it must read as.
ARRAY_CONSTANT_TEMPLATES: list[tuple[str, str, dict[str, str]]] = [
    ("/teams/time-tracker/{id}", "self::IDS", {"id": DIGITS}),
    (
        PARTICIPANT + "/review/{x}",
        "self::REVIEW_REQS",
        {"t": DIGITS, "a": DIGITS, "p": DIGITS, "x": "submit"},
    ),
    ("/teams/appraisals/{a}/users/{u}", "self::APPRAISAL_REQS", {"a": DIGITS, "u": DIGITS}),
    ("/users/{action}", "self::ACTIONS_REQS", {"action": "logout|register"}),
    ("/api/v1/users/{id}/time-tracker", "self::TRACKER", {"id": DIGITS}),
    (
        "/api/v1/appraisals/{a}/users/{u}/{k}",
        "[...self::LOOSE_USER, 'u' => self::NUM, 'k' => self::KINDS]",
        {"a": DIGITS, "u": DIGITS, "k": "self-review|manager"},
    ),
    (
        "/users/{action}",
        "['action' => '[^/]+', ...self::ACTION_OVERRIDE]",
        {"action": "logout|register"},
    ),
]


def _route_with(method: str, template: str, requirements: str) -> str:
    return (
        f"    #[Route('{template}', requirements: {requirements})]\n"
        f"    public function {method}() {{}}\n"
    )


def test_differential_with_requirements_from_constants(tmp_path):
    source = (
        "class ConstantController {\n"
        + "".join(f"    const {k} = '{v}';\n" for k, v in REQUIREMENT_CONSTANTS.items())
        + "".join(f"    const {k} = {v};\n" for k, v in REQUIREMENT_ARRAYS.items())
        + "".join(_route(f"r{i}", t, php) for i, (t, php, _) in enumerate(CONSTANT_TEMPLATES))
        + "".join(
            _route_with(f"w{i}", t, php) for i, (t, php, _) in enumerate(ARRAY_CONSTANT_TEMPLATES)
        )
        + "}\n"
    )
    resolution = _routes_of(tmp_path, source)
    by_method = {d.controller_method: d for d in resolution.declarations}
    guarded = {g.route_key for g in resolution.route_guards}
    cases = [(f"r{i}", t, r) for i, (t, _, r) in enumerate(CONSTANT_TEMPLATES)] + [
        (f"w{i}", t, r) for i, (t, _, r) in enumerate(ARRAY_CONSTANT_TEMPLATES)
    ]
    disagreements: list[tuple] = []
    decided_routes = 0
    for method, template, resolved in cases:
        declaration = by_method[method]
        known = declaration.placeholder_facts
        assert known is not None, method
        assert known == ac.PlaceholderFacts(
            requirements=tuple(resolved.items()), defaults=(), unread=False
        ), method
        assert (declaration.template, declaration.template_reason) == (template, ""), method
        urls = _urls(template, resolved, ())
        assert urls, method
        found, _, decided = _differential(template, urls, known)
        disagreements += found
        decided_routes += decided
        assert (declaration.node_key in guarded) == decided, method
    assert disagreements == []
    # Every route here is decided once its constant reads; without it each one is undecided.
    assert decided_routes == len(cases)


# `requirements: ['u' => <expr>]` that must stay unread, by method name.
UNREAD_REQUIREMENTS = {
    "notString": "self::NUM",
    "declaredTwice": "self::TWICE",
    "concatenatedValue": "self::BUILT",
    "otherClass": "Other::REQ",
    "lateBound": "static::REQ",
    "parentScope": "parent::INHERITED",
    "inherited": "self::INHERITED",
    "fromInterface": "self::IFACE",
    "undeclared": "self::MISSING",
}
APPRAISAL = "/teams/appraisals/{a}/users/{u}"


def test_requirement_constant_reads_only_from_its_own_class_body(tmp_path):
    source = (
        "interface HasReq { const IFACE = '\\d+'; }\n"
        "class Base { const INHERITED = '\\d+'; }\n"
        "class Other { const REQ = '\\d+'; }\n"
        "trait Routes {\n"
        "    const TRAIT_REQ = '\\d+';\n"
        + _route("inTrait", APPRAISAL, {"a": "'\\d+'", "u": "self::TRAIT_REQ"})
        + "}\n"
        "class AppraisalController extends Base implements HasReq {\n"
        "    const REQ = '\\d+';\n"
        "    const HEAD = '\\d';\n"
        "    const NUM = 1;\n"
        "    const TWICE = '\\d+';\n"
        "    const TWICE = '\\d+';\n"
        "    const BUILT = '\\d' . '+';\n"
        + _route("constant", APPRAISAL, {"a": "'\\d+'", "u": "self::REQ"})
        + _route("concatenated", APPRAISAL, {"a": "'\\d+'", "u": "self::HEAD . '+'"})
        + "".join(
            _route(m, APPRAISAL, {"a": "'\\d+'", "u": e}) for m, e in UNREAD_REQUIREMENTS.items()
        )
        + "}\n"
    )
    resolution = _routes_of(tmp_path, source)
    by_method = {d.controller_method: d for d in resolution.declarations}
    guarded = {g.route_key: g.node_key for g in resolution.route_guards}
    assert set(by_method) == {"constant", "concatenated", "inTrait", *UNREAD_REQUIREMENTS}

    appraisal_rule = f"access_control#{ORDERED.index(R_TEAM_APPRAISAL)}"
    for method in ("constant", "concatenated"):
        declaration = by_method[method]
        assert declaration.placeholder_facts == ac.PlaceholderFacts(
            requirements=(("a", DIGITS), ("u", DIGITS)), defaults=(), unread=False
        ), method
        assert declaration.template_reason == "", method
        assert guarded.get(declaration.node_key) == appraisal_rule, method

    for method in ("inTrait", *UNREAD_REQUIREMENTS):
        declaration = by_method[method]
        known = declaration.placeholder_facts
        assert known is not None and ("u", None) in known.requirements, method
        assert declaration.template_reason == symfony.TEMPLATE_REQUIREMENT_UNREAD, method
        assert declaration.node_key not in guarded, method
        assert declaration.node_key in resolution.route_undecided, method


GOALS = "/api/v1/teams/{me}/goals/{cycle}/users/{user}/goals"


def test_whole_requirements_array_constant_resolves(tmp_path):
    # The shape of `Rest/Goals/TeamController`: a typed array constant, spread into another.
    source = (
        "class TeamController {\n"
        "    private const string PATH = '" + GOALS + "';\n"
        "    private const string NUM = '\\d+';\n"
        "    private const array REQUIREMENTS ="
        " ['me' => '\\d+', 'cycle' => '\\d{4}', 'user' => self::NUM];\n"
        "    private const array GOAL_REQUIREMENTS = [...self::REQUIREMENTS, 'goal' => '\\d+'];\n"
        "    #[Route(self::PATH, requirements: self::REQUIREMENTS, methods: ['GET'])]\n"
        "    public function listing() {}\n"
        "    #[Route(self::PATH . '/{goal}', requirements: self::GOAL_REQUIREMENTS)]\n"
        "    public function goal() {}\n"
        "}\n"
        "#[Route('/teams', requirements: self::TEAM)]\n"
        "class PrefixedController {\n"
        "    const TEAM = ['a' => '[^/]+', 'u' => '\\d+'];\n"
        "    const APPRAISAL = ['a' => '\\d+'];\n"
        "    #[Route('/appraisals/{a}/users/{u}', requirements: self::APPRAISAL)]\n"
        "    public function appraisal() {}\n"
        "}\n"
    )
    resolution = _routes_of(tmp_path, source)
    by_method = {d.controller_method: d for d in resolution.declarations}
    base = (("me", DIGITS), ("cycle", r"\d{4}"), ("user", DIGITS))
    assert by_method["listing"].placeholder_facts == ac.PlaceholderFacts(
        requirements=base, defaults=(), unread=False
    )
    assert by_method["goal"].placeholder_facts == ac.PlaceholderFacts(
        requirements=(*base, ("goal", DIGITS)), defaults=(), unread=False
    )
    for method in ("listing", "goal"):
        assert by_method[method].template_reason == "", method

    # A class-level array constant merges under the method's, which wins for `a`.
    appraisal = by_method["appraisal"]
    assert appraisal.placeholder_facts == ac.PlaceholderFacts(
        requirements=(("a", "[^/]+"), ("u", DIGITS), ("a", DIGITS)), defaults=(), unread=False
    )
    assert appraisal.template_reason == ""
    guarded = {g.route_key: g.node_key for g in resolution.route_guards}
    rule = f"access_control#{ORDERED.index(R_TEAM_APPRAISAL)}"
    assert guarded.get(appraisal.node_key) == rule


def test_spread_override_changes_the_verdict(tmp_path):
    # `{user}` that may read `login` is not DISJOINT from `^/users/login`; once a later key
    # narrows it to digits, it is. The override must win over the spread, and only in order.
    source = (
        "class UserController {\n"
        "    const LOOSE = ['user' => 'login|logout'];\n"
        "    const NARROWED = [...self::LOOSE, 'user' => '\\d+'];\n"
        "    const WIDENED = ['user' => '\\d+', ...self::LOOSE];\n"
        + _route_with("loose", "/users/{user}", "self::LOOSE")
        + _route_with("narrowed", "/users/{user}", "self::NARROWED")
        + _route_with("widened", "/users/{user}", "self::WIDENED")
        + _route_with("inlineNarrowed", "/users/{user}", "[...self::LOOSE, 'user' => '\\d+']")
        + _route_with("inlineWidened", "/users/{user}", "['user' => '\\d+', ...self::LOOSE]")
        + "}\n"
    )
    by_method = {d.controller_method: d for d in _routes_of(tmp_path, source).declarations}
    verdicts = {}
    for method, declaration in by_method.items():
        known = declaration.placeholder_facts
        assert known is not None and not known.unread, method
        assert declaration.template is not None, method
        verdicts[method] = ac.path_verdict(declaration.template, R_LOGIN, known)
    assert verdicts == {
        "loose": U,
        "narrowed": D,
        "widened": U,
        "inlineNarrowed": D,
        "inlineWidened": U,
    }
    narrowed = by_method["narrowed"].placeholder_facts
    assert narrowed is not None and narrowed.requirements == (("user", DIGITS),)


# `requirements: <expr>` that must stay unread as a whole, by method name.
UNREAD_ARRAYS = {
    "cycle": "self::CYCLE_A",
    "selfReference": "self::SELF_REF",
    "declaredTwice": "self::TWICE",
    "lateBound": "static::REQS",
    "parentScope": "parent::INHERITED",
    "inherited": "self::INHERITED",
    "otherClass": "Other::REQS",
    "fromInterface": "self::IFACE",
    "undeclared": "self::MISSING",
    "intKey": "self::INT_KEY",
    "listElement": "self::LIST",
    "nonLiteralValue": "self::NON_LITERAL",
    "arrayValue": "self::ARRAY_VALUE",
    "spreadOther": "self::SPREAD_OTHER",
    "spreadStatic": "self::SPREAD_STATIC",
    "spreadString": "self::SPREAD_STRING",
    "spreadBroken": "self::SPREAD_BROKEN",
    "stringConstant": "self::STR",
    "inlineSpreadCycle": "['a' => '\\d+', ...self::CYCLE_A]",
    "inlineSpreadParent": "['a' => '\\d+', ...parent::INHERITED]",
}


def test_requirements_array_constant_unread_cases(tmp_path):
    source = (
        "interface HasReqs { const IFACE = ['a' => '\\d+', 'u' => '\\d+']; }\n"
        "class Base { const INHERITED = ['a' => '\\d+', 'u' => '\\d+']; }\n"
        "class Other { const REQS = ['a' => '\\d+', 'u' => '\\d+']; }\n"
        "trait Routes {\n"
        "    const TRAIT_REQS = ['a' => '\\d+', 'u' => '\\d+'];\n"
        + _route_with("inTrait", APPRAISAL, "self::TRAIT_REQS")
        + "}\n"
        "class AppraisalController extends Base implements HasReqs {\n"
        "    const STR = '\\d+';\n"
        "    const REQS = ['a' => '\\d+', 'u' => '\\d+'];\n"
        "    const CYCLE_A = [...self::CYCLE_B, 'a' => '\\d+'];\n"
        "    const CYCLE_B = [...self::CYCLE_A, 'u' => '\\d+'];\n"
        "    const SELF_REF = ['a' => '\\d+', 'u' => '\\d+', ...self::SELF_REF];\n"
        "    const TWICE = ['a' => '\\d+', 'u' => '\\d+'];\n"
        "    const TWICE = ['a' => '\\d+', 'u' => '\\d+'];\n"
        "    const INT_KEY = ['a' => '\\d+', 'u' => '\\d+', 1 => '\\d+'];\n"
        "    const LIST = ['a' => '\\d+', 'u' => '\\d+', '\\d+'];\n"
        "    const NON_LITERAL = ['a' => '\\d+', 'u' => SOME_GLOBAL];\n"
        "    const ARRAY_VALUE = ['a' => '\\d+', 'u' => self::REQS];\n"
        "    const SPREAD_OTHER = [...Other::REQS];\n"
        "    const SPREAD_STATIC = [...static::REQS];\n"
        "    const SPREAD_STRING = ['a' => '\\d+', 'u' => '\\d+', ...self::STR];\n"
        "    const SPREAD_BROKEN = [...self::INT_KEY, 'a' => '\\d+', 'u' => '\\d+'];\n"
        + _route_with("readable", APPRAISAL, "self::REQS")
        + "".join(_route_with(m, APPRAISAL, e) for m, e in UNREAD_ARRAYS.items())
        + "}\n"
    )
    resolution = _routes_of(tmp_path, source)
    by_method = {d.controller_method: d for d in resolution.declarations}
    guarded = {g.route_key: g.node_key for g in resolution.route_guards}
    assert set(by_method) == {"readable", "inTrait", *UNREAD_ARRAYS}

    # The control: the same array, readable, decides the route.
    readable = by_method["readable"]
    assert readable.placeholder_facts == ac.PlaceholderFacts(
        requirements=(("a", DIGITS), ("u", DIGITS)), defaults=(), unread=False
    )
    rule = f"access_control#{ORDERED.index(R_TEAM_APPRAISAL)}"
    assert guarded.get(readable.node_key) == rule

    for method in ("inTrait", *UNREAD_ARRAYS):
        declaration = by_method[method]
        known = declaration.placeholder_facts
        assert known is not None and known.unread, method
        if not method.startswith("inline"):
            # Unread as a whole: not even the readable keys survive.
            assert known.requirements == (), method
        assert declaration.template_reason == symfony.TEMPLATE_REQUIREMENT_UNREAD, method
        assert declaration.node_key not in guarded, method
        assert declaration.node_key in resolution.route_undecided, method


# Bounded digit requirements: `\d{n}` and `\d{n,m}` with n >= 1 are digits of a known length
# range. Forms that may be empty, are unbounded, or use `[0-9]` stay unread.
YEAR = r"\d{4}"
SHORT = r"\d{2,3}"


def test_bounded_digits_against_digit_and_literal_segments():
    tracker = "/api/v1/users/{id}/time-tracker"
    assert verdict(tracker, R_USER_TRACKER, {"id": YEAR}) == M
    assert verdict(tracker, R_USER_TRACKER, {"id": SHORT}) == M
    assert verdict(tracker + "/{day}", R_USER_TRACKER, {"id": YEAR}) == M
    assert verdict("/api/v1/users/{id}", R_USER_TRACKER, {"id": YEAR}) == D
    assert verdict("/teams/time-tracker/{id}", R_TEAM_TRACKER, {"id": r"\d{1}"}) == M
    assert verdict("/x/{id}", "^/x/[^/]+$", {"id": YEAR}) == M

    # A full literal segment: all digits of an allowed length is UNDECIDED, otherwise DISJOINT.
    assert verdict("/x/{id}/y", "^/x/12/y", {"id": SHORT}) == U
    assert verdict("/x/{id}/y", "^/x/123/y", {"id": SHORT}) == U
    assert verdict("/x/{id}/y", "^/x/1/y", {"id": SHORT}) == D
    assert verdict("/x/{id}/y", "^/x/1234/y", {"id": SHORT}) == D
    assert verdict("/x/{id}/y", "^/x/abc/y", {"id": SHORT}) == D
    assert verdict("/x/{id}", "^/x/12$", {"id": SHORT}) == U
    assert verdict("/x/{id}", "^/x/1$", {"id": SHORT}) == D
    # A prefix literal: any all-digit literal no longer than the maximum may start a value.
    assert verdict("/x/{id}", "^/x/1", {"id": SHORT}) == U
    assert verdict("/x/{id}", "^/x/123", {"id": SHORT}) == U
    assert verdict("/x/{id}", "^/x/1234", {"id": SHORT}) == D
    assert verdict("/x/{id}", "^/x/a", {"id": SHORT}) == D
    assert verdict("/x/{id}", "^/x/", {"id": SHORT}) == M


def test_bounded_digit_forms_that_stay_unread():
    kind = ac._value_kind
    assert kind("id", YEAR, set()) == ("digits", 4, 4)
    assert kind("id", SHORT, set()) == ("digits", 2, 3)
    for requirement in (
        r"\d{4,}",
        r"\d{0,2}",
        r"\d{0}",
        r"\d{0,0}",
        r"\d{04}",
        r"\d{3,2}",
        r"\d{,3}",
        r"\d{2, 3}",
        r"\d{70000}",
        r"[0-9]{4}",
        r"\d{4}x",
        r"\d{4}?",
    ):
        assert kind("id", requirement, set()) is ac._UNKNOWN, requirement
    tracker = "/api/v1/users/{id}/time-tracker"
    for requirement in (r"\d{4,}", r"\d{0,2}", r"[0-9]{4}"):
        assert verdict(tracker, R_USER_TRACKER, {"id": requirement}) == U, requirement
    # A defaulted bounded placeholder stays unread like any other.
    assert verdict(tracker, R_USER_TRACKER, {"id": YEAR}, ("id",)) == U


def test_bounded_digit_template_keeps_the_placeholder():
    for requirement in (YEAR, SHORT):
        assert symfony.symfony_template("/users/{cycle}/x", {"cycle": requirement}) == (
            "/users/{cycle}/x",
            "",
        )
        assert symfony.symfony_template(f"/users/{{cycle<{requirement}>}}/x") == (
            "/users/{cycle}/x",
            "",
        )


BOUNDED_RULES = [
    R_USER_TRACKER,
    R_TEAM_TRACKER,
    r"^/x/\d+/y$",
    r"^/x/[^/]+$",
    "^/x/1",
    "^/x/12",
    "^/x/123",
    "^/x/1234",
    "^/x/12345",
    "^/x/12/y",
    "^/x/123/y$",
    "^/x/1234/y",
    "^/x/9$",
    "^/x/abc",
    "^/x/",
    "^/x/1x",
]
BOUNDED_TEMPLATES = [
    "/api/v1/users/{id}/time-tracker",
    "/api/v1/users/{id}",
    "/teams/time-tracker/{id}",
    "/x/{id}",
    "/x/{id}/y",
    "/x/{id}/{k}",
]
BOUNDED_REQUIREMENTS = [r"\d{1}", r"\d{2}", r"\d{4}", r"\d{1,2}", r"\d{2,3}", r"\d{3,5}"]


def _digit_values(low: int, high: int) -> list[str]:
    """Values of every allowed length and of both boundary lengths outside the range."""
    values = []
    for length in range(max(low - 1, 0), high + 2):
        values += ["1" * length, "9" * length, "123456"[:length], "000000"[:length]]
    return list(dict.fromkeys(values))


def test_bounded_digits_differential_against_re_search():
    disagreements: list[tuple] = []
    decided = 0
    for requirement in BOUNDED_REQUIREMENTS:
        lengths = ac._digit_lengths(ac._value_kind("id", requirement, set()))
        assert lengths is not None and lengths[1] is not None, requirement
        low, high = lengths[0], lengths[1]
        candidates = _digit_values(low, high)
        values = [v for v in candidates if re.fullmatch(requirement, v)]
        assert {len(v) for v in values} == set(range(low, high + 1)), requirement
        # The boundary lengths are generated and rejected by the requirement itself.
        assert {len(v) for v in candidates if v not in values} >= {low - 1, high + 1} - {-1, 0}
        for template in BOUNDED_TEMPLATES:
            filled = [template.replace("{id}", v) for v in values]
            urls = {url.replace("{k}", k) for url in filled for k in ("y", "z", "1")}
            known = facts({"id": requirement})
            for regex in BOUNDED_RULES:
                got = ac.path_verdict(template, regex, known)
                hits = {bool(re.search(regex, url, re.ASCII)) for url in urls}
                if (got == M and hits != {True}) or (got == D and hits != {False}):
                    disagreements.append((template, requirement, regex, got, hits))
                old = ac.path_verdict(template, regex)
                if old != U and got != old:
                    disagreements.append((template, requirement, regex, "old", old, got))
                decided += old == U and got != U
    assert disagreements == []
    assert decided >= 100
