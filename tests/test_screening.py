"""The prohibited-item taxonomy and screening outcomes.

The taxonomy is a research instrument before it is a UI: if two guards looking at the
same knife file it differently, the category counts in the incident summary mean
nothing. These tests are mostly about that property.
"""
import pytest

from trackify.core import screening
from trackify.core.screening import Outcome


# --- the taxonomy -----------------------------------------------------------

def test_categories_are_disjoint_by_what_makes_them_dangerous():
    """The list this replaced had a dagger matching three buttons at once."""
    codes = [c.code for c in screening.CATEGORIES]
    assert codes == ["bladed", "blunt", "pointed", "tool", "other"]
    assert len(set(codes)) == len(codes)


def test_every_category_has_a_default_severity_in_range():
    for cat in screening.CATEGORIES:
        assert screening.SEVERITY_MIN <= cat.default_severity <= screening.SEVERITY_MAX


def test_the_decision_rule_is_available_to_the_screen():
    """A taxonomy nobody can apply under time pressure at a gate is not a taxonomy."""
    assert "edge" in screening.DECISION_RULE
    assert "Pointed" in screening.DECISION_RULE


def test_unknown_category_names_the_valid_ones():
    with pytest.raises(screening.InvalidCategory) as exc:
        screening.category("dagger")
    assert "bladed" in str(exc.value)


def test_school_tools_are_the_lowest_severity():
    """A pair of art-class scissors is not a weapon; it routes to custody."""
    assert screening.default_severity("tool") == 1
    assert screening.default_severity("bladed") == 4


# --- incident validation ----------------------------------------------------

def test_description_is_mandatory():
    """The category is for counting; the description is for knowing what happened."""
    with pytest.raises(ValueError, match="item_description"):
        screening.validate_incident("bladed", "   ", 4, None)


def test_severity_matching_the_default_needs_no_reason():
    screening.validate_incident("bladed", "folding knife, ~8 cm blade", 4, None)


def test_changed_severity_requires_a_reason():
    """A penknife and a hunting knife should not score the same -- but an unexplained
    score is not evidence."""
    with pytest.raises(ValueError, match="reason is required"):
        screening.validate_incident("bladed", "penknife", 2, None)

    screening.validate_incident("bladed", "penknife", 2, "blunt tip, under 3 cm")


def test_severity_outside_the_scale_is_refused():
    with pytest.raises(ValueError, match="1-4"):
        screening.validate_incident("other", "thing", 5, "because")


# --- outcomes ---------------------------------------------------------------

def test_silence_is_not_screened_never_clear():
    """The most important line in this module. Recording 'clear' when nobody pressed
    anything would have the system assert a guard checked a bag when nobody did."""
    assert screening.DEFAULT_OUTCOME is Outcome.NOT_SCREENED
    assert screening.DEFAULT_OUTCOME is not Outcome.CLEAR


def test_unfinished_outcomes_are_flagged_as_unresolved():
    assert not Outcome.PENDING_VERIFICATION.is_resolved
    assert not Outcome.NOT_SCREENED.is_resolved
    for done in (Outcome.CLEAR, Outcome.COMMON_ITEMS, Outcome.PROHIBITED,
                 Outcome.SCHOOL_HAZARD, Outcome.OVERRIDDEN):
        assert done.is_resolved


def test_only_prohibited_writes_an_incident():
    """Rule 1: a clear screening must never attach anything to a named minor."""
    writing = [o for o in Outcome if o.writes_incident]
    assert writing == [Outcome.PROHIBITED]


def test_only_school_hazard_writes_custody():
    writing = [o for o in Outcome if o.writes_custody]
    assert writing == [Outcome.SCHOOL_HAZARD]


def test_clear_and_common_items_are_kept_apart():
    """The difference between them is the only false-positive measure left now that
    the detector is an off-the-shelf device rather than ours."""
    assert Outcome.CLEAR is not Outcome.COMMON_ITEMS
    assert Outcome.CLEAR.is_resolved and Outcome.COMMON_ITEMS.is_resolved
