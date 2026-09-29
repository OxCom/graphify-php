"""The reference set is hand-written, so its integrity is itself a test subject.

A label that cites a line the fixture no longer has is worse than a missing label: it scores a
resolver against text that is not there and nothing complains.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import reference_set as rs  # noqa: E402


def test_invariants_hold():
    assert rs.validate() == []


def test_every_site_has_a_stratum_and_a_rationale():
    for site in rs.SITES:
        assert isinstance(site.stratum, rs.Stratum)
        assert len(site.why) > 20, site.site_id


@pytest.mark.parametrize("site", rs.SITES, ids=lambda s: s.site_id)
def test_label_points_at_real_source(site):
    path = site.resolve_path()
    if not path.exists():
        pytest.skip(f"corpus not present: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert site.line <= len(lines), f"{site.site_id}: file has {len(lines)} lines"
    text = lines[site.line - 1]
    # The method name is the one token every site shape shares; receivers vary too much
    # (`parent`, a chain, a language construct) to assert on.
    needle = site.method.lstrip("$")
    assert needle in text, f"{site.site_id}: line {site.line} reads {text!r}"


def test_strata_cover_the_patterns_the_harness_claims():
    present = set(rs.stratum_counts())
    required = {
        rs.Stratum.PROMOTED_CTOR_PROPERTY.value,
        rs.Stratum.DECLARED_PROPERTY.value,
        rs.Stratum.CTOR_BODY_ASSIGNMENT.value,
        rs.Stratum.LOCAL_FROM_NEW.value,
        rs.Stratum.INHERITANCE.value,
        rs.Stratum.PARENT_CALL.value,
        rs.Stratum.TRAIT.value,
        rs.Stratum.CHAINED.value,
        rs.Stratum.INTERFACE_RECEIVER.value,
        rs.Stratum.NULLABLE_TYPE.value,
        rs.Stratum.UNION_TYPE.value,
        rs.Stratum.SAME_NAME_TRAP.value,
        rs.Stratum.DYNAMIC_NAME.value,
        rs.Stratum.MAGIC_CALL.value,
        rs.Stratum.REFLECTION.value,
        rs.Stratum.SERVICE_LOCATOR.value,
    }
    assert required <= present, sorted(required - present)


def test_negative_cases_exist_in_useful_numbers():
    negatives = rs.negative_sites()
    # Precision is the metric the negatives feed. A handful of them cannot distinguish a
    # resolver that refuses correctly from one that happens to miss.
    assert len(negatives) >= 8
    assert all(not site.all_accepted_targets for site in negatives)


def test_same_name_traps_have_a_matching_wrong_answer_available():
    """A trap only traps if the wrong target exists in the corpus."""
    trap = rs.site("same_name_traps.php:49:$this->store->get")
    source = (rs.FIXTURE_ROOT / "same_name_traps.php").read_text(encoding="utf-8")
    assert source.count("public function get(") >= 2
    assert source.count("public function empty(") >= 2
    assert trap.expectation is rs.Expectation.REFUSE


def test_multi_target_sites_are_derived_not_declared():
    union = rs.site("nullable_union.php:23:$this->either->get")
    assert union.multi_target
    single = rs.site("promoted.php:15:$this->emails->reminderSubmitWeek")
    assert not single.multi_target


def test_out_of_scope_sites_still_carry_their_true_target():
    chained = rs.site("chained.php:15:$this->provider->mailer()->reminderSubmitWeek")
    assert chained.in_scope is False
    assert chained.expectation is rs.Expectation.RESOLVE
    assert chained.all_accepted_targets
