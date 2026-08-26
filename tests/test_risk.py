"""Composite risk: the saturating terms, the weighted sum, and the bands.

docs/analytics-model.md sections 6 and 7. The composite arithmetic is worked through by
hand in the doc, so those figures are asserted directly.

The properties that matter as much as the arithmetic: the score is bounded, one extreme
student cannot rescale everyone else, and a P(absent) that came from an observed
frequency rather than a fitted model always says so.
"""
import math

import pytest

from trackify.analytics import ahp, risk
from trackify.analytics.risk import MODEL_FITTED, MODEL_OBSERVED


def record(conn, student_id, date, status, flags=""):
    from trackify.core.db import utcnow
    conn.execute(
        """INSERT INTO attendance_days (student_id, date, status, flags, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (student_id, date, status, flags, utcnow()),
    )


def day(index: int) -> str:
    from datetime import date as Date, timedelta
    return (Date(2026, 9, 1) + timedelta(days=index - 1)).isoformat()


# --- the saturating terms ---------------------------------------------------

def test_the_documented_severity_table_reproduces():
    """Section 6 tabulates 1 - exp(-0.25 * x) at 0, 1, 2, 4, 8 and 12."""
    expected = {0: 0.000, 1: 0.221, 2: 0.393, 4: 0.632, 8: 0.865, 12: 0.950}
    for count, value in expected.items():
        assert round(risk.saturating(count, 0.25), 3) == value


def test_the_documented_tardiness_term_reproduces():
    """Section 6's worked example: 3 tardies at mu = 0.20 gives 0.4512."""
    assert round(risk.saturating(3, 0.20), 4) == 0.4512


def test_the_terms_are_bounded():
    """Mathematically the range is [0, 1). In float64 exp(-125) underflows to zero, so
    an absurd count saturates at exactly 1.0 -- still bounded, never above."""
    for count in (0, 1, 5, 50, 500):
        assert 0.0 <= risk.saturating(count, 0.25) <= 1.0
    assert risk.saturating(50, 0.25) < 1.0, "realistic counts stay strictly inside"


def test_zero_occurrences_scores_zero():
    assert risk.saturating(0, 0.20) == 0.0


def test_one_extreme_student_cannot_rescale_anyone_else():
    """Section 6's reason for saturating exponentials over min-max: with min-max, adding
    one student with eight early departures silently lowers everyone else's score, so
    nothing can be compared across sections or over time."""
    quiet = risk.saturating(1, 0.25)
    assert risk.saturating(1, 0.25) == quiet          # unchanged by context
    assert risk.saturating(8, 0.25) > quiet
    # The mapping is a pure function of the count, so it cannot depend on a cohort.
    assert risk.saturating(1, 0.25) == quiet


# --- the composite ----------------------------------------------------------

def test_the_documented_composite_reproduces(config):
    """Section 6, recomputed for early departure as the third criterion: P = 0.42,
    3 tardies, 2 early departures, against the documented weights."""
    weights = ahp.derive(ahp.DOCUMENTED_MATRIX)
    t = risk.saturating(3, config.risk.mu_tardiness)
    e = risk.saturating(2, config.risk.nu_early_departure)

    composite = (weights.absence * 0.42 + weights.tardiness * t
                 + weights.early_departure * e)

    assert round(t, 4) == 0.4512
    assert round(e, 4) == 0.3935
    assert round(composite, 4) == 0.4031
    assert risk.band_for(composite, config) == "Monitor"


def test_the_composite_is_bounded_in_zero_to_one(config):
    weights = ahp.derive()
    for p, late, early in ((0, 0, 0), (1, 100, 100), (0.5, 3, 2), (1, 0, 0)):
        score = (weights.absence * p
                 + weights.tardiness * risk.saturating(late, 0.2)
                 + weights.early_departure * risk.saturating(early, 0.25))
        assert 0.0 <= score <= 1.0


# --- the bands --------------------------------------------------------------

@pytest.mark.parametrize("score,band", [
    (0.00, "Low"), (0.29, "Low"), (0.2999, "Low"),
    (0.30, "Monitor"), (0.54, "Monitor"),
    (0.55, "Elevated"), (0.74, "Elevated"),
    (0.75, "High"), (1.00, "High"),
])
def test_each_band_boundary_lands_on_the_documented_side(config, score, band):
    """A boundary decides whether a real child is referred to guidance. Off-by-one here
    is not a rounding detail."""
    assert risk.band_for(score, config) == band


def test_the_bands_come_from_config_not_from_code(config):
    import dataclasses
    strict = dataclasses.replace(
        config, risk=dataclasses.replace(config.risk, band_low=0.10))

    assert risk.band_for(0.2, config) == "Low"
    assert risk.band_for(0.2, strict) == "Monitor"


# --- computing over a cohort ------------------------------------------------

def test_an_empty_database_scores_nobody(conn, config):
    report = risk.compute(conn, config)

    assert report.rows == []
    assert "No attendance has been recorded" in report.model_note


def test_every_active_student_is_scored(conn, section, make_student, config):
    """Section 8: compute risk for everyone and act on it selectively. Scoring only
    already-flagged students leaves no negative cases and makes validation impossible."""
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    record(conn, a, day(1), "present")
    record(conn, b, day(1), "absent")

    report = risk.compute(conn, config)
    assert {row.name for row in report.rows} == {"One, A", "Two, B"}


def test_a_deactivated_student_is_not_scored(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "absent")
    conn.execute("UPDATE students SET active = 0 WHERE id = ?", (a,))

    assert risk.compute(conn, config).rows == []


def test_tardies_and_early_departures_feed_their_terms(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "late")
    record(conn, a, day(2), "present", flags="early_departure")
    record(conn, a, day(3), "late")

    row = risk.compute(conn, config).rows[0]
    assert row.n_late == 2
    assert row.n_early == 1
    assert round(row.tardiness, 4) == round(risk.saturating(2, 0.20), 4)
    assert round(row.early_departure, 4) == round(risk.saturating(1, 0.25), 4)


def test_rows_come_back_worst_first(conn, make_student, config):
    """The point of the list is who to look at, so the top of it should be the answer."""
    calm = make_student(first="Calm", last="Aaa")
    risky = make_student(first="Risky", last="Zzz")
    for index in range(1, 6):
        record(conn, calm, day(index), "present")
        record(conn, risky, day(index), "absent", flags="early_departure")

    rows = risk.compute(conn, config).rows
    assert rows[0].name == "Zzz, Risky"
    assert rows[0].composite > rows[-1].composite


# --- the model, and what happens without one --------------------------------

def test_an_unfittable_model_falls_back_and_labels_the_number(conn, make_student, config):
    """Silently swapping an observed frequency for a model prediction is the failure to
    avoid -- the two mean different things and only one is a forecast."""
    a = make_student()
    record(conn, a, day(1), "absent")
    record(conn, a, day(2), "present")

    report = risk.compute(conn, config)

    assert report.model is None
    assert report.model_note
    assert report.rows[0].p_absent_source == MODEL_OBSERVED
    assert round(report.rows[0].p_absent, 4) == 0.5      # 1 of 2 counted days


def test_no_absences_at_all_is_explained_not_crashed(conn, make_student, config):
    # Enough rows that the sample-size check passes and only the missing-events check
    # can fire -- otherwise this asserts the wrong branch.
    a = make_student()
    for index in range(1, 62):
        record(conn, a, day(index), "present")

    report = risk.compute(conn, config)
    assert "nothing for a model of absence to learn from" in report.model_note
    assert report.rows[0].p_absent == 0.0


def test_excused_days_are_not_attendance_opportunities(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "absent")
    record(conn, a, day(2), "excused")

    row = risk.compute(conn, config).rows[0]
    assert row.n_days == 1, "the excused day is not counted either way"
    assert row.p_absent == 1.0


# --- persistence ------------------------------------------------------------

def test_a_persisted_score_records_the_weights_that_produced_it(conn, section,
                                                                make_student, config):
    """Without weights_version a stored score cannot be explained six months later."""
    ahp.save(conn, ahp.DOCUMENTED_MATRIX, elicited_from="the panel")
    a = make_student()
    record(conn, a, day(1), "absent")

    risk.compute(conn, config, persist=True)

    row = conn.execute("SELECT * FROM risk_scores").fetchone()
    assert row["weights_version"] == 1
    assert row["band"] in ("Low", "Monitor", "Elevated", "High")
    assert math.isclose(
        row["composite"],
        row["p_absent"] * ahp.active(conn).absence
        + row["tardiness_score"] * ahp.active(conn).tardiness
        + row["early_departure_score"] * ahp.active(conn).early_departure,
        abs_tol=1e-9,
    )


def test_nothing_is_written_unless_persist_is_asked_for(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "absent")
    risk.compute(conn, config)

    assert conn.execute("SELECT COUNT(*) FROM risk_scores").fetchone()[0] == 0


def test_the_report_carries_the_weights_it_used(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "absent")

    report = risk.compute(conn, config)
    assert report.weights is not None
    assert report.weights.elicited is False, "no panel recorded yet"
    assert "PLACEHOLDER" in report.weights.caveat


def test_band_counts_are_summarised(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "present")

    counts = risk.compute(conn, config).by_band()
    assert sum(counts.values()) == 1
    assert set(counts) == {"Low", "Monitor", "Elevated", "High"}


# --- prohibited-item incidents ----------------------------------------------
#
# A floor on the band, never a term in the composite. The reason is arithmetic and is
# asserted below: a weighted term could not have raised a band at all.


def incident(conn, student_id, *, severity=4, category="bladed",
             description="folding penknife", day="2026-09-02"):
    """One confirmed incident, through the real chain: scan -> screening -> incident."""
    from trackify.core.db import utcnow

    scan_id = conn.execute(
        """INSERT INTO scan_events (student_id, scanned_at, date, direction, method)
           VALUES (?, ?, ?, 'in', 'scan')""",
        (student_id, f"{day}T07:00:00", day)).lastrowid
    event = conn.execute(
        """INSERT INTO screening_events (scan_event_id, occurred_at, metal_detected,
               outcome) VALUES (?, ?, 1, 'prohibited')""",
        (scan_id, utcnow())).lastrowid
    conn.execute(
        """INSERT INTO incidents (student_id, screening_event_id, occurred_at, category,
               item_description, severity)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (student_id, event, f"{day}T07:05:00", category, description, severity))


def test_a_weighted_term_could_not_have_reached_monitor(config):
    """Why this is a floor and not a fourth AHP criterion.

    One incident through the usual saturating transform is 0.2212, and Monitor starts
    at 0.30. Reaching Monitor on one incident alone would need a weight of 1.356 -- and
    weights sum to 1. A student found with a bladed weapon would still have read "Low"
    however the panel weighted it.
    """
    one = risk.saturating(1, config.risk.nu_early_departure)

    assert round(one, 4) == 0.2212
    assert one < config.risk.band_low, "even at weight 1.0 it cannot reach Monitor"
    assert round(config.risk.band_low / one, 3) == 1.356


# --- the floor --------------------------------------------------------------

@pytest.mark.parametrize("severity,band", [
    (1, "Monitor"), (2, "Monitor"), (3, "Elevated"), (4, "High"),
])
def test_each_severity_floors_at_its_configured_band(conn, make_student, config,
                                                     severity, band):
    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, severity=severity)

    row = risk.compute(conn, config).rows[0]
    assert row.band == band
    assert row.max_severity == severity


def test_a_clean_record_is_still_low(conn, make_student, config):
    """The floor must not fire for a student who has done nothing."""
    a = make_student()
    record(conn, a, day(1), "present")

    row = risk.compute(conn, config).rows[0]
    assert row.band == "Low"
    assert row.n_incidents == 0
    assert row.band_source == risk.COMPOSITE


def test_a_floor_never_lowers_a_band(config):
    """A severity-1 tool must not drag a genuinely high-scoring student downwards."""
    assert risk.worst_of("High", "Monitor") == "High"
    assert risk.worst_of("Elevated", "Monitor") == "Elevated"
    assert risk.worst_of("Low", "High") == "High"
    assert risk.worst_of("Monitor", None) == "Monitor"


def test_the_composite_is_not_changed_by_an_incident(conn, make_student, config):
    """The score keeps meaning what it meant. Only the band moves."""
    a = make_student()
    for index in range(1, 4):
        record(conn, a, day(index), "late")
    before = risk.compute(conn, config).rows[0]

    incident(conn, a, severity=4)
    after = risk.compute(conn, config).rows[0]

    assert after.composite == before.composite
    assert after.band == "High" and before.band == "Low"


def test_the_band_source_says_which_rule_applied(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, severity=3)

    row = risk.compute(conn, config).rows[0]
    assert row.band_source == "incident floor (severity 3)"


def test_the_floor_comes_from_config_not_from_code(conn, make_student, config):
    import dataclasses

    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, severity=1)

    lenient = dataclasses.replace(
        config, risk=dataclasses.replace(
            config.risk, incident_floor=("Low", "Low", "Low", "Low")))

    assert risk.compute(conn, config).rows[0].band == "Monitor"
    assert risk.compute(conn, lenient).rows[0].band == "Low"


def test_the_kinds_are_reported_without_the_description(conn, make_student, config):
    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, category="bladed", description="folding penknife")
    incident(conn, a, category="pointed", description="sharpened compass",
             day="2026-09-03", severity=3)

    row = risk.compute(conn, config).rows[0]
    assert row.n_incidents == 2
    assert row.incident_kinds == ("bladed", "pointed"), "sorted, so exports are stable"
    assert "penknife" not in repr(row), "RA 10173: the description never leaves the table"


def test_an_incident_after_the_end_date_is_not_counted(conn, make_student, config):
    """occurred_at is a timestamp, so an end DATE has to reach the end of that day."""
    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, severity=4, day="2026-09-10")

    assert risk.compute(conn, config, end="2026-09-09").rows[0].band == "Low"
    assert risk.compute(conn, config, end="2026-09-10").rows[0].band == "High"


def test_a_persisted_score_records_the_incident_and_why(conn, section, make_student,
                                                        config):
    """Without band_source a stored 'High' on a 0.06 composite looks like a bug."""
    a = make_student()
    record(conn, a, day(1), "present")
    incident(conn, a, severity=4)

    risk.compute(conn, config, persist=True)

    row = conn.execute("SELECT * FROM risk_scores").fetchone()
    assert row["incidents"] == 1
    assert row["band"] == "High"
    assert row["band_source"] == "incident floor (severity 4)"
    assert row["composite"] < config.risk.band_low, "the band came from the floor"
