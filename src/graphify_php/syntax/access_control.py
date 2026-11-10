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


def first_rule(template: str, rules: Sequence[AccessRule]) -> tuple[AccessRule | None, str]:
    """The rule Symfony applies to every URL of `template`, or None and a fixed reason."""
    for rule in rules:
        verdict = rule_verdict(template, rule)
        if verdict == DISJOINT:
            continue
        if verdict == MATCH:
            return rule, ""
        return None, REASON_UNDECIDED
    return None, REASON_NO_RULE


def rule_verdict(template: str, rule: AccessRule) -> str:
    """MATCH, DISJOINT or UNDECIDED for every URL of `template` against one rule."""
    if not isinstance(rule.path, str):
        # No path means every path; a non-string one is not something to evaluate.
        return UNDECIDED
    verdict = path_verdict(template, rule.path)
    if rule.other_matchers and verdict != DISJOINT:
        return UNDECIDED
    return verdict


def path_verdict(template: str, regex: str) -> str:
    """MATCH, DISJOINT or UNDECIDED for every URL of `template` against one path regex."""
    # `Route::setPath()`: `'/'.ltrim(trim($pattern), '/')`.
    url = "/" + template.strip().lstrip("/")
    if "%" in regex or "%" in url or not _portable(regex):
        # `%` is a container parameter in the rule, or a percent-escape the request decodes.
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
