"""The parser-backed `CallTargetSource`.

Needs nothing but the source text: no `vendor/`, no PHP binary, no composer autoloader. That
is what makes it the source that always runs, and what bounds what it can prove. Measured on
one Symfony application before this package existed: 2172 `calls` edges against 3426 callable
nodes, 24 % of callables with an outgoing call edge and 20 % with an incoming one. Most of the
missing edges are `$this->someInjectedService->method()`, and the type is written in the same
file, in the constructor.

Every site it cannot prove is counted under a fixed reason instead of guessed at. Six upstream
issues across four code-graph projects trace their false edges to a name-only fallback — one
of them after `empty()` was wired to a method named `empty` and `->get()` to an unrelated
class. A false edge is worse than a missing one, because a missing edge looks missing.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from ..ports import CallSite, CallTarget, Confidence, Resolution
from ..syntax import nodes
from ..syntax.class_index import DECLARED, MAGIC, NOT_DECLARED, ClassIndex, index_from_facts
from ..syntax.collectors import REASSIGNED_LOCAL, UNION_TYPE
from ..syntax.facts import FileFacts
from ..syntax.names import NameResolver
from ..syntax.file_scan import scan_source


class Reason:
    """The fixed vocabulary of refusals.

    Constants rather than literals at the call sites: these counts are the work list for the
    next version of this package, and a histogram of near-duplicate free-text strings is not a
    work list.
    """

    DYNAMIC_NAME = "dynamic-name"
    CHAINED_CALL = "chained-call"
    NESTED_PROPERTY = "nested-property-access"
    STATIC_BINDING = "static-binding"
    INDIRECT_DISPATCH = "indirect-dispatch"
    REASSIGNED_LOCAL = REASSIGNED_LOCAL
    UNION_TYPE = UNION_TYPE
    UNSUPPORTED_RECEIVER = "unsupported-receiver"
    UNKNOWN_RECEIVER_TYPE = "unknown-receiver-type"
    UNLOCATABLE_SITE = "unlocatable-call-site"
    UNREADABLE_FILE = "unreadable-file"
    AMBIGUOUS_SITE = "ambiguous-call-site"
    CALL_SITE_NOT_FOUND = "call-site-not-found"
    METHOD_NOT_DECLARED = "method-not-declared"
    AMBIGUOUS_INTERSECTION = "ambiguous-intersection"
    DYNAMIC_SCOPE = "dynamic-scope"
    UNRESOLVABLE_SCOPE = "unresolvable-scope"


# Types whose methods dispatch somewhere else at runtime. Resolving `$container->get('x')` to
# `ContainerInterface::get` is true and useless: the edge that matters goes to whatever the
# container returns, which is not written at the call site. Reflection is the same shape.
DISPATCH_BARRIER_TYPES = frozenset({
    "Psr\\Container\\ContainerInterface",
    "Symfony\\Component\\DependencyInjection\\ContainerInterface",
    "Symfony\\Component\\DependencyInjection\\Container",
    "ReflectionClass",
    "ReflectionMethod",
    "ReflectionProperty",
    "ReflectionFunction",
    "ReflectionObject",
    "Closure",
})

# Methods that are a dispatch mechanism rather than a destination.
DISPATCH_BARRIER_METHODS = frozenset({"__call", "__callStatic", "__invoke", "__get", "__set"})

_IDENTIFIER = re.compile(r"^[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*$")
_THIS_PROPERTY = re.compile(r"^\$this\s*->\s*([A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)$")
_LOCAL = re.compile(r"^\$([A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)$")
# `L6` is graphify's own shape (engine.py:6402); the bare number covers the rest.
_LEADING_LINE = re.compile(r"\s*L?(\d+)")


class TreeSitterSource:
    """Resolves `$this->prop->method()` and `$local->method()` from the file's own text."""

    name = "tree_sitter"

    def available(self, repo_root: str) -> tuple[bool, str]:
        """True wherever the parser imports. No `vendor/`, so no repository can be too bare."""
        return nodes.tree_sitter_available()

    def resolve(
        self,
        repo_root: str,
        sites: Iterable[CallSite],
        corpus: Iterable[str] | None = None,
    ) -> Resolution:
        resolution = Resolution(targets=[], unresolved={})
        # One cache for the whole call, keyed by real path, so a file that is both a corpus
        # entry and a site file is parsed once.
        cache: dict[str, FileFacts | None] = {}
        # Built before any site is answered, because "this class declares no such method" is
        # only honest once the files that could have declared it have been read. The same
        # cache serves the sites below, so each file is parsed once.
        by_file: dict[str, list[CallSite]] = {}
        for site in sites:
            by_file.setdefault(site.source_file, []).append(site)
        index = self._corpus_index(repo_root, list(by_file), corpus, cache)

        for path, file_sites in by_file.items():
            facts = self._facts(repo_root, path, cache)
            if facts is None:
                _count(resolution, Reason.UNREADABLE_FILE, len(file_sites))
                continue
            resolution.files_seen += 1
            for site in file_sites:
                target, reason = self._resolve_site(site, facts, index)
                if target is not None:
                    resolution.targets.append(target)
                else:
                    _count(resolution, reason, 1)
        return resolution

    def _corpus_index(
        self,
        repo_root: str,
        site_paths,
        corpus: Iterable[str] | None,
        cache: dict[str, FileFacts | None],
    ) -> ClassIndex:
        """Declarations from every file this run may read, which is what it can disprove with.

        `corpus` is what the caller parsed for the build; without it only the files carrying
        call sites are read. The difference is not cosmetic: a leaf service class holds no
        unresolved call of its own, so it never appears among the sites, and without a corpus
        whether its methods are known depends on whether some unrelated file happens to have a
        site in it. The site files are always included, because a site file is readable by
        definition and a caller may pass a corpus that does not list it.

        Never a filesystem walk. The set is bounded by what the build already parsed, so this
        reads nothing the build did not.
        """
        index = ClassIndex()
        paths = list(site_paths)
        if corpus is not None:
            paths.extend(corpus)
        for path in paths:
            facts = self._facts(repo_root, path, cache)
            if facts is not None:
                index_from_facts(index, facts)
        return index

    def _facts(self, repo_root: str, path: str, cache: dict[str, FileFacts | None]) -> FileFacts | None:
        full = path if os.path.isabs(path) else os.path.join(repo_root, path)
        full = os.path.realpath(full)
        if full in cache:
            return cache[full]
        try:
            with open(full, "rb") as handle:
                source = handle.read()
        except OSError:
            cache[full] = None
            return None
        facts = scan_source(path, source)
        cache[full] = facts
        return facts

    def _resolve_site(self, site: CallSite, facts: FileFacts, index: ClassIndex) -> tuple[CallTarget | None, str]:
        method = (site.method or "").strip()
        if not _IDENTIFIER.match(method):
            return None, Reason.DYNAMIC_NAME
        if method in DISPATCH_BARRIER_METHODS:
            return None, Reason.INDIRECT_DISPATCH

        line = line_of(site.source_location)
        if line is None:
            return None, Reason.UNLOCATABLE_SITE
        klass = facts.class_at(line)
        if klass is None:
            return None, Reason.UNLOCATABLE_SITE

        resolved, reason = self._receiver_target(site, facts, klass, line, method, index)
        if resolved is None:
            resolved, scoped_reason = self._scoped_target(facts, klass, line, method)
            if resolved is None:
                # The member-call reason is the more informative of the two whenever the site
                # was a member call at all; the scope reason only applies when it was not.
                return None, reason if reason != Reason.CALL_SITE_NOT_FOUND else scoped_reason
        class_fqn, confidence, via = resolved

        if class_fqn in DISPATCH_BARRIER_TYPES:
            return None, Reason.INDIRECT_DISPATCH

        # The receiver's type is only half a target. Emitting the call site's method name
        # without checking the class declares it is how a resolver invents a member.
        #
        # Only a class this package actually parsed can be disproved. When the class was never
        # read, the target is emitted: `GraphNodeIndex.method_node` looks the method up across
        # the whole corpus and adds no edge when it finds none, so an unprovable target dies
        # there anyway. Refusing here would drop real edges to buy nothing.
        verdict = index.declares(class_fqn, method)
        if verdict == MAGIC:
            return None, Reason.INDIRECT_DISPATCH
        if verdict == NOT_DECLARED:
            return None, Reason.METHOD_NOT_DECLARED

        # First-class callable syntax, `$this->emails->reminderSubmitWeek(...)`, produces a
        # Closure rather than invoking anything, so this edge is arguably a reference and not a
        # call. It is emitted as a call deliberately: the question the graph is built to answer
        # is "what breaks if I change this method", and for that the edge is worth more than
        # its absence. The grammar makes no distinction here, so nothing downstream can tell
        # the two apart — which is the cost of the choice, recorded rather than hidden.
        return CallTarget(
            site=site,
            class_fqn=class_fqn,
            method=method,
            confidence=confidence,
            via=f"{TreeSitterSource.name}:{via}",
        ), ""

    def _receiver_target(self, site, facts, klass, line, method, index):
        """`$this->prop->m()` and `$local->m()`, as (class, confidence, via) or (None, reason)."""
        receiver, reason = self._receiver(site, facts, line, method)
        if receiver is None:
            return None, reason
        kind, key, reason = classify_receiver(receiver)
        if kind is None:
            return None, reason

        if kind == "self":
            # `$this->m()` is provable when `m` is declared anywhere in an ancestry this run
            # actually read — the class itself, a trait it uses, a parent in a file with call
            # sites. When the owner is in an unread file the answer is not known, and naming
            # this class anyway would put the wrong class on a real edge.
            verdict = index.declares(klass.fqn, method)
            if verdict == DECLARED:
                return (klass.fqn, Confidence.EXTRACTED, "self-receiver"), ""
            if verdict == MAGIC:
                return None, Reason.INDIRECT_DISPATCH
            return None, Reason.UNSUPPORTED_RECEIVER

        if kind == "property":
            fact, refusal = index.property_fact(klass.fqn, key)
            if refusal:
                return None, refusal
        else:
            scope = klass.method_at(line)
            if scope is None:
                return None, Reason.UNLOCATABLE_SITE
            if key in scope.refusals:
                return None, scope.refusals[key]
            fact = scope.locals.get(key)

        if fact is None:
            # The type is not in this file. Inherited from a parent class in another file, set
            # by a container, or simply untyped — and any of those could be a class with this
            # short name anywhere in the corpus, which is precisely the match not to make.
            return None, Reason.UNKNOWN_RECEIVER_TYPE
        if fact.is_intersection:
            return self._intersection_target(fact, method, index)
        return (fact.fqn, fact.confidence, fact.via), ""

    def _intersection_target(self, fact, method: str, index: ClassIndex):
        """`A&B`: one object that is both, so the constituent declaring the method is the target.

        This is why an intersection is not a union. With `A|B` the object is one of two things
        and there are two candidate targets, so there is nothing to choose between. With `A&B`
        there is one object, and a method declared in only one constituent names it without
        ambiguity.
        """
        verdicts = {member: index.declares(member, method) for member in fact.members}
        declaring = [m for m, v in verdicts.items() if v == DECLARED]
        if len(declaring) == 1:
            return (declaring[0], Confidence.EXTRACTED, f"{fact.via}+intersection"), ""
        if len(declaring) > 1:
            # One call, one object, two declarations. Which body runs depends on the concrete
            # class, which the type does not name, so the graph cannot say.
            return None, Reason.AMBIGUOUS_INTERSECTION
        if any(v == MAGIC for v in verdicts.values()):
            return None, Reason.INDIRECT_DISPATCH
        if all(v == NOT_DECLARED for v in verdicts.values()):
            return None, Reason.METHOD_NOT_DECLARED
        # A constituent sits in a file this run never read, so the method may well be there.
        return None, Reason.UNKNOWN_RECEIVER_TYPE

    def _scoped_target(self, facts, klass, line, method):
        """`self::`, `parent::`, `static::` and `Class::`, each on its own terms.

        graphify records the scope text as the callee for these (`engine.py:6147-6152`), so the
        method name is only in the source and the line is the only way back to it.
        """
        scoped, matches = facts.calls.scope_for(line, method)
        if scoped is None:
            return None, Reason.AMBIGUOUS_SITE if matches > 1 else Reason.CALL_SITE_NOT_FOUND
        if scoped.is_dynamic:
            # `$class::m()`: the class is a runtime value, and no name is written to resolve.
            return None, Reason.DYNAMIC_SCOPE

        written = scoped.scope
        if written == "self":
            return (klass.fqn, Confidence.EXTRACTED, "self-scope"), ""
        if written == "parent":
            if not klass.parent:
                return None, Reason.UNRESOLVABLE_SCOPE
            # The parent's body is in another file, so whether it declares the method is not a
            # question this file can answer; the class identity is what is proved here.
            return (klass.parent, Confidence.EXTRACTED, "parent-scope"), ""
        if written == "static":
            # Late static binding names the lexical class, but the runtime class may be a
            # subclass that overrides the method, so this edge points at a declaration that
            # may not be the one that executes. INFERRED says so. Subclasses are deliberately
            # not enumerated: that is a closed-world assumption nothing here supports, and it
            # turns one honest edge into one per subclass, all but one of them wrong.
            return (klass.fqn, Confidence.INFERRED, "static-binding"), ""

        resolver = NameResolver(namespace=facts.namespace, imports=facts.imports)
        target = resolver.resolve(written)
        if not target:
            return None, Reason.UNRESOLVABLE_SCOPE
        return (target, Confidence.EXTRACTED, "class-scope"), ""


    def _receiver(self, site: CallSite, facts: FileFacts, line: int, method: str) -> tuple[str | None, str]:
        """The receiver expression for this site, read from the tree when the site has none.

        graphify's PHP branch never fills the field — `extractors/engine.py:6152-6157` sets
        `is_member_call` and the callee name and stops, so `engine.py:6403` writes
        `"receiver": swift_receiver or member_receiver` as None for every PHP site. A source
        that trusted the field would resolve nothing in production while passing every test
        that supplies one.

        A non-empty `site.receiver` still wins, so a later graphify that does fill it is used
        rather than second-guessed.
        """
        given = (site.receiver or "").strip()
        if given:
            return given, ""
        found, matches = facts.calls.receiver_for(line, method)
        if found is not None:
            return found, ""
        if matches > 1:
            # `$a->run(); $b->run();` on one line: two receivers, two classes, and the
            # location is not precise enough to say which. Picking either is a coin toss.
            return None, Reason.AMBIGUOUS_SITE
        return None, Reason.CALL_SITE_NOT_FOUND


def classify_receiver(receiver: str) -> tuple[str | None, str, str]:
    """Read a receiver expression as ("property"|"local", name, "") or (None, "", reason).

    Ordered so that the refusals are decided before any shape is accepted. A receiver that
    almost matches `$this->prop` is not a `$this->prop`, and treating it as one is how an
    unrelated class ends up on the other end of an edge.
    """
    text = (receiver or "").strip()
    if not text:
        return None, "", Reason.UNSUPPORTED_RECEIVER
    if "(" in text:
        # `$this->a->b()` as a receiver means the target is the return type of `b()`, which
        # this version does not track.
        return None, "", Reason.CHAINED_CALL
    if text == "static" or text.startswith("static::"):
        # Late static binding names the declaration, not the runtime class.
        return None, "", Reason.STATIC_BINDING
    if "::" in text:
        return None, "", Reason.UNSUPPORTED_RECEIVER
    if "->" in text:
        head, _, rest = text.partition("->")
        if head.strip() != "$this":
            return None, "", Reason.NESTED_PROPERTY
        if "->" in rest:
            return None, "", Reason.NESTED_PROPERTY
        if rest.strip().startswith("$") or rest.strip().startswith("{"):
            return None, "", Reason.DYNAMIC_NAME
        match = _THIS_PROPERTY.match(text)
        if match is None:
            return None, "", Reason.UNSUPPORTED_RECEIVER
        return "property", match.group(1), ""
    if text.startswith("$$"):
        return None, "", Reason.DYNAMIC_NAME
    match = _LOCAL.match(text)
    if match is None:
        return None, "", Reason.UNSUPPORTED_RECEIVER
    if match.group(1) == "this":
        return "self", "", ""
    return "local", match.group(1), ""


def line_of(location: str) -> int | None:
    """The 1-based line from whatever graphify put in `source_location`.

    graphify writes `L<line>` — `extractors/engine.py:6402` builds it as
    `f"L{node.start_point[0] + 1}"` — so the `L` prefix is the shape that actually arrives and
    is stripped here. The others (`12`, `12:4`, `path.php:12`, `12-30`) are tolerated because
    the field is a plain string in `ports` and this package does not own its format.

    The first number-leading segment wins, so `12:4` yields the line rather than the column,
    and path segments are skipped rather than mined for digits.
    """
    if not location:
        return None
    for part in location.split(":"):
        if "/" in part or "." in part:
            continue
        match = _LEADING_LINE.match(part)
        if match:
            return int(match.group(1))
    return None


def _count(resolution: Resolution, reason: str, count: int) -> None:
    resolution.unresolved[reason] = resolution.unresolved.get(reason, 0) + count
