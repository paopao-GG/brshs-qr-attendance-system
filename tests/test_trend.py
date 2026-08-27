"""Linear regression on the attendance trend.

docs/analytics-model.md section 2. The two traps it names are the two tests that matter
here: regressing a cumulative total instead of the daily rate, and ignoring that
consecutive school days are autocorrelated.
"""
import pytest

from trackify.analytics import trend
from trackify.analytics.trend import Insufficient, Trend


def record(conn, student_id, date, status, flags=""):
    from trackify.core.db import utcnow
    conn.execute(
        """INSERT INTO attendance_days (student_id, date, status, flags, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (student_id, date, status, flags, utcnow()),
    )


def day(index: int) -> str:
    """A date string for school day `index`, 1-based, starting 2026-09-01."""
    from datetime import date as Date
    from datetime import timedelta
    return (Date(2026, 9, 1) + timedelta(days=index - 1)).isoformat()


@pytest.fixture
def cohort(conn, section, make_student):
    return [make_student(first=f"S{n}", last=f"Test{n}") for n in range(1, 11)]


def fill(conn, cohort, rates):
    """Give each school day the attendance rate in `rates` (10 students, so tenths)."""
    for index, rate in enumerate(rates, start=1):
        attending = round(rate * len(cohort))
        for position, student in enumerate(cohort):
            record(conn, student, day(index),
                   "present" if position < attending else "absent")


# --- not enough data --------------------------------------------------------

def test_an_empty_database_says_what_is_needed(conn):
    result = trend.attendance_trend(conn)

    assert isinstance(result, Insufficient)
    assert result.n == 0
    assert "at least 3 school days" in result.reason


def test_two_days_is_still_not_enough(conn, cohort):
    fill(conn, cohort, [0.9, 0.8])
    result = trend.attendance_trend(conn)

    assert isinstance(result, Insufficient)
    assert result.n == 2


def test_an_unvarying_series_is_reported_as_no_variation_not_a_flat_trend(conn, cohort):
    """OLS gives a NaN p-value here. 'No variation to describe' is more useful in a
    spreadsheet than a blank cell."""
    fill(conn, cohort, [0.9, 0.9, 0.9, 0.9])
    result = trend.attendance_trend(conn)

    assert isinstance(result, Insufficient)
    assert "identical attendance rate" in result.reason


# --- the fit ----------------------------------------------------------------

def test_a_perfect_line_recovers_its_slope_and_r_squared(conn, cohort):
    """Attendance falling by exactly 10 points a day."""
    fill(conn, cohort, [1.0, 0.9, 0.8, 0.7, 0.6])
    result = trend.attendance_trend(conn)

    assert isinstance(result, Trend)
    assert result.n == 5
    assert round(result.slope, 10) == -0.1
    assert round(result.intercept, 10) == 1.1
    assert round(result.r_squared, 10) == 1.0
    assert result.direction == "falling"


def test_a_rising_series_reads_as_rising(conn, cohort):
    fill(conn, cohort, [0.5, 0.6, 0.7, 0.8, 0.9])
    result = trend.attendance_trend(conn)

    assert round(result.slope, 10) == 0.1
    assert result.direction == "rising"


def test_the_confidence_interval_brackets_the_slope(conn, cohort):
    fill(conn, cohort, [1.0, 0.8, 0.9, 0.7, 0.6, 0.7, 0.5])
    result = trend.attendance_trend(conn)

    assert result.ci_low <= result.slope <= result.ci_high


# --- the two traps from section 2 -------------------------------------------

def test_the_trend_regresses_the_daily_rate_not_a_running_total(conn, cohort):
    """THE trap. A cumulative series fits at R^2 ~ 0.99 with a slope that is just the
    mean rate restated -- it looks like the strongest result in the study and contains
    no information."""
    fill(conn, cohort, [0.9, 0.5, 0.9, 0.5, 0.9, 0.5])
    result = trend.attendance_trend(conn)

    rates = [rate for _, rate in result.days]
    assert rates == [0.9, 0.5, 0.9, 0.5, 0.9, 0.5], "these are daily rates"
    assert rates != sorted(rates), "a running total would be monotonic"
    # An alternating series has no trend. A cumulative one would fit near-perfectly.
    assert result.r_squared < 0.5


def test_durbin_watson_is_always_reported(conn, cohort):
    fill(conn, cohort, [1.0, 0.9, 0.8, 0.7, 0.6, 0.5])
    result = trend.attendance_trend(conn)

    assert result.durbin_watson > 0
    assert any("Durbin-Watson" in note for note in result.caveats)


def test_autocorrelation_is_called_out_when_present(conn, cohort):
    """A steadily drifting series has strongly autocorrelated residuals, which makes the
    p-value on the slope optimistic. Saying so is a stronger result than not looking."""
    fill(conn, cohort, [0.5, 0.6, 0.9, 1.0, 0.9, 0.6, 0.5, 0.6, 0.9, 1.0])
    result = trend.attendance_trend(conn)

    if result.autocorrelated:
        assert any("independence assumption" in note for note in result.caveats)
    else:
        assert any("no evidence of autocorrelation" in note for note in result.caveats)


def test_low_power_is_flagged_under_ten_days(conn, cohort):
    fill(conn, cohort, [1.0, 0.9, 0.8, 0.7])
    result = trend.attendance_trend(conn)

    assert result.low_power
    assert any("Low statistical power" in note for note in result.caveats)


# --- what counts ------------------------------------------------------------

def test_excused_days_leave_the_denominator(conn, section, make_student):
    """Same rule as the register and the XLSX export: a student with a medical
    certificate is neither attending nor truant."""
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    record(conn, a, day(1), "present")
    record(conn, b, day(1), "excused")

    rates = trend.daily_rates(conn)
    assert rates == [(day(1), 1.0)], "1 of 1 eligible, not 1 of 2"


def test_a_day_where_everyone_is_excused_is_dropped_not_scored_zero(conn, make_student):
    """A rate of 0% for a day nobody was expected in would drag the slope down."""
    a = make_student()
    record(conn, a, day(1), "excused")

    assert trend.daily_rates(conn) == []


def test_online_participation_counts_as_attending(conn, make_student):
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    record(conn, a, day(1), "online")
    record(conn, b, day(1), "absent")

    assert trend.daily_rates(conn) == [(day(1), 0.5)]


def test_late_counts_as_attending(conn, make_student):
    a = make_student()
    record(conn, a, day(1), "late")

    assert trend.daily_rates(conn) == [(day(1), 1.0)]


def test_superseded_rows_are_ignored(conn, make_student):
    """A corrected day is counted once, at its corrected value -- not twice, and not at
    the value the scanner originally recorded."""
    from trackify.core import corrections

    a = make_student()
    record(conn, a, day(1), "absent")
    corrections.correct(conn, a, day(1), corrections.CorrectionType.DATA_ERROR,
                        status="present", reason="scanner missed them",
                        actor_name="T. San Jose")

    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 2
    assert trend.daily_rates(conn) == [(day(1), 1.0)]


def test_a_section_filter_narrows_the_series(conn, section, make_student):
    mine = make_student()
    other_section = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Other', 9)").lastrowid
    theirs = conn.execute(
        """INSERT INTO students (lrn, first_name, last_name, section_id,
               guardian_mobile, consent_on_file, created_at)
           VALUES ('900', 'Not', 'Mine', ?, '639171234567', 1, '2026-01-01')""",
        (other_section,)).lastrowid

    record(conn, mine, day(1), "present")
    record(conn, theirs, day(1), "absent")

    assert trend.daily_rates(conn, section_id=section) == [(day(1), 1.0)]
    assert trend.daily_rates(conn) == [(day(1), 0.5)]
