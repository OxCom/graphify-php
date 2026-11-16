"""Which `security.access_control` rule applies to a route, when that can be proved.

Placed in `syntax/` rather than `config/`: the decision is pure string work, and importing
anything under `config/` runs that package's `__init__`, which needs pyyaml.

Symfony's `AccessMap::getPatterns()` walks the rules in order and applies the FIRST one whose
matchers all match the request. The path matcher is
`preg_match('{'.$path.'}', rawurldecode($request->getPathInfo()))`: a search, anchored only
where the pattern itself says `^`. So a route is edged to rule k only when, for EVERY URL its
template can produce, rules 0..k-1 provably do not match and rule k provably does. Anything
short of that proof is `access-control-undecided` and no edge: a wrong guard edge tells a
reader a route is protected by a rule that does not apply to it.

What is read: the top-level `security.access_control` list of `config/packages/security.yaml`
only. `config_blocker` refuses every edge of the build instead of guessing in two cases:

- `access_control` may be declared elsewhere: under a `when@<env>` key of `security.yaml`, in
  any other YAML file under `config/packages/`, or in a PHP/XML file there whose text names
  `access_control`/`accessControl` (`access-control-overridden-elsewhere`);
- a routes-config import carries a `prefix`, so the attribute-declared `route_path` may not be
  the served path (`access-control-route-prefix-unread`). Routing YAML is parsed; routing PHP and
  XML and `src/Kernel.php` get a text check for `prefix(`, `prefix=` and `->prefix` only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .routes import requirement_may_match_slash

SECURITY_YAML = "config/packages/security.yaml"

MATCH = "match"
DISJOINT = "disjoint"
UNDECIDED = "undecided"

# Fixed reasons. Never a variable inside one.
REASON_UNDECIDED = "access-control-undecided"
REASON_NO_RULE = "access-control-no-rule"
REASON_NO_PATH = "access-control-no-path"
REASON_PYYAML_MISSING = "access-control-pyyaml-missing"
REASON_UNPARSED = "access-control-unparsed"
REASON_NOT_A_LIST = "access-control-not-a-list"
REASON_ROUTE_PREFIX_UNREAD = "access-control-route-prefix-unread"
REASON_OVERRIDDEN_ELSEWHERE = "access-control-overridden-elsewhere"
# Counted once per build: only the literal-head comparison runs (`segment_facts_blocked`).
REASON_SEGMENT_FACTS_UNREAD = "access-control-segment-facts-unread"

ROUTES_DIR = "config/routes"
PACKAGES_DIR = "config/packages"
KERNEL_PHP = "src/Kernel.php"
# Text markers for files this module does not parse (routing PHP/XML, security PHP/XML).
_PREFIX_MARKERS = ("prefix(", "prefix=", "->prefix")
_ACCESS_CONTROL_MARKERS = ("access_control", "accessControl")
# Same cap as `config.safe_yaml.MAX_BYTES`; a larger file is treated as unreadable.
_MAX_TEXT_BYTES = 2 * 1024 * 1024

# Keys that do not restrict which requests a rule matches. Every other key is a matcher this
# module does not evaluate (host, methods, ips, port, attributes, route, request_matcher, and
# any key a later Symfony adds), so such a rule is only ever proved disjoint by its path.
_NON_MATCHER_KEYS = frozenset({"path", "roles", "allow_if", "requires_channel"})

# `RouteCompiler::SEPARATORS`. The separator before an optional placeholder belongs to the
# optional part, so `/blog/{page}` with a default also serves `/blog`.
_ROUTE_SEPARATORS = "/,;.:-_~+*=@|"

_REGEX_META = ".^$*+?()[]{}|\\"
# Escapes that mean the same in PCRE and Python's `re` with `re.ASCII`.
_SAFE_ESCAPES = frozenset("dDwWsS" + ".^$*+?()[]{}|\\/-")


@dataclass(frozen=True)
class AccessRule:
    """One `access_control` entry, as far as matching and labelling need it."""

    index: int
    path: object  # the `path` value as written; only a str is ever evaluated
    roles: tuple[str, ...] = ()
    allow_if: str | None = None
    other_matchers: bool = False

    @classmethod
    def from_config(cls, index: int, entry) -> AccessRule:
        if not isinstance(entry, dict):
            # Not a rule this module can read, so nothing after it can be proved either.
            return cls(index, None, other_matchers=True)
        roles = entry.get("roles")
        if roles is None:
            role_list: tuple[str, ...] = ()
        elif isinstance(roles, (list, tuple)):
            role_list = tuple(str(r) for r in roles)
        else:
            role_list = (str(roles),)
        allow_if = entry.get("allow_if")
        return cls(
            index=index,
            path=entry.get("path"),
            roles=role_list,
            allow_if=None if allow_if is None else str(allow_if),
            other_matchers=any(key not in _NON_MATCHER_KEYS for key in entry),
        )

    @property
    def node_key(self) -> str:
        return f"access_control#{self.index}"

    @property
    def label(self) -> str:
        roles = f"roles: [{', '.join(self.roles)}]"
        if self.allow_if is None:
            return roles
        if self.roles:
            return f"{roles}; allow_if: {self.allow_if}"
        return f"allow_if: {self.allow_if}"


@dataclass(frozen=True)
class PlaceholderFacts:
    """What a route declaration says about its placeholders, read by `segment_verdict` only.

    Never published on a node. `requirements` are the `requirements:` option entries in merge
    order (a later entry wins), None for a value that is not a literal. `defaults` are the
    names with a default: the `defaults:` option and controller parameters with a default
    value, which Symfony's attribute loader turns into route defaults. `unread` says that one
    of those lists held an entry this package could not read, so any placeholder may differ.
    """

    requirements: tuple[tuple[str, str | None], ...] = ()
    defaults: tuple[str, ...] = ()
    unread: bool = False


def first_rule(
    template: str, rules: Sequence[AccessRule], facts: PlaceholderFacts | None = None
) -> tuple[AccessRule | None, str]:
    """The rule Symfony applies to every URL of `template`, or None and a fixed reason.

    `facts` enables the segment procedure (`segment_verdict`); without it only the literal
    head of the template is compared.
    """
    for rule in rules:
        verdict = rule_verdict(template, rule, facts)
        if verdict == DISJOINT:
            continue
        if verdict == MATCH:
            return rule, ""
        return None, REASON_UNDECIDED
    return None, REASON_NO_RULE


def rule_verdict(template: str, rule: AccessRule, facts: PlaceholderFacts | None = None) -> str:
    """MATCH, DISJOINT or UNDECIDED for every URL of `template` against one rule."""
    if not isinstance(rule.path, str):
        # No path means every path; a non-string one is not something to evaluate.
        return UNDECIDED
    verdict = path_verdict(template, rule.path, facts)
    if rule.other_matchers and verdict != DISJOINT:
        return UNDECIDED
    return verdict


def path_verdict(template: str, regex: str, facts: PlaceholderFacts | None = None) -> str:
    """MATCH, DISJOINT or UNDECIDED for every URL of `template` against one path regex.

    The literal-head comparison runs first and its decided answer is final. Only an undecided
    head is handed to `segment_verdict`, and only when the declaration's `facts` are known.
    """
    verdict = _head_verdict(template, regex)
    if verdict != UNDECIDED or facts is None or not _gated(template, regex):
        return verdict
    return segment_verdict(template, regex, facts)


def _url(template: str) -> str:
    # `Route::setPath()`: `'/'.ltrim(trim($pattern), '/')`.
    return "/" + template.strip().lstrip("/")


def _gated(template: str, regex: str) -> bool:
    """False when the rule or the URL holds something neither comparison evaluates."""
    # `%` is a container parameter in the rule, or a percent-escape the request decodes.
    return "%" not in regex and "%" not in _url(template) and _portable(regex)


def _head_verdict(template: str, regex: str) -> str:
    """The literal-head comparison: the template's text before its first `{` only."""
    url = _url(template)
    if not _gated(template, regex):
        return UNDECIDED
    if "{" not in url:
        try:
            return MATCH if re.search(regex, url, re.ASCII) else DISJOINT
        except re.error:
            return UNDECIDED

    head = url[: url.index("{")]
    # Every URL of the template starts with `head`, minus a trailing separator that an optional
    # placeholder takes with it.
    if head and head[-1] in _ROUTE_SEPARATORS:
        head = head[:-1]
    if not regex.startswith("^") or _top_level_alternation(regex):
        return UNDECIDED
    prefix, rest, quantified = _literal_prefix(regex[1:])

    if not head.startswith(prefix):
        if prefix.startswith(head):
            # A placeholder may produce the rest of the prefix.
            return UNDECIDED
        return DISJOINT
    if rest == "" and not quantified:
        return MATCH
    if quantified or len(head) <= len(prefix):
        return UNDECIDED
    following = head[len(prefix)]
    if rest.startswith("$") and following != "\n":
        # `^P$` matches only P itself (or P and a final newline); every URL is longer.
        return DISJOINT
    if _requires_digit(rest) and following not in "0123456789":
        return DISJOINT
    return UNDECIDED


def _portable(regex: str) -> bool:
    """True when the pattern means the same in PCRE (inside `{...}`) and in Python `re`."""
    if "[:" in regex or "[[" in regex or "{," in regex:
        return False
    depth = 0
    i = 0
    while i < len(regex):
        c = regex[i]
        if c == "\\":
            if i + 1 >= len(regex) or regex[i + 1] not in _SAFE_ESCAPES:
                return False
            i += 2
            continue
        if c == "(" and regex.startswith("(?", i) and not regex.startswith("(?:", i):
            return False
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                # Unbalanced `}` closes Symfony's `{...}` delimiter early.
                return False
        i += 1
    return depth == 0


def _top_level_alternation(regex: str) -> bool:
    depth = 0
    in_class = False
    i = 0
    while i < len(regex):
        c = regex[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


def _literal_prefix(body: str) -> tuple[str, str, bool]:
    """(literal prefix, the regex after it, whether a quantifier cut the prefix short).

    `\\/` and `\\-` are literal; any other escape or metacharacter ends the prefix. A quantifier
    right after the prefix applies to its last character, which is then dropped from it.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            if i + 1 < len(body) and body[i + 1] in "/-":
                out.append(body[i + 1])
                i += 2
                continue
            break
        if c in _REGEX_META:
            break
        out.append(c)
        i += 1
    rest = body[i:]
    quantified = bool(rest) and rest[0] in "?*+{"
    if quantified and out:
        out.pop()
    return "".join(out), rest, quantified


def _requires_digit(rest: str) -> bool:
    """True when the next atom is `\\d` or `[0-9]` and is not optional."""
    for atom in ("\\d", "[0-9]"):
        if rest.startswith(atom):
            after = rest[len(atom) :]
            return not after or after[0] not in "?*{"
    return False


# The segment procedure. A URL of the template is `/s1/s2/.../sn`; the rule is a closed
# subset of regexes that also splits on `/`. Each side's segments are compared as LANGUAGES
# (value sets), never by sampling. A segment kind is one of these; a literal is a str.
_DIGITS = ("digits",)  # `\d+`
# `\d{n}` and `\d{n,m}` read as ("digits", n, m) with 1 <= n <= m. Forms that may be empty or
# are unbounded stay unread. 65535 is PCRE's quantifier limit; a larger bound does not compile.
_BOUNDED_DIGITS = re.compile(r"\\d\{([1-9][0-9]{0,4})(?:,([1-9][0-9]{0,4}))?\}")
_QUANTIFIER_LIMIT = 65535
_ANY = ("any",)  # `[^/]+`, Symfony's default requirement for a whole-segment placeholder
_UNKNOWN = ("unknown",)  # a template segment this procedure does not evaluate

# `Route::extractInlineDefaultsAndRequirements`, as `frameworks/symfony.py` reads it.
_PLACEHOLDER = re.compile(r"\{(!?)(\w+)(?::\w+(?:\.\w+)?)?(<.*?>)?(\?[^}]*)?\}")
_MARK = "\x00"
_MARKED_SEGMENT = re.compile(r"\x00(\d+)\x00")
_WORD_ALTERNATION = re.compile(r"[A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)*")
# Placeholders whose value Symfony may set from configuration (`framework.default_locale`,
# the request format), whatever the route declares.
_SPECIAL_PLACEHOLDERS = frozenset({"_locale", "_format"})
# Word-alternation placeholders are expanded into literal templates; more than this many is
# left undecided rather than enumerated.
_EXPANSION_CAP = 64
_DIGIT_CHARS = frozenset("0123456789")
# Escapes read as the literal character in a rule's literal text.
_LITERAL_ESCAPES = frozenset(".^$*+?()[]{}|\\-")

_FULL = "full"  # the rule segment is followed by `/`: it matches the whole URL segment
_FULL_END = "full-end"  # the last rule segment before `$`
_PREFIX = "prefix"  # the last rule segment without `$`: it matches a prefix of the segment


def segment_verdict(template: str, regex: str, facts: PlaceholderFacts) -> str:
    """MATCH, DISJOINT or UNDECIDED, deciding segment by segment by language.

    The rule must be in the closed subset `_rule_alternatives` parses and every placeholder
    must have a supported value set; anything else is UNDECIDED. Without a final `$` a rule
    is a PREFIX match: `^/users/login` also matches `/users/loginx` and `/users/login/x`, so
    its last segment is compared by prefix and the URL may run on past it.
    """
    if not _gated(template, regex):
        return UNDECIDED
    alternatives = _rule_alternatives(regex)
    shapes = _template_shapes(template, facts)
    if alternatives is None or shapes is None:
        return UNDECIDED
    verdicts = set()
    for segments, present in shapes:
        per_alternative = [_linear_verdict(segments, present, alt) for alt in alternatives]
        if MATCH in per_alternative:
            verdicts.add(MATCH)
        elif all(v == DISJOINT for v in per_alternative):
            verdicts.add(DISJOINT)
        else:
            verdicts.add(UNDECIDED)
    return verdicts.pop() if len(verdicts) == 1 else UNDECIDED


def _template_shapes(template: str, facts: PlaceholderFacts) -> list[tuple[list, int]] | None:
    """`(segments, present)` per expansion of the template, or None when it is not evaluated.

    `present` is how many leading segments every URL has: a trailing placeholder with a
    default may be absent together with the separator before it. Word alternations are
    expanded into literal segments, one shape per combination.
    """
    url = _url(template)
    if facts.unread or "\n" in url:
        return None
    matches = list(_PLACEHOLDER.finditer(url))
    names = [m.group(2) for m in matches]
    if len(set(names)) != len(names):
        return None
    options: dict[str, str | None] = dict(facts.requirements)
    defaults = set(facts.defaults)
    kinds: list[object] = []
    for match in matches:
        name = match.group(2)
        inline = match.group(3)[1:-1] if match.group(3) else None
        option = options.get(name)
        if name in options and (option is None or (inline is not None and inline != option)):
            # Not a literal, or an inline and an option requirement whose precedence is not read.
            return None
        requirement = inline if inline is not None else option
        if requirement is not None and requirement_may_match_slash(requirement):
            # A value with `/` shifts every later segment.
            return None
        if match.group(4) is not None:
            defaults.add(name)
        kinds.append(_value_kind(name, requirement, defaults))

    counter = iter(range(len(matches)))
    marked = _PLACEHOLDER.sub(lambda m: f"{_MARK}{next(counter)}{_MARK}", url)
    if "{" in marked or "}" in marked:
        return None
    segments: list[object] = []
    present: int | None = None
    for index, text in enumerate(marked[1:].split("/")):
        if _MARK not in text:
            segments.append(text)
            continue
        placed = [int(k) for k in _MARKED_SEGMENT.findall(text)]
        if present is None and any(names[k] in defaults for k in placed):
            present = index
        whole = _MARKED_SEGMENT.fullmatch(text)
        # Literal text and a placeholder in one segment (`page-{id}`) is not evaluated.
        segments.append(kinds[int(whole.group(1))] if whole else _UNKNOWN)
    if present is None:
        present = len(segments)

    shapes: list[tuple[list, int]] = [([], present)]
    for segment in segments:
        options_here = sorted(segment) if isinstance(segment, frozenset) else [segment]
        shapes = [(done + [value], present) for done, _ in shapes for value in options_here]
        if len(shapes) > _EXPANSION_CAP:
            return None
    return shapes


def _value_kind(name: str, requirement: str | None, defaults: set[str]) -> object:
    """The value set of a whole-segment placeholder.

    `_DIGITS`, bounded digits `("digits", n, m)`, `_ANY`, a word set, or `_UNKNOWN`.
    """
    if name in defaults or name in _SPECIAL_PLACEHOLDERS:
        return _UNKNOWN
    if requirement is None:
        return _ANY
    if requirement == "\\d+":
        return _DIGITS
    bounded = _BOUNDED_DIGITS.fullmatch(requirement)
    if bounded:
        low = int(bounded.group(1))
        high = int(bounded.group(2) or low)
        if low <= high <= _QUANTIFIER_LIMIT:
            return ("digits", low, high)
        return _UNKNOWN
    if _WORD_ALTERNATION.fullmatch(requirement):
        return frozenset(requirement.split("|"))
    return _UNKNOWN


def _rule_alternatives(regex: str) -> list[tuple[list, bool]] | None:
    """The rule as `(segment patterns, ends with $)` alternatives, or None outside the subset.

    The subset: `^`, then `/`-separated segments that are each a literal, `\\d+` or `[^/]+`,
    then an optional group of literal alternatives each starting with `/` (`(/a|/b)?`), then
    an optional `$`. The optional group expands into one alternative without it and one per
    branch, so the rule matches a URL when any alternative does.
    """
    if not regex.startswith("^"):
        return None
    base, i = _read_segments(regex, 1, stop="($")
    if base is None:
        return None
    branches: list[list] = []
    if regex.startswith("(", i):
        i += 3 if regex.startswith("(?:", i) else 1
        while True:
            branch, i = _read_segments(regex, i, stop="|)")
            if branch is None or branch[0] != "":
                # A branch must start with `/`, read as an empty first segment.
                return None
            branches.append(branch[1:])
            if regex.startswith("|", i):
                i += 1
                continue
            break
        if not regex.startswith(")?", i):
            return None
        i += 2
    end = regex.startswith("$", i)
    if end:
        i += 1
    if i != len(regex) or base[0] != "":
        # The text after `^` must start with `/`.
        return None
    head = base[1:]
    return [(head, end)] + [(head + branch, end) for branch in branches]


def _read_segments(regex: str, i: int, stop: str) -> tuple[list | None, int]:
    """Segment patterns from `regex[i:]` up to a character in `stop` or the end.

    A `/` (or `\\/`) starts a new segment, so text starting with `/` yields an empty first one.
    `\\d+` and `[^/]+` must fill their segment. Any other metacharacter gives None.
    """
    segments: list = []
    literal: list[str] = []
    pattern: object = None
    while i < len(regex) and regex[i] not in stop:
        if regex.startswith("/", i) or regex.startswith("\\/", i):
            segments.append(pattern if pattern is not None else "".join(literal))
            literal, pattern = [], None
            i += 1 if regex[i] == "/" else 2
            continue
        atom = next((a for a in ("\\d+", "[^/]+") if regex.startswith(a, i)), None)
        if atom is not None:
            after = i + len(atom)
            following = regex[after : after + 1]
            fills = following in ("", "/", *stop) or regex.startswith("\\/", after)
            if literal or pattern is not None or not fills:
                return None, i
            pattern = _DIGITS if atom == "\\d+" else _ANY
            i = after
            continue
        c = regex[i]
        if pattern is not None or c in _REGEX_META and c != "\\":
            return None, i
        if c == "\\":
            if i + 1 >= len(regex) or regex[i + 1] not in _LITERAL_ESCAPES:
                return None, i
            literal.append(regex[i + 1])
            i += 2
            continue
        literal.append(c)
        i += 1
    segments.append(pattern if pattern is not None else "".join(literal))
    return segments, i


def _linear_verdict(segments: list, present: int, rule: tuple[list, bool]) -> str:
    """One alternative against one shape: a conjunction over independent segments.

    Each URL segment comes from its own placeholder or literal, so the URL set is a product:
    one never-true conjunct makes the whole DISJOINT, all always-true make it MATCH, and
    anything else has URLs on both sides.
    """
    patterns, end = rule
    last = len(patterns) - 1
    count = len(segments)
    verdicts: list[str] = []
    for index, pattern in enumerate(patterns):
        if index >= count:
            # Every URL ends before this rule segment.
            return DISJOINT
        if index >= present:
            # Some URLs end before this rule segment.
            verdicts.append(UNDECIDED)
            continue
        mode = _FULL if index < last else (_FULL_END if end else _PREFIX)
        verdicts.append(_segment(segments[index], pattern, mode))
    if end:
        if present > len(patterns):
            # Every URL has a `/` after the last rule segment, which `$` cannot pass.
            return DISJOINT
        if count > len(patterns):
            verdicts.append(UNDECIDED)
    if DISJOINT in verdicts:
        return DISJOINT
    return MATCH if all(v == MATCH for v in verdicts) else UNDECIDED


def _segment(value: object, pattern: object, mode: str) -> str:
    """Whether every, no, or some values of one URL segment match one rule segment in `mode`.

    `_FULL_END` also accepts the value plus a final newline (`$` without the `D` modifier).
    Only `_ANY` values can hold a newline, and against `_ANY` those values match anyway.
    """
    if value is _UNKNOWN:
        return UNDECIDED
    if isinstance(value, str):
        return MATCH if _literal_matches(value, pattern, mode) else DISJOINT
    if pattern is _ANY:
        return MATCH
    lengths = _digit_lengths(value)
    if lengths is not None:
        if pattern is _DIGITS:
            return MATCH
        # A literal rule segment.
        if mode == _PREFIX and pattern == "":
            return MATCH
        if isinstance(pattern, str) and pattern and set(pattern) <= _DIGIT_CHARS:
            low, high = lengths
            if high is not None and len(pattern) > high:
                # Longer than every value, so neither equal to one nor a prefix of one.
                return DISJOINT
            if mode != _PREFIX and len(pattern) < low:
                return DISJOINT
            return UNDECIDED
        return DISJOINT
    # `_ANY` values against `\d+` or a literal.
    if mode == _PREFIX and pattern == "":
        return MATCH
    if mode == _FULL and pattern == "":
        # Every value is non-empty.
        return DISJOINT
    return UNDECIDED


def _digit_lengths(value: object) -> tuple[int, int | None] | None:
    """`(min, max)` value length of a digits-only value set, max None for `\\d+`; else None."""
    if value is _DIGITS:
        return 1, None
    if isinstance(value, tuple) and len(value) == 3 and value[0] == "digits":
        return value[1], value[2]
    return None


def _literal_matches(text: str, pattern: object, mode: str) -> bool:
    if pattern is _ANY:
        return text != ""
    if pattern is _DIGITS:
        if mode == _PREFIX:
            return text[:1] in _DIGIT_CHARS and text != ""
        return text != "" and set(text) <= _DIGIT_CHARS
    if not isinstance(pattern, str):
        return False
    if mode == _PREFIX:
        return text.startswith(pattern)
    return text == pattern


def load_rules(repo_root: str) -> tuple[list[AccessRule] | None, str]:
    """The top-level `security.access_control` rules, or None and a fixed reason.

    A missing file is (None, ""): no rules and nothing to report. pyyaml absent is a reason,
    never an exception.
    """
    path = Path(repo_root) / SECURITY_YAML
    if not path.is_file():
        return None, ""
    try:
        from ..config.safe_yaml import SafeDocumentParser
    except ImportError as exc:
        if getattr(exc, "name", None) != "yaml":
            raise
        return None, REASON_PYYAML_MISSING
    parsed = SafeDocumentParser().parse(path)
    if parsed.data is None:
        return None, REASON_UNPARSED
    security = parsed.data.get("security")
    if not isinstance(security, dict):
        return [], ""
    entries = security.get("access_control")
    if entries is None:
        return [], ""
    if not isinstance(entries, list):
        return None, REASON_NOT_A_LIST
    return [AccessRule.from_config(i, entry) for i, entry in enumerate(entries)], ""


def config_blocker(repo_root: str) -> str:
    """A fixed reason when no `access_control` edge may be published for this build, else "".

    Two things this module cannot read would make an edge wrong. A routes-config import with
    a `prefix` makes the served URL longer than the attribute-declared `route_path`
    (`REASON_ROUTE_PREFIX_UNREAD`). An `access_control` list declared anywhere but the
    top level of `security.yaml` may replace the one read here (`REASON_OVERRIDDEN_ELSEWHERE`).
    A file that cannot be read or parsed counts as a hit: absence is not proved.
    """
    try:
        from ..config.safe_yaml import SafeDocumentParser
    except ImportError as exc:
        if getattr(exc, "name", None) != "yaml":
            raise
        return REASON_PYYAML_MISSING
    root = Path(repo_root)
    parser = SafeDocumentParser()
    if _route_prefix_possible(root, parser):
        return REASON_ROUTE_PREFIX_UNREAD
    if _access_control_elsewhere(root, parser):
        return REASON_OVERRIDDEN_ELSEWHERE
    return ""


def _config_files(
    root: Path, top: str | None, directory: str, suffixes: tuple[str, ...]
) -> list[Path]:
    """`<top>.<suffix>` and every `<directory>/**/*.<suffix>` that is a file, in a fixed order."""
    found = [root / f"{top}{suffix}" for suffix in suffixes] if top else []
    folder = root / directory
    if folder.is_dir():
        found.extend(sorted(p for p in folder.rglob("*") if p.suffix in suffixes))
    return [p for p in found if p.is_file()]


def _text(path: Path) -> str | None:
    """The file's text, or None when it cannot be read or is larger than any config file."""
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _blank(text: str) -> bool:
    """True for a YAML file with nothing but blank lines and comments."""
    return all(not line.strip() or line.lstrip().startswith("#") for line in text.splitlines())


def _mappings(value) -> list[dict]:
    """Every mapping inside a parsed document, the document itself and `when@*` blocks included."""
    found: list[dict] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _prefixed_import(mapping: dict) -> bool:
    """True when an import carries a `prefix` that may reach attribute-declared routes.

    `type: attribute|annotation` imports controllers directly. A resource that is not a
    bundle path (`@...`) is a project directory, a project PHP file, or a project routing
    file whose own imports inherit the prefix, so each of those is treated alike.
    """
    if "resource" not in mapping or "prefix" not in mapping:
        return False
    if mapping.get("type") in ("attribute", "annotation"):
        return True
    resource = mapping["resource"]
    if not isinstance(resource, str):
        return True
    return not resource.startswith("@")


def _route_prefix_possible(root: Path, parser) -> bool:
    for path in _config_files(root, "config/routes", ROUTES_DIR, (".yaml", ".yml")):
        text = _text(path)
        if text is None:
            return True
        if _blank(text):
            continue
        parsed = parser.parse(path)
        if parsed.data is None:
            return True
        if any(_prefixed_import(m) for m in _mappings(parsed.data)):
            return True
    # PHP and XML are not parsed: any prefix call or attribute in the text is a hit.
    text_files = _config_files(root, "config/routes", ROUTES_DIR, (".php", ".xml"))
    kernel = root / KERNEL_PHP
    if kernel.is_file():
        text_files.append(kernel)
    for path in text_files:
        text = _text(path)
        if text is None or any(marker in text for marker in _PREFIX_MARKERS):
            return True
    return False


# Import options that change a route's placeholders: `RouteCollection::addRequirements` and
# `addDefaults` override the attribute's own, and `locale`/`format` set `_locale`/`_format`.
_IMPORT_PLACEHOLDER_KEYS = ("requirements", "defaults", "locale", "format")
_IMPORT_PLACEHOLDER_MARKERS = (
    "requirements(",
    "defaults(",
    "->locale(",
    "->format(",
    "<requirement",
    "<default",
)


def segment_facts_blocked(repo_root: str) -> bool:
    """True when a routes-config import may change the placeholders the attributes declare.

    `segment_verdict` reads each placeholder's value set from the declaration, so an import
    carrying `requirements`, `defaults`, `locale` or `format` makes those facts unproved.
    Routing YAML is parsed; routing PHP and XML and `src/Kernel.php` get a text check. A file
    that cannot be read or parsed counts as a hit.
    """
    try:
        from ..config.safe_yaml import SafeDocumentParser
    except ImportError as exc:
        if getattr(exc, "name", None) != "yaml":
            raise
        return True
    root = Path(repo_root)
    parser = SafeDocumentParser()
    for path in _config_files(root, "config/routes", ROUTES_DIR, (".yaml", ".yml")):
        text = _text(path)
        if text is None:
            return True
        if _blank(text):
            continue
        parsed = parser.parse(path)
        if parsed.data is None:
            return True
        for mapping in _mappings(parsed.data):
            if "resource" in mapping and any(k in mapping for k in _IMPORT_PLACEHOLDER_KEYS):
                return True
    text_files = _config_files(root, "config/routes", ROUTES_DIR, (".php", ".xml"))
    kernel = root / KERNEL_PHP
    if kernel.is_file():
        text_files.append(kernel)
    for path in text_files:
        text = _text(path)
        if text is None or any(marker in text for marker in _IMPORT_PLACEHOLDER_MARKERS):
            return True
    return False


def _declares_access_control(security) -> bool:
    return isinstance(security, dict) and "access_control" in security


def _access_control_elsewhere(root: Path, parser) -> bool:
    main = root / SECURITY_YAML
    for path in _config_files(root, None, PACKAGES_DIR, (".yaml", ".yml")):
        parsed = parser.parse(path)
        if parsed.data is None:
            text = _text(path)
            if text is None or any(marker in text for marker in _ACCESS_CONTROL_MARKERS):
                return True
            continue
        if path != main and _declares_access_control(parsed.data.get("security")):
            return True
        for key, value in parsed.data.items():
            if (
                isinstance(key, str)
                and key.startswith("when@")
                and isinstance(value, dict)
                and _declares_access_control(value.get("security"))
            ):
                return True
    for path in _config_files(root, None, PACKAGES_DIR, (".php", ".xml")):
        text = _text(path)
        if text is None or any(marker in text for marker in _ACCESS_CONTROL_MARKERS):
            return True
    return False
