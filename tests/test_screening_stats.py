"""Descriptive statistics for the screening procedure.

These are counts, never scores. prohibited-items.md section 9 is explicit that incidents
must not enter the risk composite: over a short study the count is near zero for every
student, and a near-constant criterion contributes noise and cannot be validated.
"""

from trackify.analytics import screening
from trackify.core.db import utcnow

DAY = "2026-09-01"


def scan(conn, student_id, at="2026-09-01T07:00:00"):
    return conn.execute(
        """INSERT INTO scan_events (student_id, scanned_at, date, direction, method)
           VALUES (?, ?, ?, 'in', 'scan')""",
        (student_id, at, at[:10]),
    ).lastrowid


def screen(conn, scan_id, outcome, metal=0):
    return conn.execute(
        """INSERT INTO screening_events
           (scan_event_id, occurred_at, metal_detected, outcome)
           VALUES (?, ?, ?, ?)""",
        (scan_id, utcnow(), metal, outcome),
    ).lastrowid


def incident(conn, student_id, screening_id, category="bladed", severity=2):
    conn.execute(
        """INSERT INTO incidents (student_id, screening_event_id, occurred_at,
               category, item_description, severity)
           VALUES (?, ?, ?, ?, 'penknife', ?)""",
        (student_id, screening_id, DAY + "T07:05:00", category, severity),
    )


# --- the empty case ---------------------------------------------------------

def test_an_empty_database_says_no_screening_has_happened(conn):
    summary = screening.summarise(conn)

    assert summary.scans == 0
    assert summary.coverage is None
    assert "No scans recorded" in summary.notes[0]


def test_rates_are_none_not_zero_when_undefined(conn, make_student):
    """0% coverage and 'nothing measured' are different claims, and a zero in a
    spreadsheet reads as the first."""
    scan(conn, make_student())
    summary = screening.summarise(conn)

    assert summary.coverage == 0.0          # a real 0: one scan, none screened
    assert summary.alarm_rate is None       # undefined: nothing was screened
    assert summary.confirmation_rate is None


# --- the procedure metrics --------------------------------------------------

def test_coverage_ignores_the_out_scan(conn, make_student):
    """Screening happens at the gate on the way IN.

    Every student also scans out, so dividing by both directions caps coverage near
    50% however thorough the guards are -- a perfect procedure would have reported
    itself as half-failing. Caught on simulated data reading 51.4%.
    """
    a = make_student()
    entry = scan(conn, a, "2026-09-01T07:00:00")
    conn.execute(
        """INSERT INTO scan_events (student_id, scanned_at, date, direction, method)
           VALUES (?, '2026-09-01T16:10:00', '2026-09-01', 'out', 'scan')""", (a,))
    screen(conn, entry, "clear")

    summary = screening.summarise(conn)
    assert summary.scans == 2
    assert summary.arrivals == 1
    assert summary.coverage == 1.0, "the one arrival was screened; coverage is 100%"


def test_a_not_screened_row_does_not_count_as_screened(conn, make_student):
    """It is a record that screening did NOT happen. Counting it would put coverage at
    100% exactly when nobody was being screened."""
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    screen(conn, scan(conn, a), "clear")
    screen(conn, scan(conn, b), "not_screened")

    summary = screening.summarise(conn)
    assert summary.screened == 2, "both are screening records"
    assert summary.examined == 1, "but only one person was actually examined"
    assert summary.coverage == 0.5


def test_alarm_rate_is_over_the_bags_actually_examined(conn, make_student):
    students = [make_student(first=f"S{n}", last=f"T{n}") for n in range(3)]
    scans = [scan(conn, s) for s in students]
    screen(conn, scans[0], "clear", metal=0)
    screen(conn, scans[1], "common_items", metal=1)
    screen(conn, scans[2], "not_screened", metal=0)

    summary = screening.summarise(conn)
    assert summary.alarm_rate == 0.5, "1 alarm out of 2 examined, not 3 records"


def test_coverage_is_screenings_over_scans(conn, make_student):
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    first = scan(conn, a)
    scan(conn, b)
    screen(conn, first, "clear")

    assert screening.summarise(conn).coverage == 0.5


def test_alarm_and_confirmation_rates(conn, make_student):
    students = [make_student(first=f"S{n}", last=f"T{n}") for n in range(4)]
    scans = [scan(conn, s) for s in students]

    screen(conn, scans[0], "clear", metal=0)
    screen(conn, scans[1], "common_items", metal=1)
    third = screen(conn, scans[2], "prohibited", metal=1)
    screen(conn, scans[3], "school_hazard", metal=1)
    incident(conn, students[2], third)

    summary = screening.summarise(conn)
    assert summary.screened == 4
    assert summary.alarms == 3
    assert summary.confirmed == 2                       # prohibited + school_hazard
    assert round(summary.alarm_rate, 4) == 0.75
    assert round(summary.confirmation_rate, 4) == round(2 / 3, 4)


def test_not_screened_is_reported_as_a_choice_not_a_gap(conn, make_student):
    """Silence is 'not_screened', never 'clear' -- a person chose it, and the count is
    a real measure rather than missing data."""
    first = scan(conn, make_student())
    screen(conn, first, "not_screened")

    summary = screening.summarise(conn)
    assert summary.outcomes["not_screened"] == 1
    assert any("deliberate outcome a person chose" in n for n in summary.notes)


def test_every_outcome_appears_even_at_zero(conn, make_student):
    screen(conn, scan(conn, make_student()), "clear")
    summary = screening.summarise(conn)

    assert set(summary.outcomes) == set(screening.OUTCOMES)
    assert summary.outcomes["overridden"] == 0


# --- incidents --------------------------------------------------------------

def test_incidents_are_counted_by_category_and_severity(conn, make_student):
    a = make_student()
    event = screen(conn, scan(conn, a), "prohibited", metal=1)
    incident(conn, a, event, category="bladed", severity=3)

    summary = screening.summarise(conn)
    assert summary.incidents_by_category["bladed"] == 1
    assert summary.incidents_by_severity[3] == 1
    assert summary.severity_total == 3
    assert summary.incident_total == 1


def test_a_small_incident_count_is_flagged_as_undermodellable(conn, make_student):
    a = make_student()
    event = screen(conn, scan(conn, a), "prohibited", metal=1)
    incident(conn, a, event)

    notes = screening.summarise(conn).notes
    assert any("far too small to model or to weight" in n for n in notes)


def test_the_summary_contains_no_student_identifiers(conn, make_student):
    """incidents.visibility defaults to 'restricted': a record naming a minor beside a
    prohibited item is sensitive personal information under RA 10173."""
    a = make_student(first="Juan", last="Dela Cruz")
    event = screen(conn, scan(conn, a), "prohibited", metal=1)
    incident(conn, a, event)

    text = repr(screening.summarise(conn))
    assert "Juan" not in text
    assert "Dela Cruz" not in text
    assert "penknife" not in text


# --- custody ----------------------------------------------------------------

def test_custody_is_counted_by_status(conn, make_student):
    a = make_student()
    conn.execute(
        """INSERT INTO custody_items (student_id, item_description, status, collected_at)
           VALUES (?, 'cutter', 'held', ?)""", (a, utcnow()))
    conn.execute(
        """INSERT INTO custody_items (student_id, item_description, status,
               collected_at, released_unbacked)
           VALUES (?, 'scissors', 'released', ?, 1)""", (a, utcnow()))

    summary = screening.summarise(conn)
    assert summary.custody_total == 2
    assert summary.custody_by_status["held"] == 1
    assert summary.released_unbacked == 1
    assert any("no hazard request on file" in n for n in summary.notes)


# --- date ranges ------------------------------------------------------------

def test_a_date_range_narrows_the_counts(conn, make_student):
    a = make_student()
    scan(conn, a, "2026-09-01T07:00:00")
    scan(conn, a, "2026-09-05T07:00:00")

    assert screening.summarise(conn).scans == 2
    assert screening.summarise(conn, start="2026-09-03").scans == 1
    assert screening.summarise(conn, end="2026-09-02").scans == 1
