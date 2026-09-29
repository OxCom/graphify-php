"""Score a resolver's output against the labelled reference set.

This module knows nothing about how any resolver works, and imports nothing from the package.
It takes two pieces of data — labelled sites, and predicted targets keyed by site id — so a
tree-sitter resolver, a PHPStan-backed one and a resolver written next year in another
language are all scored by the same code with no edit here.

The numbers are kept apart on purpose:

- **Precision** over predicted targets. A resolver raises it by predicting less, which is the
  correct trade when the alternative is a wrong edge.
- **Recall within supported scope**, over sites the reference set labels resolvable *and*
  in scope for the version under test. Out-of-scope sites are excluded here and still counted
  in coverage, so refusing to support a pattern cannot quietly inflate recall.
- **Exact target-set accuracy** over sites with more than one legitimate target. Picking one
  arm of a union is a wrong answer, not a partial one, and per-target precision alone would
  score it as half right.
- **Operational coverage**, over every eligible site the resolver was offered. This is the
  number that tracks the 24 %-of-callables figure the package exists to move, and it is the
  one that rises when a resolver starts guessing.

Call sites and caller-callee pairs are reported separately. Two sites calling two methods on
the same property are two sites and, for a graph, possibly one edge each but one *pair* when
the method matches; pooling them lets a file with a hot loop of calls carry the average.

Errors are itemised rather than summed, because "precision 0.94" does not say whether the 6 %
is a wrong class (a broken type table) or a wrong method (a broken node lookup).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class TargetLike(Protocol):
    class_fqn: str
    method: str


@dataclass(frozen=True)
class Prediction:
    """One target a resolver claims for one site.

    `confidence` and `via` are carried, never interpreted: they belong in the report so a
    reviewer can see whether the errors cluster in INFERRED targets, but a scorer that gave
    them meaning would be scoring one resolver's vocabulary.
    """

    site_id: str
    class_fqn: str
    method: str
    confidence: str = ""
    via: str = ""


@dataclass(frozen=True)
class ResolverOutput:
    """Everything the scorer accepts from a resolver.

    `observed` is the set of eligible sites the resolver was actually offered. It defaults to
    every labelled site, which is the honest default: a resolver that never saw a file still
    failed to resolve it, and letting a resolver shrink its own denominator by skipping files
    is how coverage becomes meaningless.
    """

    predictions: tuple[Prediction, ...] = ()
    unresolved: Mapping[str, str] = field(default_factory=dict)   # site_id -> reason
    observed: frozenset[str] | None = None


ErrorKind = str
WRONG_CLASS: ErrorKind = "wrong_class"
WRONG_METHOD: ErrorKind = "wrong_method"
WRONG_CLASS_AND_METHOD: ErrorKind = "wrong_class_and_method"
RESOLVED_A_NEGATIVE: ErrorKind = "resolved_a_negative"
EXTRA_TARGET: ErrorKind = "extra_target"
UNKNOWN_SITE: ErrorKind = "unknown_site"


@dataclass(frozen=True)
class ScoringError:
    site_id: str
    kind: ErrorKind
    predicted: str
    expected: str
    stratum: str = ""


@dataclass(frozen=True)
class Ratio:
    """A measurement that always carries its denominator.

    Separate from a bare float because "resolved 400 calls" means nothing without "out of how
    many", and a regression that halves the denominator keeps producing a healthy-looking
    percentage.
    """

    hits: int
    total: int

    @property
    def value(self) -> float | None:
        return self.hits / self.total if self.total else None

    def __str__(self) -> str:
        if not self.total:
            return "n/a (0 observations)"
        return f"{self.value:.3f} ({self.hits}/{self.total})"


@dataclass(frozen=True)
class Report:
    target_precision: Ratio
    site_recall_in_scope: Ratio
    site_recall_any_correct: Ratio
    multi_target_exact: Ratio
    coverage: Ratio
    negatives_held: Ratio
    pair_precision: Ratio
    pair_recall_in_scope: Ratio
    per_stratum_recall: dict[str, Ratio]
    unresolved_by_reason: dict[str, int]
    errors: tuple[ScoringError, ...]
    error_kinds: dict[str, int]
    sample_sizes: dict[str, int]
    zero_error_upper_bound: float | None
    zero_error_basis: str


def _key(target: TargetLike) -> tuple[str, str]:
    return (target.class_fqn, target.method)


def _accepted_keys(site) -> tuple[frozenset[tuple[str, str]], ...]:
    return tuple(frozenset(_key(t) for t in option) for option in site.accepted)


def _best_option(predicted: frozenset, options: Sequence[frozenset]) -> frozenset:
    """The accepted answer the prediction is closest to.

    A site with several accepted answers must be judged against the one the resolver was
    aiming at; scoring an interface fan-out against the interface-declaration answer would
    charge every implementation as a wrong target.
    """
    return max(options, key=lambda option: (len(predicted & option), -len(predicted ^ option)))


def _classify(predicted_key: tuple[str, str], option: frozenset[tuple[str, str]]) -> ErrorKind:
    class_fqn, method = predicted_key
    same_class = any(c == class_fqn for c, _ in option)
    same_method = any(m == method for _, m in option)
    if same_class and not same_method:
        return WRONG_METHOD
    if same_method and not same_class:
        return WRONG_CLASS
    if same_class and same_method:
        # Both halves appear in the accepted set but not as a pair: an over-eager fan-out.
        return EXTRA_TARGET
    return WRONG_CLASS_AND_METHOD


# Rule of three: with zero observed errors in n independent trials, the one-sided 95 % upper
# bound on the error rate is about 3/n (Hanley & Lippman-Scott, "If nothing goes wrong, is
# everything all right?", JAMA 1983;249:1743-5). It assumes the trials are independent. These
# are not: several sites come from one fixture and one authoring pass, so a single systematic
# mistake shows up in a group of them at once and the true bound is looser than 3/n. Treat the
# number as the most optimistic reading available, never as the expected field error rate.
_RULE_OF_THREE_Z = 3.0


def zero_error_upper_bound(trials: int) -> float | None:
    return _RULE_OF_THREE_Z / trials if trials else None


def score(sites: Sequence, output: ResolverOutput) -> Report:
    by_id = {site.site_id: site for site in sites}
    observed = output.observed if output.observed is not None else frozenset(by_id)

    predicted_by_site: dict[str, set[tuple[str, str]]] = {}
    unknown_sites: list[Prediction] = []
    for prediction in output.predictions:
        if prediction.site_id not in by_id:
            unknown_sites.append(prediction)
            continue
        predicted_by_site.setdefault(prediction.site_id, set()).add(_key(prediction))

    errors: list[ScoringError] = []
    correct_targets = 0
    total_targets = 0

    recall_hits = recall_total = 0
    any_hits = any_total = 0
    multi_hits = multi_total = 0
    negatives_held = negatives_total = 0
    stratum_hits: Counter[str] = Counter()
    stratum_total: Counter[str] = Counter()

    predicted_pairs: set[tuple[str, str, str]] = set()
    correct_pairs: set[tuple[str, str, str]] = set()
    expected_pairs: set[tuple[str, str, str]] = set()

    for prediction in unknown_sites:
        # Not silently dropped: a prediction for a site nobody labelled cannot be judged, and
        # counting it as correct or as wrong would both be inventions.
        errors.append(
            ScoringError(prediction.site_id, UNKNOWN_SITE, f"{prediction.class_fqn}::{prediction.method}", "")
        )

    for site in sites:
        options = _accepted_keys(site)
        predicted = frozenset(predicted_by_site.get(site.site_id, ()))
        target_option = _best_option(predicted, options) if predicted else options[0]
        is_negative = not site.all_accepted_targets

        total_targets += len(predicted)
        for predicted_key in sorted(predicted):
            if predicted_key in target_option:
                correct_targets += 1
                continue
            kind = RESOLVED_A_NEGATIVE if is_negative else _classify(predicted_key, target_option)
            errors.append(
                ScoringError(
                    site_id=site.site_id,
                    kind=kind,
                    predicted=f"{predicted_key[0]}::{predicted_key[1]}",
                    expected=", ".join(sorted(f"{c}::{m}" for c, m in target_option)) or "(no target)",
                    stratum=getattr(site.stratum, "value", str(site.stratum)),
                )
            )
        missing = target_option - predicted
        if predicted and missing:
            errors.append(
                ScoringError(
                    site_id=site.site_id,
                    kind="missing_target",
                    predicted=", ".join(sorted(f"{c}::{m}" for c, m in predicted)),
                    expected=", ".join(sorted(f"{c}::{m}" for c, m in target_option)),
                    stratum=getattr(site.stratum, "value", str(site.stratum)),
                )
            )

        if is_negative:
            negatives_total += 1
            if not predicted:
                negatives_held += 1
            continue

        exact = predicted == target_option
        any_correct = bool(predicted & target_option)
        stratum = getattr(site.stratum, "value", str(site.stratum))

        if site.in_scope:
            recall_total += 1
            any_total += 1
            stratum_total[stratum] += 1
            if exact:
                recall_hits += 1
                stratum_hits[stratum] += 1
            if any_correct:
                any_hits += 1

        if site.multi_target:
            multi_total += 1
            if exact:
                multi_hits += 1

        for class_fqn, method in target_option:
            if site.in_scope:
                expected_pairs.add((site.caller, class_fqn, method))
        for class_fqn, method in predicted:
            pair = (site.caller, class_fqn, method)
            predicted_pairs.add(pair)
            if (class_fqn, method) in target_option:
                correct_pairs.add(pair)

    resolved_sites = sum(1 for site_id in observed if predicted_by_site.get(site_id))

    reasons = Counter(output.unresolved.values())

    hard_errors = [e for e in errors if e.kind != "missing_target"]
    bound_basis = (
        f"{total_targets} predicted targets"
        if total_targets
        else "no predicted targets, so no bound applies"
    )

    return Report(
        target_precision=Ratio(correct_targets, total_targets),
        site_recall_in_scope=Ratio(recall_hits, recall_total),
        site_recall_any_correct=Ratio(any_hits, any_total),
        multi_target_exact=Ratio(multi_hits, multi_total),
        coverage=Ratio(resolved_sites, len(observed)),
        negatives_held=Ratio(negatives_held, negatives_total),
        pair_precision=Ratio(len(correct_pairs), len(predicted_pairs)),
        pair_recall_in_scope=Ratio(len(correct_pairs & expected_pairs), len(expected_pairs)),
        per_stratum_recall={s: Ratio(stratum_hits[s], stratum_total[s]) for s in sorted(stratum_total)},
        unresolved_by_reason=dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        errors=tuple(errors),
        error_kinds=dict(sorted(Counter(e.kind for e in errors).items())),
        sample_sizes={
            "labelled_sites": len(sites),
            "observed_sites": len(observed),
            "in_scope_resolvable_sites": recall_total,
            "negative_sites": negatives_total,
            "multi_target_sites": multi_total,
            "predicted_targets": total_targets,
            "unique_predicted_pairs": len(predicted_pairs),
            "unique_expected_pairs": len(expected_pairs),
        },
        zero_error_upper_bound=zero_error_upper_bound(total_targets) if not hard_errors else None,
        zero_error_basis=bound_basis,
    )


def format_report(report: Report) -> str:
    lines = [
        "call-site metrics",
        f"  target precision           {report.target_precision}",
        f"  recall (in scope, exact)   {report.site_recall_in_scope}",
        f"  recall (in scope, any hit) {report.site_recall_any_correct}",
        f"  multi-target exact match   {report.multi_target_exact}",
        f"  negatives held             {report.negatives_held}",
        f"  operational coverage       {report.coverage}",
        "caller-callee pair metrics",
        f"  pair precision             {report.pair_precision}",
        f"  pair recall (in scope)     {report.pair_recall_in_scope}",
        "per-stratum recall (in scope, exact)",
    ]
    for stratum, ratio in report.per_stratum_recall.items():
        lines.append(f"  {stratum:<26} {ratio}")
    lines.append("unresolved by reason")
    for reason, count in report.unresolved_by_reason.items() or [("(none reported)", 0)]:
        lines.append(f"  {reason:<26} {count}")
    lines.append("sample sizes")
    for name, count in report.sample_sizes.items():
        lines.append(f"  {name:<26} {count}")
    if report.zero_error_upper_bound is not None:
        lines.append(
            f"zero errors observed; 95 % upper bound on the error rate ~= "
            f"{report.zero_error_upper_bound:.3f} over {report.zero_error_basis}, "
            f"loosened by correlated cases from shared fixtures"
        )
    else:
        lines.append("errors by kind")
        for kind, count in report.error_kinds.items():
            lines.append(f"  {kind:<26} {count}")
    return "\n".join(lines)


def output_from_json(payload: Mapping) -> ResolverOutput:
    """Build a `ResolverOutput` from parsed JSON.

    The file format is the resolver-independent seam: anything that can write

        {"predictions": [{"site_id": ..., "class_fqn": ..., "method": ...}],
         "unresolved": {"<site_id>": "<reason>"},
         "observed": ["<site_id>", ...]}

    can be scored, including a resolver that is not Python.
    """
    observed = payload.get("observed")
    return ResolverOutput(
        predictions=predictions_from_rows(payload.get("predictions", ())),
        unresolved=dict(payload.get("unresolved", {})),
        observed=frozenset(observed) if observed is not None else None,
    )


def predictions_from_rows(rows: Iterable[Mapping[str, str]]) -> tuple[Prediction, ...]:
    """Build predictions from plain dicts, so a resolver can hand over JSON and be scored.

    The adapter lives here rather than in a resolver because the scorer owns the input shape;
    a resolver in another language has no way to import a dataclass.
    """
    return tuple(
        Prediction(
            site_id=row["site_id"],
            class_fqn=row["class_fqn"],
            method=row["method"],
            confidence=row.get("confidence", ""),
            via=row.get("via", ""),
        )
        for row in rows
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    import sys

    from .reference_set import SITES

    parser = argparse.ArgumentParser(description="Score a resolver's output against the reference set.")
    parser.add_argument("predictions", help="JSON file, or - for stdin")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.predictions == "-" else open(args.predictions, encoding="utf-8").read()
    report = score(SITES, output_from_json(json.loads(text)))
    print(format_report(report))
    # Non-zero on any wrong target, so a build pipeline can gate on precision without parsing
    # the text above. A missing target is not an error here: under-resolving is the safe
    # failure and it shows up in recall.
    return 1 if any(error.kind != "missing_target" for error in report.errors) else 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
