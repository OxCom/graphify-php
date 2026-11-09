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

import dataclasses
import os
import re
from collections.abc import Iterable

from ..ports import (
    CallerSites,
    CallSite,
    CallTarget,
    Confidence,
    NewSiteLine,
    NullsafeCall,
    Resolution,
)
from ..syntax import nodes
from ..syntax.call_sites import CALL_KINDS, SCOPED_KIND
from ..syntax.class_index import DECLARED, MAGIC, NOT_DECLARED, ClassIndex, index_from_facts
from ..syntax.collectors import REASSIGNED_LOCAL, UNION_TYPE
from ..syntax.facts import FileFacts
from ..syntax.file_scan import scan_source
from ..syntax.names import NameResolver


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
    ANONYMOUS_CLASS_SCOPE = "anonymous-class-scope"
    # The site names a receiver or scope, and the one call of that method on the line is
    # written with another: it is a different call, not this one.
    RECEIVER_MISMATCH = "receiver-mismatch"
    SCOPE_MISMATCH = "scope-mismatch"
    # Two classes or two methods cover the site's line (a one-line class), and the site's
    # caller does not pick one of them.
    SHARED_LINE_SCOPE = "shared-line-scope"
    # A `?->m()` graphify never sends. Its line holds a call of that name that is not nullsafe,
    # or a dynamic name; its line's nullsafe calls of that name sit in different callables;
    # another callable starts where its caller does; or no callable holds it.
    NULLSAFE_OTHER_CALL = "other-call-on-line"
    NULLSAFE_CALLERS_DIFFER = "callers-differ"
    NULLSAFE_CALLER_LINE_SHARED = "caller-line-shared"
    NULLSAFE_OUTSIDE_CALLABLE = "outside-callable"


# Types whose methods dispatch somewhere else at runtime. Resolving `$container->get('x')` to
# `ContainerInterface::get` is true and useless: the edge that matters goes to whatever the
# container returns, which is not written at the call site. Reflection is the same shape.
DISPATCH_BARRIER_TYPES = frozenset(
    {
        "Psr\\Container\\ContainerInterface",
        "Symfony\\Component\\DependencyInjection\\ContainerInterface",
        "Symfony\\Component\\DependencyInjection\\Container",
        "ReflectionClass",
        "ReflectionMethod",
        "ReflectionProperty",
        "ReflectionFunction",
        "ReflectionObject",
        "Closure",
    }
)

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

    def __init__(self) -> None:
        # Parsed trees for `member_call_lines`, by real path, for this source's one build.
        self._trees: dict[str, object] = {}
        # `new_site_line`: source lines by real path (None when unreadable), and per-line facts
        # by real path (None when the file does not parse cleanly).
        self._lines: dict[str, list[bytes] | None] = {}
        self._line_facts: dict[str, dict[int, _LineFacts] | None] = {}

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
        # Where each class was read, so the graph layer binds a target to that file's class
        # and never to another class sharing its short name. Two files declaring one name
        # leave it unnamed. A class no corpus file declares is in no graph node at all.
        declared: dict[str, set[str]] = {}
        for full, read in cache.items():
            for scope in read.classes if read is not None else ():
                declared.setdefault(_scope_key(scope.fqn), set()).add(full)

        for path, file_sites in by_file.items():
            facts = self._facts(repo_root, path, cache)
            if facts is None:
                _count(resolution, Reason.UNREADABLE_FILE, len(file_sites))
                continue
            resolution.files_seen += 1
            for site in file_sites:
                target, reason = self._resolve_site(site, facts, index)
                if target is not None:
                    homes = declared.get(_scope_key(target.class_fqn), ())
                    if len(homes) == 1:
                        target = dataclasses.replace(target, declaring_file=next(iter(homes)))
                    elif not homes and corpus is not None:
                        target = dataclasses.replace(target, outside_corpus=True)
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

    def _facts(
        self, repo_root: str, path: str, cache: dict[str, FileFacts | None]
    ) -> FileFacts | None:
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

    def _resolve_site(
        self, site: CallSite, facts: FileFacts, index: ClassIndex
    ) -> tuple[CallTarget | None, str]:
        method = (site.method or "").strip()
        if not _IDENTIFIER.match(method):
            return None, Reason.DYNAMIC_NAME
        if method in DISPATCH_BARRIER_METHODS:
            return None, Reason.INDIRECT_DISPATCH

        line = line_of(site.source_location)
        if line is None:
            return None, Reason.UNLOCATABLE_SITE
        klass, reason = _one_covering(facts.classes, line, site, by_class=True)
        if reason:
            return None, reason
        if klass is None:
            return None, Reason.UNLOCATABLE_SITE
        if facts.in_anonymous_class(line):
            # `class_at` finds the enclosing class, but `$this->emails` here is a property of
            # the `new class { ... }` the walker read nothing of, so the enclosing class's
            # `$emails` would be the wrong one.
            return None, Reason.ANONYMOUS_CLASS_SCOPE

        if site.scoped:
            # A scoped call or a probe: a member call of that name on the same line is a
            # different call and must never answer it.
            resolved, reason = self._scoped_target(facts, klass, line, method, site.receiver)
        else:
            resolved, reason = self._receiver_target(site, facts, klass, line, method, index)
        if reason == Reason.SHARED_LINE_SCOPE:
            return None, reason
        # A site that arrived with a receiver name is a member call; only the receiver-less
        # shape of older graphify can be a scoped call in disguise.
        if (
            resolved is None
            and not site.scoped
            and not (site.receiver or "").strip()
            and facts.calls.receiver_for(line, method)[1] == 0
        ):
            # Only a line with no member call of this name can be a scoped call. A caller that
            # does not flag `scoped` sends a scoped site with no receiver at all, which the
            # member path refuses and the scoped path answers. A member call that is there is
            # answered by the member path alone: `Other::foo()` sitting in the argument list
            # of `$x->foo(...)` must not stand in for a receiver that could not be typed.
            resolved, reason = self._scoped_target(facts, klass, line, method)
        if resolved is None:
            return None, reason
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
            scope, reason = _one_covering(klass.methods, line, site, by_class=False)
            if reason:
                return None, reason
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

    def _scoped_target(self, facts, klass, line, method, expected_scope: str = ""):
        """`self::`, `parent::`, `static::` and `Class::`, each on its own terms.

        graphify records the scope text as the callee for these (`engine.py:6147-6152`), so the
        method name is only in the source and the line is the only way back to it. When the
        site carries its scope text, the call found must be written with that same scope.
        """
        scoped, matches = facts.calls.scope_for(line, method)
        if scoped is None:
            return None, Reason.AMBIGUOUS_SITE if matches > 1 else Reason.CALL_SITE_NOT_FOUND
        expected = (expected_scope or "").strip()
        if expected and _scope_key(expected) != _scope_key(scoped.scope):
            return None, Reason.SCOPE_MISMATCH
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

    def _receiver(
        self, site: CallSite, facts: FileFacts, line: int, method: str
    ) -> tuple[str | None, str]:
        """The receiver expression for this site, read from the tree when the site has none.

        graphify's PHP branch never fills the field — `extractors/engine.py:6152-6157` sets
        `is_member_call` and the callee name and stops, so `engine.py:6403` writes
        `"receiver": swift_receiver or member_receiver` as None for every PHP site. A source
        that trusted the field would resolve nothing in production while passing every test
        that supplies one.

        A `site.receiver` that is a PHP expression (`$...`) still wins. Anything else is not
        one: graphify 0.9.103 sends a bare name, `mailer` for both `$this->mailer` and
        `$mailer`, so the line is re-read instead.
        """
        given = (site.receiver or "").strip()
        if given.startswith("$"):
            return given, ""
        found, matches = facts.calls.receiver_for(line, method)
        if found is not None:
            # A bare name is still evidence: `baz` cannot be the call written `$this->bar`.
            named = _receiver_name(found)
            if given and named is not None and named != given:
                return None, Reason.RECEIVER_MISMATCH
            return found, ""
        if matches > 1:
            # `$a->run(); $b->run();` on one line: two receivers, two classes, and the
            # location is not precise enough to say which. Picking either is a coin toss.
            return None, Reason.AMBIGUOUS_SITE
        return None, Reason.CALL_SITE_NOT_FOUND

    def member_call_lines(
        self, repo_root: str, source_file: str, caller_location: str, method: str
    ) -> CallerSites | None:
        """Every `->method(` and `?->method(` in the caller's body, and what else is near them.

        The caller is every function, method or closure starting on `caller_location`'s line.
        Its span includes nested closures, so their calls count too: an answer that lists too
        many sites only ever keeps an edge. A line is foreign when a member call of that name on
        it sits in a body other than the caller's, or when more than one callable starts on the
        caller's line. None when the file cannot be read, does not parse cleanly, or no callable
        starts on that line, which is "cannot tell" and never "no sites".
        """
        start = line_of(caller_location)
        if start is None or not method:
            return None
        root = self._tree(repo_root, source_file)
        if root is None:
            return None
        return _caller_sites(root, start, method.lower())

    def method_declared_at(
        self, repo_root: str, source_file: str, location: str, name: str
    ) -> bool | None:
        """Whether what is declared as `name` on `location`'s line is a class method.

        False when a function of that name is declared there, True when only methods are (an
        anonymous class's method included). None when the file cannot be read, does not parse
        cleanly, or nothing of that name is declared on the line.
        """
        line = line_of(location)
        if line is None or not name:
            return None
        root = self._tree(repo_root, source_file)
        if root is None:
            return None
        kinds: set[str] = set()
        _declarations_at(root, line, name.lower(), kinds)
        if not kinds or _FUNCTION_DECLARATION in kinds:
            return False if kinds else None
        return True

    def _tree(self, repo_root: str, source_file: str):
        """The file's parse tree, read once per build; None when unreadable or not clean."""
        if not nodes.tree_sitter_available()[0]:
            return None
        full = os.path.realpath(
            source_file if os.path.isabs(source_file) else os.path.join(repo_root, source_file)
        )
        trees = self._trees
        if full not in trees:
            try:
                with open(full, "rb") as handle:
                    root = nodes.parse(handle.read()).root_node
                trees[full] = None if root.has_error else root
            except OSError:
                trees[full] = None
        return trees[full]

    def nullsafe_calls(self, repo_root: str, files: Iterable[str]) -> list[NullsafeCall]:
        """Every `$x?->m()` in `files`, each with its innermost caller's start lines or a refusal.

        graphify sends no raw call for these, so nothing else names their caller. A site is
        refused when its line holds a call of that name that is not nullsafe (a member or
        scoped call has a raw site of its own, and a record cannot say which call it is), when
        the line's nullsafe calls of that name sit in different callables, when another
        callable starts on the caller's lines, or when no callable holds it. A file that
        cannot be read contributes nothing; each file is read once whatever its spelling.
        """
        if not nodes.tree_sitter_available()[0]:
            return []
        found: list[NullsafeCall] = []
        seen: set[str] = set()
        for path in files:
            full = os.path.realpath(path if os.path.isabs(path) else os.path.join(repo_root, path))
            if full in seen:
                continue
            seen.add(full)
            try:
                with open(full, "rb") as handle:
                    source = handle.read()
            except OSError:
                continue
            if b"?->" not in source:
                continue
            found.extend(_nullsafe_in(path, nodes.parse(source).root_node))
        return found

    def new_site_line(
        self, repo_root: str, source_file: str, line: int, name: str
    ) -> NewSiteLine | None:
        """Whether `new <name>(` starts on `line`, and whether a call of `name` sits there.

        Read from the parse tree, so a `new` in a comment or a string is no evidence. A line
        whose text holds no `new` or no `name` creates nothing and is answered without parsing.
        None when the file cannot be read, the line is outside it, or the file has a parse
        error: "cannot tell", never "no".
        """
        if line < 1 or not name or not nodes.tree_sitter_available()[0]:
            return None
        full = os.path.realpath(
            source_file if os.path.isabs(source_file) else os.path.join(repo_root, source_file)
        )
        if full not in self._lines:
            try:
                with open(full, "rb") as handle:
                    self._lines[full] = handle.read().split(b"\n")
            except OSError:
                self._lines[full] = None
        lines = self._lines[full]
        if lines is None or line > len(lines):
            return None
        text = lines[line - 1].lower()
        if b"new" not in text or name.lower().encode() not in text:
            return NewSiteLine(creates=False, calls_name=False)
        if full not in self._line_facts:
            root = nodes.parse(b"\n".join(lines)).root_node
            self._line_facts[full] = None if root.has_error else _facts_by_line(root)
        facts = self._line_facts[full]
        if facts is None:
            return None
        found = facts.get(line)
        if found is None:
            return NewSiteLine(creates=False, calls_name=False)
        key = name.lower()
        return NewSiteLine(
            creates=key in found.created,
            calls_name=found.dynamic_call or key in found.called,
        )


@dataclasses.dataclass
class _LineFacts:
    created: set[str] = dataclasses.field(default_factory=set)  # `new X(` last segments
    called: set[str] = dataclasses.field(default_factory=set)  # call names
    dynamic_call: bool = False  # a call whose name is not literal


_NEW_KIND = "object_creation_expression"
_FUNCTION_KIND = "function_call_expression"
_NOT_A_CLASS = frozenset({"self", "static", "parent"})


def _facts_by_line(root) -> dict[int, _LineFacts]:
    """Per line: the classes `new` creates starting there, and the call names written there.

    A creation counts on the line its `new` starts, the line graphify reports for it. A call
    counts on its start line and on its name's line, so a chained `->m(` on a later line is
    seen on both: an extra call only ever keeps an edge.
    """
    facts: dict[int, _LineFacts] = {}

    def at(line: int) -> _LineFacts:
        return facts.setdefault(line, _LineFacts())

    def walk(node) -> None:
        if node.type == _NEW_KIND:
            for child in node.children:
                if child.type in ("name", "qualified_name"):
                    last = nodes.node_text(child).rsplit("\\", 1)[-1].lower()
                    if last not in _NOT_A_CLASS:
                        at(node.start_point[0] + 1).created.add(last)
                    break
        elif node.type in _ANY_CALL_KINDS or node.type == _FUNCTION_KIND:
            field = "function" if node.type == _FUNCTION_KIND else "name"
            name = node.child_by_field_name(field)
            lines = {node.start_point[0] + 1}
            if name is not None:
                lines.add(name.start_point[0] + 1)
            literal = name is not None and name.type in ("name", "qualified_name")
            for line in lines:
                if literal:
                    at(line).called.add(nodes.node_text(name).rsplit("\\", 1)[-1].lower())
                else:
                    at(line).dynamic_call = True
        for child in node.children:
            walk(child)

    walk(root)
    return facts


# Everything graphify makes a caller node for: a method, a function, a closure.
_CALLABLE_KINDS = ("method_declaration", "function_definition", *nodes.CLOSURE_KINDS)


_FUNCTION_DECLARATION = "function_definition"
_MEMBER, _OTHER = "member", "other"


def _call_of(node, method: str) -> str:
    """`_MEMBER` for `->method(`/`?->method(`, `_OTHER` for a scoped or function call of it."""
    if node.type in CALL_KINDS or node.type == SCOPED_KIND:
        name = node.child_by_field_name("name")
        if name is None or name.type != "name" or nodes.node_text(name).lower() != method:
            return ""
        return _MEMBER if node.type in CALL_KINDS else _OTHER
    if node.type == _FUNCTION_KIND:
        name = node.child_by_field_name("function")
        if name is None or name.type not in ("name", "qualified_name"):
            return ""
        return _OTHER if nodes.node_text(name).rsplit("\\", 1)[-1].lower() == method else ""
    return ""


def _caller_sites(root, line: int, method: str) -> CallerSites | None:
    """`CallerSites` for the callables starting (or named) on `line`; None when there is none.

    Lines are the call node's start line, because that is the line graphify and PHPStan report.
    The name's line counts as a start, because an attribute above a method starts its
    declaration.
    """
    callers: list[object] = []
    # Per call of that name: (line, innermost callable or None, _MEMBER or _OTHER).
    calls: list[tuple[int, object, str]] = []

    def walk(node, holder) -> None:
        if node.type in _CALLABLE_KINDS:
            name = node.child_by_field_name("name")
            if node.start_point[0] + 1 == line or (
                name is not None and name.start_point[0] + 1 == line
            ):
                callers.append(node)
            holder = node
        kind = _call_of(node, method)
        if kind:
            calls.append((node.start_point[0] + 1, holder, kind))
        for child in node.children:
            walk(child, holder)

    walk(root, None)
    if not callers:
        return None
    spans = [nodes.line_span(node) for node in callers]
    counts: dict[int, int] = {}
    other_kinds = False
    for at, _holder, kind in calls:
        if not any(first <= at <= last for first, last in spans):
            continue
        if kind == _MEMBER:
            counts[at] = counts.get(at, 0) + 1
        else:
            other_kinds = True
    if len(callers) != 1:
        # Two callables start here and a type engine may name either: no line is the caller's.
        return CallerSites(counts, frozenset(counts), other_kinds)
    own = callers[0]
    # A member call of that name on a site's line whose innermost body is not the caller's: a
    # nested closure, or the method around a closure on the closure's first or last line.
    foreign = frozenset(
        at for at, holder, kind in calls if kind == _MEMBER and at in counts and holder is not own
    )
    return CallerSites(counts, foreign, other_kinds)


def _declarations_at(node, line: int, name: str, kinds: set[str]) -> None:
    """The kinds of every method or function named `name` declared (or named) on `line`."""
    if node.type in ("method_declaration", _FUNCTION_DECLARATION):
        found = node.child_by_field_name("name")
        if (
            found is not None
            and nodes.node_text(found).lower() == name
            and line in (node.start_point[0] + 1, found.start_point[0] + 1)
        ):
            kinds.add(node.type)
    for child in node.children:
        _declarations_at(child, line, name, kinds)


_NULLSAFE_KIND = "nullsafe_member_call_expression"
_ANY_CALL_KINDS = (*CALL_KINDS, SCOPED_KIND)


def _caller_lines(callable_node) -> tuple[int, ...]:
    """The lines a callable starts on: its declaration (attributes included), and its name."""
    lines = [callable_node.start_point[0] + 1]
    name = callable_node.child_by_field_name("name")
    if name is not None and name.start_point[0] + 1 not in lines:
        lines.append(name.start_point[0] + 1)
    return tuple(lines)


def _nullsafe_in(path: str, root) -> list[NullsafeCall]:
    """Nullsafe calls in one parsed file, judged against every call on their lines."""
    # Per call: (line, column, kind, method name or None when dynamic, innermost callable).
    calls: list[tuple[int, int, str, str | None, object]] = []
    starts: dict[int, int] = {}  # line -> how many callables start (or are named) on it

    def walk(node, holder) -> None:
        if node.type in _CALLABLE_KINDS:
            for line in _caller_lines(node):
                starts[line] = starts.get(line, 0) + 1
            holder = node
        if node.type in _ANY_CALL_KINDS:
            name = node.child_by_field_name("name")
            method = nodes.node_text(name) if name is not None and name.type == "name" else None
            calls.append((node.start_point[0] + 1, node.start_point[1], node.type, method, holder))
        for child in node.children:
            walk(child, holder)

    walk(root, None)
    by_line: dict[int, list[tuple[int, int, str, str | None, object]]] = {}
    for call in calls:
        by_line.setdefault(call[0], []).append(call)

    found: list[NullsafeCall] = []
    for line, _column, kind, method, holder in sorted(calls, key=lambda c: (c[0], c[1])):
        if kind != _NULLSAFE_KIND:
            continue
        if method is None:
            found.append(NullsafeCall(path, line, "", (), Reason.DYNAMIC_NAME))
            continue
        # A dynamic name elsewhere on the line may be this name at runtime.
        same = [c for c in by_line[line] if c[3] is None or c[3].lower() == method.lower()]
        reason = ""
        if any(c[2] != _NULLSAFE_KIND or c[3] is None for c in same):
            reason = Reason.NULLSAFE_OTHER_CALL
        elif holder is None:
            reason = Reason.NULLSAFE_OUTSIDE_CALLABLE
        elif any(c[4] is not holder for c in same):
            reason = Reason.NULLSAFE_CALLERS_DIFFER
        elif any(starts.get(n, 0) > 1 for n in _caller_lines(holder)):
            reason = Reason.NULLSAFE_CALLER_LINE_SHARED
        lines = () if reason else _caller_lines(holder)
        found.append(NullsafeCall(path, line, method, lines, reason))
    return found


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


def _receiver_name(receiver: str) -> str | None:
    """`bar` for `$this->bar` and for `$bar`, the bare name graphify 0.9.103 sends; else None."""
    text = receiver.strip()
    match = _THIS_PROPERTY.match(text) or _LOCAL.match(text)
    return match.group(1) if match else None


def _one_covering(scopes, line: int, site: CallSite, *, by_class: bool):
    """The one class or method covering `line`, as (scope, "") or (None, reason).

    Named classes and methods never nest, so two covering one line share it, and the line
    alone cannot say which holds the call. The site's caller spelling (`Class::method`) can.
    """
    covering = [scope for scope in scopes if scope.contains(line)]
    if len(covering) <= 1:
        return (covering[0] if covering else None), ""
    wanted = set()
    for spelled in site.caller_scope or ():
        cls, _, name = spelled.rpartition("::")
        wanted.add((cls if by_class else name).lower())
    wanted.discard("")
    named = [
        scope
        for scope in covering
        if (scope.fqn.rsplit("\\", 1)[-1] if by_class else scope.name).lower() in wanted
    ]
    return (named[0], "") if len(named) == 1 else (None, Reason.SHARED_LINE_SCOPE)


def _scope_key(scope: str) -> str:
    # PHP class names are case-insensitive, and `\App\Foo` names the same class as `App\Foo`.
    return scope.strip().lstrip("\\").lower()


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
