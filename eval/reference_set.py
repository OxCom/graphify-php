"""Call sites labelled from source, with the targets a correct resolver must produce.

Why a labelled set rather than a before/after edge count. The measurement this package exists
to move is 2172 `calls` edges against 3426 callable nodes on the reference application — 24 %
of callables with an outgoing call edge. An edge count answers "more or fewer", never "right
or wrong", and the two come apart: the upstream project that reported the closest fix recorded
+1,566 correct edges and -454 false ones in the same change, so half its value was in edges it
stopped emitting. A denominator produced by the tool under test cannot see that half. Every
label below was read off PHP source by hand, and none of it came from a resolver's output.

The labels are deliberately stratified, because a set that is 90 % promoted constructor
properties measures one pattern and reports it as a package-wide number. Promoted properties
really are the bulk of the population — 652 of 715 injected properties on the reference
application carry a concrete class type — which is exactly why the rarer strata need their own
denominators rather than a share of one pooled figure.

Two flags carry meanings that are easy to confuse:

- `accepted` — the target sets a correct answer may have. More than one set appears where the
  graph has two defensible shapes (a trait method as the using class's or as the trait's), and
  listing both keeps the reference set from encoding one resolver's node conventions.
- `in_scope` — whether *this package version* claims to resolve the site, taken from the
  refusals `php_types` documents. A site can have a target that is provable from source and
  still be out of scope; chained calls are. Recall is reported against in-scope sites only,
  and out-of-scope sites still count in operational coverage, so the gap stays visible.

`REFUSE` sites carry the single accepted answer "no target at all". They are not filler. Six
upstream issues across four code-graph projects trace their false edges to a name-only
fallback, one of them after `empty()` was wired to a method named `empty` — so the set holds
both the synthetic version of that trap and a real `empty($user)` from the reference
application.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

# Read-only. Labels that cite it stay in the set when it is absent; the integrity check skips
# them rather than failing, so the harness runs on a machine that does not have the app.
REFERENCE_APP_ROOT = Path("/var/www/www.cem.dev.lo")


class Stratum(str, Enum):
    """The source pattern the site exercises, not the verdict on it."""

    PROMOTED_CTOR_PROPERTY = "promoted_ctor_property"
    DECLARED_PROPERTY = "declared_property"
    CTOR_BODY_ASSIGNMENT = "ctor_body_assignment"
    LOCAL_FROM_NEW = "local_from_new"
    SELF_RECEIVER = "self_receiver"
    INHERITANCE = "inheritance"
    PARENT_CALL = "parent_call"
    TRAIT = "trait"
    CHAINED = "chained"
    INTERFACE_RECEIVER = "interface_receiver"
    NULLABLE_TYPE = "nullable_type"
    UNION_TYPE = "union_type"
    SAME_NAME_TRAP = "same_name_trap"
    DYNAMIC_NAME = "dynamic_name"
    MAGIC_CALL = "magic_call"
    REFLECTION = "reflection"
    SERVICE_LOCATOR = "service_locator"


class Expectation(str, Enum):
    RESOLVE = "resolve"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Target:
    class_fqn: str
    method: str

    def __str__(self) -> str:  # appears in scorer output, so keep it readable
        return f"{self.class_fqn}::{self.method}"


def _t(class_fqn: str, method: str) -> Target:
    return Target(class_fqn, method)


def _set(*targets: Target) -> frozenset[Target]:
    return frozenset(targets)


@dataclass(frozen=True)
class LabelledSite:
    """One call site and the answer a correct resolver gives for it."""

    site_id: str
    stratum: Stratum
    expectation: Expectation
    in_scope: bool
    path: str                       # relative to FIXTURE_ROOT, or absolute for a real site
    line: int
    receiver: str                   # source text; empty for a language construct like empty()
    method: str
    caller: str                     # enclosing callable, as Class::method
    accepted: tuple[frozenset[Target], ...]
    why: str
    trap: bool = False              # a name-only resolver produces an edge here and is wrong

    @property
    def synthetic(self) -> bool:
        return not Path(self.path).is_absolute()

    @property
    def canonical(self) -> frozenset[Target]:
        return self.accepted[0]

    @property
    def all_accepted_targets(self) -> frozenset[Target]:
        return frozenset().union(*self.accepted)

    @property
    def multi_target(self) -> bool:
        """More than one legitimate target in some accepted answer.

        Kept as a property rather than a hand-set flag so it cannot drift away from `accepted`.
        """
        return any(len(option) > 1 for option in self.accepted)

    def resolve_path(self) -> Path:
        path = Path(self.path)
        return path if path.is_absolute() else FIXTURE_ROOT / path


_EMAILS = "Eval\\Mail\\Emails"
_CACHE = "Eval\\Traps\\Cache"
_NO_TARGET: tuple[frozenset[Target], ...] = (frozenset(),)


SITES: tuple[LabelledSite, ...] = (
    # --- promoted constructor properties -------------------------------------------------
    LabelledSite(
        site_id="promoted.php:15:$this->emails->reminderSubmitWeek",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="promoted.php",
        line=15,
        receiver="$this->emails",
        method="reminderSubmitWeek",
        caller="Eval\\Promoted\\Reminder::weekly",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="`private readonly Emails $emails` promoted in the constructor, `use Eval\\Mail\\Emails`.",
    ),
    LabelledSite(
        site_id="promoted.php:20:$this->emails->reminderSubmitMonth",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="promoted.php",
        line=20,
        receiver="$this->emails",
        method="reminderSubmitMonth",
        caller="Eval\\Promoted\\Reminder::monthly",
        accepted=(_set(_t(_EMAILS, "reminderSubmitMonth")),),
        why="Same property, second method: two sites that collapse into one caller-callee pair.",
    ),
    LabelledSite(
        site_id="secrets.php:37:$this->emails->reminderSubmitWeek",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="secrets.php",
        line=37,
        receiver="$this->emails",
        method="reminderSubmitWeek",
        caller="Eval\\Secrets\\Mailer::notify",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="A resolvable site in the canary fixture, so that file is indexed like any other.",
    ),
    # --- declared typed properties -------------------------------------------------------
    LabelledSite(
        site_id="declared_property.php:20:$this->legacy->reminderSubmitWeek",
        stratum=Stratum.DECLARED_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="declared_property.php",
        line=20,
        receiver="$this->legacy",
        method="reminderSubmitWeek",
        caller="Eval\\Declared\\Digest::send",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="`private readonly Emails $legacy;` declared on the class, assigned in the constructor.",
    ),
    LabelledSite(
        site_id="inheritance.php:18:$this->emails->reminderSubmitWeek",
        stratum=Stratum.DECLARED_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="inheritance.php",
        line=18,
        receiver="$this->emails",
        method="reminderSubmitWeek",
        caller="Eval\\Inherit\\BaseNotifier::notify",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="`protected readonly Emails $emails;` used in the class that declares it.",
    ),
    # --- untyped property assigned from a typed constructor parameter --------------------
    LabelledSite(
        site_id="ctor_body_assign.php:25:$this->mailer->reminderSubmitWeek",
        stratum=Stratum.CTOR_BODY_ASSIGNMENT,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="ctor_body_assign.php",
        line=25,
        receiver="$this->mailer",
        method="reminderSubmitWeek",
        caller="Eval\\CtorBody\\Untyped::run",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="Property has no type; `__construct(Emails $mailer)` does, and assigns it directly.",
    ),
    LabelledSite(
        site_id="ctor_body_assign.php:30:$this->unknown->reminderSubmitWeek",
        stratum=Stratum.CTOR_BODY_ASSIGNMENT,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="ctor_body_assign.php",
        line=30,
        receiver="$this->unknown",
        method="reminderSubmitWeek",
        caller="Eval\\CtorBody\\Untyped::opaque",
        accepted=_NO_TARGET,
        why="Same shape as the site above with an untyped parameter: nothing in source names a class.",
        trap=True,
    ),
    # --- locals bound by `new` ------------------------------------------------------------
    LabelledSite(
        site_id="locals_new.php:13:$mailer->reminderSubmitWeek",
        stratum=Stratum.LOCAL_FROM_NEW,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="locals_new.php",
        line=13,
        receiver="$mailer",
        method="reminderSubmitWeek",
        caller="Eval\\Locals\\Factory::direct",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="`$mailer = new Emails();` two lines above, no reassignment in between.",
    ),
    LabelledSite(
        site_id="locals_new.php:20:$mailer->get",
        stratum=Stratum.LOCAL_FROM_NEW,
        expectation=Expectation.RESOLVE,
        in_scope=False,
        path="locals_new.php",
        line=20,
        receiver="$mailer",
        method="get",
        caller="Eval\\Locals\\Factory::reassigned",
        accepted=(_set(_t(_CACHE, "get")),),
        why=(
            "The local is rebound to Cache before the call. The true target is Cache::get and "
            "both Emails and Cache declare get(), so a resolver that keeps the first binding "
            "scores a wrong target here rather than a missing one."
        ),
        trap=True,
    ),
    LabelledSite(
        site_id="locals_new.php:26:$mailer->reminderSubmitWeek",
        stratum=Stratum.LOCAL_FROM_NEW,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="locals_new.php",
        line=26,
        receiver="$mailer",
        method="reminderSubmitWeek",
        caller="Eval\\Locals\\Factory::fromCallable",
        accepted=_NO_TARGET,
        why="`$mailer = $make();` — a callable's return type is not stated, so no class is named.",
    ),
    # --- inheritance and parent:: ---------------------------------------------------------
    LabelledSite(
        site_id="inheritance.php:26:parent::notify",
        stratum=Stratum.PARENT_CALL,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="inheritance.php",
        line=26,
        receiver="parent",
        method="notify",
        caller="Eval\\Inherit\\WeeklyNotifier::notify",
        accepted=(_set(_t("Eval\\Inherit\\BaseNotifier", "notify")),),
        why="`extends BaseNotifier` in the same file; parent:: names the declaration, not the runtime class.",
    ),
    LabelledSite(
        site_id="inheritance.php:27:$this->emails->reminderSubmitMonth",
        stratum=Stratum.INHERITANCE,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="inheritance.php",
        line=27,
        receiver="$this->emails",
        method="reminderSubmitMonth",
        caller="Eval\\Inherit\\WeeklyNotifier::notify",
        accepted=(_set(_t(_EMAILS, "reminderSubmitMonth")),),
        why="The property is declared on the parent; resolving it needs the inheritance walk, not just the class's own table.",
    ),
    # --- traits ----------------------------------------------------------------------------
    LabelledSite(
        site_id="traits.php:13:$this->emails->reminderSubmitWeek",
        stratum=Stratum.TRAIT,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="traits.php",
        line=13,
        receiver="$this->emails",
        method="reminderSubmitWeek",
        caller="Eval\\Traits\\SendsMail::dispatch",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="The trait declares the typed property it uses, so the site is resolvable inside the trait.",
    ),
    LabelledSite(
        site_id="traits.php:28:$this->dispatch",
        stratum=Stratum.TRAIT,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="traits.php",
        line=28,
        receiver="$this",
        method="dispatch",
        caller="Eval\\Traits\\Campaign::run",
        accepted=(
            _set(_t("Eval\\Traits\\Campaign", "dispatch")),
            _set(_t("Eval\\Traits\\SendsMail", "dispatch")),
        ),
        why=(
            "PHP flattens the trait into the class, so both node shapes are defensible. Two "
            "accepted sets rather than one keeps the reference set from encoding a graph "
            "convention as a correctness claim."
        ),
    ),
    # --- chained calls ---------------------------------------------------------------------
    LabelledSite(
        site_id="chained.php:20:$this->provider->mailer",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="chained.php",
        line=20,
        receiver="$this->provider",
        method="mailer",
        caller="Eval\\Chained\\Registry::firstHop",
        accepted=(_set(_t("Eval\\Chained\\Provider", "mailer")),),
        why="The first hop of a chain is an ordinary promoted-property call and must not be lost with the second.",
    ),
    LabelledSite(
        site_id="chained.php:15:$this->provider->mailer()->reminderSubmitWeek",
        stratum=Stratum.CHAINED,
        expectation=Expectation.RESOLVE,
        in_scope=False,
        path="chained.php",
        line=15,
        receiver="$this->provider->mailer()",
        method="reminderSubmitWeek",
        caller="Eval\\Chained\\Registry::run",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why=(
            "`Provider::mailer(): Emails` makes the target provable from source, but this "
            "version does not track return types, so it is labelled resolvable and out of "
            "scope: missing it costs coverage, not recall."
        ),
    ),
    # --- interface-typed receiver ------------------------------------------------------------
    LabelledSite(
        site_id="interfaces.php:32:$this->transport->send",
        stratum=Stratum.INTERFACE_RECEIVER,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="interfaces.php",
        line=32,
        receiver="$this->transport",
        method="send",
        caller="Eval\\Iface\\Courier::deliver",
        accepted=(
            _set(_t("Eval\\Iface\\Transport", "send")),
            _set(_t("Eval\\Iface\\SmtpTransport", "send"), _t("Eval\\Iface\\NullTransport", "send")),
        ),
        why=(
            "8 % of injected properties on the reference application are interface-typed. "
            "Pointing at the interface declaration and fanning out to every implementation are "
            "both correct; picking one implementation is not, and that is what this site catches."
        ),
    ),
    # --- nullable and union types ---------------------------------------------------------
    LabelledSite(
        site_id="declared_property.php:25:$this->optional?->reminderSubmitMonth",
        stratum=Stratum.NULLABLE_TYPE,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="declared_property.php",
        line=25,
        receiver="$this->optional",
        method="reminderSubmitMonth",
        caller="Eval\\Declared\\Digest::maybeSend",
        accepted=(_set(_t(_EMAILS, "reminderSubmitMonth")),),
        why="null owns no methods, so `?Emails` reached through `?->` still has exactly one target.",
    ),
    LabelledSite(
        site_id="nullable_union.php:18:$this->maybeMailer?->reminderSubmitWeek",
        stratum=Stratum.NULLABLE_TYPE,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="nullable_union.php",
        line=18,
        receiver="$this->maybeMailer",
        method="reminderSubmitWeek",
        caller="Eval\\Variance\\Variant::nullableCall",
        accepted=(_set(_t(_EMAILS, "reminderSubmitWeek")),),
        why="Nullable on a promoted parameter rather than a declaration: same answer, different parse path.",
    ),
    LabelledSite(
        site_id="nullable_union.php:23:$this->either->get",
        stratum=Stratum.UNION_TYPE,
        expectation=Expectation.RESOLVE,
        in_scope=False,
        path="nullable_union.php",
        line=23,
        receiver="$this->either",
        method="get",
        caller="Eval\\Variance\\Variant::unionCall",
        accepted=(_set(_t(_EMAILS, "get"), _t(_CACHE, "get")),),
        why=(
            "`Emails|Cache` has two real targets. Out of scope because this version cannot "
            "express two; a resolver that picks one alternative is wrong, not partly right, "
            "and exact target-set accuracy is the number that says so."
        ),
        trap=True,
    ),
    # --- same-name traps ---------------------------------------------------------------------
    LabelledSite(
        site_id="same_name_traps.php:44:$this->cache->get",
        stratum=Stratum.SAME_NAME_TRAP,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path="same_name_traps.php",
        line=44,
        receiver="$this->cache",
        method="get",
        caller="Eval\\Traps\\Checkout::typedGet",
        accepted=(_set(_t(_CACHE, "get")),),
        why="Cache and Basket both declare get(); only the declared type picks Cache, and a name match picks at random.",
        trap=True,
    ),
    LabelledSite(
        site_id="same_name_traps.php:49:$this->store->get",
        stratum=Stratum.SAME_NAME_TRAP,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="same_name_traps.php",
        line=49,
        receiver="$this->store",
        method="get",
        caller="Eval\\Traps\\Checkout::untypedGet",
        accepted=_NO_TARGET,
        why="Untyped receiver, two classes declaring get(): the only correct output is no edge.",
        trap=True,
    ),
    LabelledSite(
        site_id="same_name_traps.php:54:$this->store->empty",
        stratum=Stratum.SAME_NAME_TRAP,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="same_name_traps.php",
        line=54,
        receiver="$this->store",
        method="empty",
        caller="Eval\\Traps\\Checkout::untypedEmpty",
        accepted=_NO_TARGET,
        why="The synthetic twin of the upstream failure where `empty()` was wired to a method named empty.",
        trap=True,
    ),
    # --- negatives that must stay unresolved ---------------------------------------------------
    LabelledSite(
        site_id="dynamic.php:29:$this->{$this->prop}->reminderSubmitWeek",
        stratum=Stratum.DYNAMIC_NAME,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="dynamic.php",
        line=29,
        receiver="$this->{$this->prop}",
        method="reminderSubmitWeek",
        caller="Eval\\Dynamic\\Caller::dynamicProperty",
        accepted=_NO_TARGET,
        why="The property name is a value. `Emails` is in scope in this file, which is exactly what makes the wrong edge tempting.",
        trap=True,
    ),
    LabelledSite(
        site_id="dynamic.php:34:$this->emails->$method",
        stratum=Stratum.DYNAMIC_NAME,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="dynamic.php",
        line=34,
        receiver="$this->emails",
        method="$method",
        caller="Eval\\Dynamic\\Caller::dynamicMethod",
        accepted=_NO_TARGET,
        why="Receiver type is known and the method name is not: half-knowledge must not become an edge to every Emails method.",
        trap=True,
    ),
    LabelledSite(
        site_id="dynamic.php:39:$this->magic->anythingAtAll",
        stratum=Stratum.MAGIC_CALL,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="dynamic.php",
        line=39,
        receiver="$this->magic",
        method="anythingAtAll",
        caller="Eval\\Dynamic\\Caller::magicCall",
        accepted=_NO_TARGET,
        why="Magic goes through __call; no declaration named anythingAtAll exists to point at.",
        trap=True,
    ),
    LabelledSite(
        site_id="dynamic.php:44:ReflectionMethod::invoke",
        stratum=Stratum.REFLECTION,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="dynamic.php",
        line=44,
        receiver="(new \\ReflectionClass($target))->getMethod('reminderSubmitWeek')",
        method="invoke",
        caller="Eval\\Dynamic\\Caller::viaReflection",
        accepted=_NO_TARGET,
        why="The method name appears as a string literal, which is the cheapest false positive available.",
        trap=True,
    ),
    LabelledSite(
        site_id="dynamic.php:49:$this->container->get('mailer')->reminderSubmitWeek",
        stratum=Stratum.SERVICE_LOCATOR,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path="dynamic.php",
        line=49,
        receiver="$this->container->get('mailer')",
        method="reminderSubmitWeek",
        caller="Eval\\Dynamic\\Caller::viaServiceLocator",
        accepted=_NO_TARGET,
        why=(
            "Only the second hop is labelled. The container's own `get` has a real target in "
            "vendor code that this corpus does not contain, and labelling what the set cannot "
            "check would be a guess."
        ),
        trap=True,
    ),
    # --- real sites, read off the reference application ----------------------------------------
    LabelledSite(
        site_id="cem:AzureUserAvatarHandler.php:23:$this->process",
        stratum=Stratum.SELF_RECEIVER,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path=str(REFERENCE_APP_ROOT / "src/Messages/Handler/AzureUserAvatarHandler.php"),
        line=23,
        receiver="$this",
        method="process",
        caller="App\\Messages\\Handler\\AzureUserAvatarHandler::__invoke",
        accepted=(_set(_t("App\\Messages\\Handler\\AzureUserAvatarHandler", "process")),),
        why="Declared at line 26 of the same file. A framework entry point calling its own method.",
    ),
    LabelledSite(
        site_id="cem:AzureUserAvatarHandler.php:28:$this->users->disableDoctrineListeners",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path=str(REFERENCE_APP_ROOT / "src/Messages/Handler/AzureUserAvatarHandler.php"),
        line=28,
        receiver="$this->users",
        method="disableDoctrineListeners",
        caller="App\\Messages\\Handler\\AzureUserAvatarHandler::process",
        accepted=(_set(_t("App\\Repository\\CEM\\UserRepository", "disableDoctrineListeners")),),
        why="`private readonly UserRepository $users`; target declared at src/Repository/CEM/UserRepository.php:50.",
    ),
    LabelledSite(
        site_id="cem:AzureUserAvatarHandler.php:36:$this->azure->users",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path=str(REFERENCE_APP_ROOT / "src/Messages/Handler/AzureUserAvatarHandler.php"),
        line=36,
        receiver="$this->azure",
        method="users",
        caller="App\\Messages\\Handler\\AzureUserAvatarHandler::process",
        accepted=(_set(_t("App\\Handler\\Azure", "users")),),
        why="`private readonly Azure $azure`; target declared at src/Handler/Azure.php:23. Only the first hop is labelled.",
    ),
    LabelledSite(
        site_id="cem:AzureUserAvatarHandler.php:53:$this->azure->avatar",
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE,
        in_scope=True,
        path=str(REFERENCE_APP_ROOT / "src/Messages/Handler/AzureUserAvatarHandler.php"),
        line=53,
        receiver="$this->azure",
        method="avatar",
        caller="App\\Messages\\Handler\\AzureUserAvatarHandler::process",
        accepted=(_set(_t("App\\Handler\\Azure", "avatar")),),
        why="Target declared at src/Handler/Azure.php:28. Same caller and receiver as the site above, different method.",
    ),
    LabelledSite(
        site_id="cem:AzureUserAvatarHandler.php:32:empty",
        stratum=Stratum.SAME_NAME_TRAP,
        expectation=Expectation.REFUSE,
        in_scope=True,
        path=str(REFERENCE_APP_ROOT / "src/Messages/Handler/AzureUserAvatarHandler.php"),
        line=32,
        receiver="",
        method="empty",
        caller="App\\Messages\\Handler\\AzureUserAvatarHandler::process",
        accepted=_NO_TARGET,
        why="`empty($user)` is a language construct. The real form of the upstream defect, in real code.",
        trap=True,
    ),
)


def site(site_id: str) -> LabelledSite:
    for entry in SITES:
        if entry.site_id == site_id:
            return entry
    raise KeyError(site_id)


def by_stratum() -> dict[str, list[LabelledSite]]:
    grouped: dict[str, list[LabelledSite]] = {}
    for entry in SITES:
        grouped.setdefault(entry.stratum.value, []).append(entry)
    return grouped


def stratum_counts() -> dict[str, int]:
    return dict(Counter(entry.stratum.value for entry in SITES))


def resolvable_sites(in_scope_only: bool = True) -> tuple[LabelledSite, ...]:
    return tuple(
        entry
        for entry in SITES
        if entry.expectation is Expectation.RESOLVE and (entry.in_scope or not in_scope_only)
    )


def negative_sites() -> tuple[LabelledSite, ...]:
    return tuple(entry for entry in SITES if entry.expectation is Expectation.REFUSE)


def validate() -> list[str]:
    """Invariants a hand-written set drifts out of. Returns problems, does not raise.

    Returning strings rather than raising lets one test report every broken label at once;
    a set this size is edited by hand and one assertion per run makes that tedious.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for entry in SITES:
        if entry.site_id in seen:
            problems.append(f"duplicate site_id: {entry.site_id}")
        seen.add(entry.site_id)
        if not entry.accepted:
            problems.append(f"{entry.site_id}: accepted must hold at least one answer")
            continue
        if entry.expectation is Expectation.REFUSE and entry.all_accepted_targets:
            problems.append(f"{entry.site_id}: REFUSE site carries targets")
        if entry.expectation is Expectation.RESOLVE and not entry.all_accepted_targets:
            problems.append(f"{entry.site_id}: RESOLVE site carries no target")
        if entry.line < 1:
            problems.append(f"{entry.site_id}: line number missing")
        if not entry.why.strip():
            problems.append(f"{entry.site_id}: no rationale")
    return problems
