"""Screening outcomes and the prohibited-item taxonomy.

Qt-free, like the rest of core/. The categories and their decision rule are the part
worth testing, and they should be testable with no display and no database.

Two rules shape everything here, and both come from docs/flow.md:

  Rule 1  The device never writes to a student record. With a separate detector there
          is no device input at all -- every outcome below is a person's judgement --
          so a ScreeningOutcome carries no student_id and reaches a student only
          through the arming scan.

  Rule 4  An unrecorded screening is NOT a clear one. See NOT_SCREENED.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Outcome(str, Enum):
    """What the person operating the detector concluded.

    CLEAR and COMMON_ITEMS are both "nothing to act on", kept apart because the
    difference between them is the only false-positive measure the study still has:
    now that the detector is an off-the-shelf device rather than ours, the useful
    number is what fraction of alarms turned out to be a phone.
    """

    CLEAR = "clear"                              # no alarm
    COMMON_ITEMS = "common_items"                # alarm, explained by declared items
    PROHIBITED = "prohibited"                    # alarm, prohibited item confirmed
    SCHOOL_HAZARD = "school_hazard"              # alarm, school tool -> custody
    PENDING_VERIFICATION = "pending_verification"  # inspection not finished
    NOT_SCREENED = "not_screened"                # nobody screened this student
    OVERRIDDEN = "overridden"                    # passed without inspection, with reason

    @property
    def is_resolved(self) -> bool:
        """False for outcomes that still need a human to finish the job."""
        return self not in (Outcome.PENDING_VERIFICATION, Outcome.NOT_SCREENED)

    @property
    def writes_incident(self) -> bool:
        return self is Outcome.PROHIBITED

    @property
    def writes_custody(self) -> bool:
        return self is Outcome.SCHOOL_HAZARD


# NOT_SCREENED exists because of one specific failure. If the screen times out with
# nobody pressing anything and the system recorded CLEAR, it would be asserting that a
# guard checked a bag when nobody did. In a study about safety that is fabricated data;
# in a real incident it is the school's liability. Silence means unscreened.
DEFAULT_OUTCOME = Outcome.NOT_SCREENED


@dataclass(frozen=True)
class Category:
    code: str
    label: str
    covers: str
    default_severity: int


# Disjoint by construction, keyed on WHAT MAKES THE OBJECT DANGEROUS rather than on
# what it is called. The list this replaces had five overlapping entries -- a dagger is
# bladed and pointed, a razor is bladed -- so two guards seeing the same knife would
# file it differently and the category counts would mean nothing.
CATEGORIES: tuple[Category, ...] = (
    Category("bladed", "Bladed object",
             "knife, dagger, razor, box cutter, blade fragment", 4),
    Category("blunt", "Blunt or impact object",
             "brass knuckles, hammer, metal pipe or bar", 4),
    Category("pointed", "Pointed object, not bladed",
             "ice pick, sharpened rod, awl", 3),
    Category("tool", "Tool with a legitimate school use",
             "scissors, cutter, screwdriver, compass", 1),
    Category("other", "Other prohibited object",
             "anything not covered above", 2),
)

BY_CODE = {c.code: c for c in CATEGORIES}

# Printed on the guard's screen. A taxonomy nobody can apply under time pressure at a
# gate is not a taxonomy.
DECISION_RULE = (
    "Has an edge -> Bladed. No edge but a point -> Pointed. Neither -> Blunt. "
    "If the item has an ordinary classroom use, choose Tool."
)

SEVERITY_MIN, SEVERITY_MAX = 1, 4


class InvalidCategory(ValueError):
    pass


def category(code: str) -> Category:
    try:
        return BY_CODE[code]
    except KeyError:
        raise InvalidCategory(
            f"{code!r} is not a prohibited-item category. "
            f"Valid: {', '.join(BY_CODE)}"
        ) from None


def default_severity(code: str) -> int:
    return category(code).default_severity


def validate_incident(
    code: str, item_description: str, severity: int, severity_reason: str | None,
) -> None:
    """Refuse an incident that could not be defended later.

    A description is mandatory because the category is for counting and the
    description is for knowing what happened -- "folding knife, ~8 cm blade" is what
    makes a record mean something a year later.

    A severity that differs from the category default needs a reason: a penknife and a
    hunting knife should not score the same, but an unexplained score is not evidence.
    """
    cat = category(code)

    if not (item_description or "").strip():
        raise ValueError("item_description is required: describe the object found")

    if not SEVERITY_MIN <= severity <= SEVERITY_MAX:
        raise ValueError(
            f"severity must be {SEVERITY_MIN}-{SEVERITY_MAX}, got {severity}"
        )

    if severity != cat.default_severity and not (severity_reason or "").strip():
        raise ValueError(
            f"severity {severity} differs from the default {cat.default_severity} "
            f"for {cat.label!r}; a reason is required"
        )
