"""Scorer tests on hand-made inputs.

The resolvers here are hand-written prediction lists, not code under test. That is the point:
each one is a defect shape stated in data — over-prediction, under-prediction, right class and
wrong method, an answer where the correct output was silence — and the test says which number
is supposed to move. A metric that does not move for its own defect shape is not measuring.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import score as sc  # noqa: E402
from eval.reference_set import Expectation, LabelledSite, Stratum, Target  # noqa: E402


def _site(site_id, accepted, in_scope=True, caller="App\\C::caller", multi=False):
    return LabelledSite(
        site_id=site_id,
        stratum=Stratum.PROMOTED_CTOR_PROPERTY,
        expectation=Expectation.RESOLVE if accepted != (frozenset(),) else Expectation.REFUSE,
        in_scope=in_scope,
        path="synthetic.php",
        line=1,
        receiver="$this->x",
        method="m",
        caller=caller,
        accepted=accepted,
        why="hand-made site for scorer tests",
    )


A = _site("A", (frozenset({Target("App\\Emails", "send")}),))
B = _site("B", (frozenset({Target("App\\Emails", "queue")}),))
NEG = _site("N", (frozenset(),))
MULTI = _site("M", (frozenset({Target("App\\X", "get"), Target("App\\Y", "get")}),))
OUT = _site("O", (frozenset({Target("App\\Chained", "deep")}),), in_scope=False)

SITES = (A, B, NEG, MULTI, OUT)

PERFECT = (
    sc.Prediction("A", "App\\Emails", "send"),
    sc.Prediction("B", "App\\Emails", "queue"),
    sc.Prediction("M", "App\\X", "get"),
    sc.Prediction("M", "App\\Y", "get"),
    sc.Prediction("O", "App\\Chained", "deep"),
)


def test_perfect_resolver_scores_clean():
    report = sc.score(SITES, sc.ResolverOutput(predictions=PERFECT))
    assert report.target_precision.value == 1.0
    assert report.site_recall_in_scope == sc.Ratio(3, 3)
    assert report.multi_target_exact == sc.Ratio(1, 1)
    assert report.negatives_held == sc.Ratio(1, 1)
    assert report.coverage == sc.Ratio(4, 5)   # the negative is eligible and correctly unresolved
    assert report.errors == ()


def test_over_prediction_costs_precision_and_leaves_recall_alone():
    output = sc.ResolverOutput(
        predictions=PERFECT
        + (
            sc.Prediction("N", "App\\Basket", "empty"),      # should have stayed silent
            sc.Prediction("O", "App\\Wrong", "deep"),        # out of scope, so not in recall
        )
    )
    report = sc.score(SITES, output)
    baseline = sc.score(SITES, sc.ResolverOutput(predictions=PERFECT))
    assert report.site_recall_in_scope == baseline.site_recall_in_scope
    assert report.target_precision.value < baseline.target_precision.value
    assert report.target_precision == sc.Ratio(5, 7)
    assert report.negatives_held == sc.Ratio(0, 1)
    assert report.error_kinds[sc.RESOLVED_A_NEGATIVE] == 1


def test_extra_target_on_an_in_scope_site_costs_exact_recall_not_any_hit_recall():
    output = sc.ResolverOutput(predictions=PERFECT + (sc.Prediction("A", "App\\Basket", "send"),))
    report = sc.score(SITES, output)
    assert report.site_recall_in_scope == sc.Ratio(2, 3)
    assert report.site_recall_any_correct == sc.Ratio(3, 3)
    assert report.error_kinds[sc.WRONG_CLASS] == 1


def test_under_prediction_costs_recall_and_coverage_not_precision():
    output = sc.ResolverOutput(predictions=tuple(p for p in PERFECT if p.site_id != "B"))
    report = sc.score(SITES, output)
    assert report.target_precision.value == 1.0
    assert report.site_recall_in_scope == sc.Ratio(2, 3)
    assert report.coverage == sc.Ratio(3, 5)


def test_right_class_wrong_method_is_named_as_such():
    output = sc.ResolverOutput(predictions=(sc.Prediction("A", "App\\Emails", "sendNow"),))
    report = sc.score(SITES, output)
    assert report.error_kinds[sc.WRONG_METHOD] == 1
    assert report.target_precision == sc.Ratio(0, 1)
    error = next(e for e in report.errors if e.kind == sc.WRONG_METHOD)
    assert error.expected == "App\\Emails::send"


def test_resolving_a_negative_is_its_own_error_kind():
    output = sc.ResolverOutput(predictions=(sc.Prediction("N", "App\\Cache", "empty"),))
    report = sc.score(SITES, output)
    assert report.negatives_held == sc.Ratio(0, 1)
    assert report.error_kinds[sc.RESOLVED_A_NEGATIVE] == 1
    assert report.zero_error_upper_bound is None


def test_partial_multi_target_answer_passes_precision_and_fails_exactness():
    """Precision alone scores half an answer as a whole one; this is why both are reported."""
    output = sc.ResolverOutput(predictions=(sc.Prediction("M", "App\\X", "get"),))
    report = sc.score(SITES, output)
    assert report.target_precision == sc.Ratio(1, 1)
    assert report.multi_target_exact == sc.Ratio(0, 1)
    assert any(e.kind == "missing_target" for e in report.errors)


def test_alternative_accepted_answers_are_both_correct():
    site = _site(
        "T",
        (
            frozenset({Target("App\\Campaign", "dispatch")}),
            frozenset({Target("App\\SendsMail", "dispatch")}),
        ),
    )
    for class_fqn in ("App\\Campaign", "App\\SendsMail"):
        report = sc.score((site,), sc.ResolverOutput(predictions=(sc.Prediction("T", class_fqn, "dispatch"),)))
        assert report.target_precision.value == 1.0, class_fqn
        assert report.site_recall_in_scope == sc.Ratio(1, 1)

    wrong = sc.score((site,), sc.ResolverOutput(predictions=(sc.Prediction("T", "App\\Other", "dispatch"),)))
    assert wrong.target_precision == sc.Ratio(0, 1)


def test_call_sites_and_pairs_are_counted_separately():
    shared = (
        _site("S1", (frozenset({Target("App\\Emails", "send")}),), caller="App\\Reminder::weekly"),
        _site("S2", (frozenset({Target("App\\Emails", "send")}),), caller="App\\Reminder::weekly"),
    )
    output = sc.ResolverOutput(
        predictions=(
            sc.Prediction("S1", "App\\Emails", "send"),
            sc.Prediction("S2", "App\\Emails", "send"),
        )
    )
    report = sc.score(shared, output)
    assert report.sample_sizes["in_scope_resolvable_sites"] == 2
    assert report.sample_sizes["unique_predicted_pairs"] == 1
    assert report.pair_recall_in_scope == sc.Ratio(1, 1)


def test_predictions_for_unlabelled_sites_are_reported_not_ignored():
    output = sc.ResolverOutput(predictions=(sc.Prediction("nope", "App\\Emails", "send"),))
    report = sc.score(SITES, output)
    assert report.error_kinds[sc.UNKNOWN_SITE] == 1
    assert report.target_precision == sc.Ratio(0, 0)


def test_unresolved_reasons_are_passed_through_as_a_histogram():
    output = sc.ResolverOutput(
        predictions=PERFECT,
        unresolved={"N": "dynamic_method_name", "X": "dynamic_method_name", "Y": "chained_call"},
    )
    report = sc.score(SITES, output)
    assert report.unresolved_by_reason == {"dynamic_method_name": 2, "chained_call": 1}


def test_observed_defaults_to_every_labelled_site():
    """A resolver that skipped files must not shrink its own coverage denominator."""
    full = sc.score(SITES, sc.ResolverOutput(predictions=PERFECT))
    narrowed = sc.score(SITES, sc.ResolverOutput(predictions=PERFECT, observed=frozenset({"A"})))
    assert full.coverage.total == 5
    assert narrowed.coverage.total == 1


def test_zero_error_bound_follows_the_rule_of_three():
    report = sc.score(SITES, sc.ResolverOutput(predictions=PERFECT))
    assert report.zero_error_upper_bound == 3.0 / 5
    assert sc.zero_error_upper_bound(0) is None


def test_ratio_reports_its_denominator():
    assert str(sc.Ratio(3, 4)) == "0.750 (3/4)"
    assert "n/a" in str(sc.Ratio(0, 0))


def test_format_report_mentions_the_bound_and_its_weakness():
    text = sc.format_report(sc.score(SITES, sc.ResolverOutput(predictions=PERFECT)))
    assert "upper bound" in text
    assert "correlated" in text
    assert "operational coverage" in text


def test_scorer_never_imports_the_package_under_test():
    """SOLID, stated as a test: the scorer takes data, so it must not depend on a resolver."""
    source = (Path(__file__).resolve().parents[1] / "eval" / "score.py").read_text(encoding="utf-8")
    assert "graphify_php" not in source


def test_json_seam_accepts_a_resolver_that_is_not_python(tmp_path):
    payload = {
        "predictions": [{"site_id": "A", "class_fqn": "App\\Emails", "method": "send", "confidence": "EXTRACTED"}],
        "unresolved": {"N": "dynamic_method_name"},
        "observed": ["A", "N"],
    }
    output = sc.output_from_json(payload)
    report = sc.score(SITES, output)
    assert report.target_precision == sc.Ratio(1, 1)
    assert report.coverage == sc.Ratio(1, 2)
    assert report.unresolved_by_reason == {"dynamic_method_name": 1}
    assert output.predictions[0].confidence == "EXTRACTED"


def test_cli_exits_non_zero_on_a_wrong_target(tmp_path, capsys):
    import json

    from eval import reference_set as rs

    good = rs.site("promoted.php:15:$this->emails->reminderSubmitWeek")
    target = next(iter(good.canonical))
    clean = tmp_path / "clean.json"
    clean.write_text(
        json.dumps(
            {
                "predictions": [
                    {"site_id": good.site_id, "class_fqn": target.class_fqn, "method": target.method}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert sc.main([str(clean)]) == 0

    dirty = tmp_path / "dirty.json"
    dirty.write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "site_id": "same_name_traps.php:49:$this->store->get",
                        "class_fqn": "Eval\\Traps\\Basket",
                        "method": "get",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert sc.main([str(dirty)]) == 1
    assert "negatives held" in capsys.readouterr().out
