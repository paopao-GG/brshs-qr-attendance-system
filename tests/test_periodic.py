"""Weekly summaries and the monthly absence reminder.

Two different shapes on purpose: the summary is a batch a person triggers, the reminder
is one student crossing a threshold on the day it happens. The properties that matter
most are the ones that stop a parent being texted twice, and the ones that stop a
corrected day being reported as an absence.
"""
import dataclasses

import pytest

from trackify.core import db
from trackify.core.attendance import Trigger
from trackify.notify import gsm7, periodic

from .conftest import at


def record(conn, student_id, date, status, superseded_by=None):
    return conn.execute(
        """INSERT INTO attendance_days (student_id, date, status, flags, created_at,
               superseded_by)
           VALUES (?, ?, ?, '', ?, ?)""",
        (student_id, date, status, db.utcnow(), superseded_by),
    ).lastrowid


@pytest.fixture
def consenting(make_student):
    """A student whose guardian can be texted. make_student consents by default."""
    return make_student()


@pytest.fixture
def service(conn, config):
    from trackify.core.service import ScanService
    return ScanService(conn, config)


def bodies(conn, trigger=None):
    return [row["body"] for row in conn.execute("SELECT trigger, body FROM notifications")
            if trigger is None or row["trigger"] == trigger.value]


def absences(conn, student_id, count, start_day=17):
    for offset in range(count):
        record(conn, student_id, f"2026-08-{start_day + offset:02d}", "absent")


# --- periods ----------------------------------------------------------------

def test_the_school_week_is_monday_to_friday():
    """Not the calendar week. A school week is Mon-Fri, and the label has to be stable
    however late in the week the button is pressed."""
    for day in ("2026-08-17", "2026-08-19", "2026-08-21"):
        start, end, label = periodic.school_week(day)
        assert (start, end) == ("2026-08-17", "2026-08-21")
        assert label == "Aug 17-21"


def test_a_weekend_press_still_names_that_week():
    start, end, _ = periodic.school_week("2026-08-22")          # Saturday
    assert (start, end) == ("2026-08-17", "2026-08-21")


def test_a_week_spanning_two_months_names_both():
    assert periodic.school_week("2026-08-31")[2] == "Aug 31-Sep 4"


def test_the_month_range_covers_the_whole_month():
    assert periodic.month_range("2026-08-14") == ("2026-08-01", "2026-08-31", "August")


# --- counting ---------------------------------------------------------------

def test_counts_are_per_student_over_the_range(conn, make_student):
    a, b = make_student(first="A", last="One"), make_student(first="B", last="Two")
    record(conn, a, "2026-08-17", "present")
    record(conn, a, "2026-08-18", "late")
    record(conn, a, "2026-08-19", "absent")
    record(conn, b, "2026-08-17", "present")

    stats = periodic.stats_for(conn, "2026-08-17", "2026-08-21")
    assert (stats[a].present, stats[a].late, stats[a].absent) == (1, 1, 1)
    assert stats[a].days == 3
    assert round(stats[a].rate, 4) == round(2 / 3, 4)
    assert stats[b].days == 1


def test_an_excused_day_is_not_an_attendance_opportunity(conn, make_student):
    a = make_student()
    record(conn, a, "2026-08-17", "present")
    record(conn, a, "2026-08-18", "excused")

    stats = periodic.stats_for(conn, "2026-08-17", "2026-08-21")
    assert stats[a].days == 1, "the excused day leaves the denominator entirely"
    assert stats[a].rate == 1.0


def test_a_corrected_day_is_counted_at_its_corrected_value(conn, make_student):
    """A parent must never be told about an absence an adviser has already excused."""
    a = make_student()
    new = record(conn, a, "2026-08-18", "excused")
    record(conn, a, "2026-08-18", "absent", superseded_by=new)

    stats = periodic.stats_for(conn, "2026-08-17", "2026-08-21")
    assert stats[a].absent == 0
    assert stats[a].days == 0


def test_days_outside_the_week_are_not_counted(conn, make_student):
    a = make_student()
    record(conn, a, "2026-08-14", "absent")                  # the Friday before
    record(conn, a, "2026-08-17", "present")

    assert periodic.stats_for(conn, "2026-08-17", "2026-08-21")[a].absent == 0


# --- the weekly summary -----------------------------------------------------

def test_a_summary_carries_the_week_counts(conn, consenting, config):
    record(conn, consenting, "2026-08-17", "present")
    record(conn, consenting, "2026-08-18", "late")
    record(conn, consenting, "2026-08-19", "absent")

    run = periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    assert run.queued == 1
    body = bodies(conn, Trigger.SUMMARY)[0]
    assert "present 1" in body and "late 1" in body and "absent 1" in body
    assert "of 3 school days" in body
    assert "Aug 17-21" in body


def test_a_dry_run_writes_nothing(conn, consenting, config):
    """The confirmation dialog has to say how many texts before anybody agrees."""
    record(conn, consenting, "2026-08-17", "present")

    preview = periodic.weekly_summaries(conn, config, week_of="2026-08-19",
                                        dry_run=True)

    assert preview.eligible == 1
    assert preview.queued == 0
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_pressing_the_button_twice_queues_nothing_more(conn, consenting, config):
    """The one failure a parent actually notices."""
    record(conn, consenting, "2026-08-17", "present")

    first = periodic.weekly_summaries(conn, config, week_of="2026-08-19")
    second = periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    assert first.queued == 1
    assert second.queued == 0
    assert "already queued (idempotent)" in second.skipped
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_a_different_week_is_a_different_message(conn, consenting, config):
    record(conn, consenting, "2026-08-17", "present")
    record(conn, consenting, "2026-08-24", "present")

    periodic.weekly_summaries(conn, config, week_of="2026-08-19")
    periodic.weekly_summaries(conn, config, week_of="2026-08-26")

    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_a_student_with_nothing_recorded_gets_no_summary(conn, consenting, config):
    """No rows at all: never reaches the loop, so nothing is queued and nobody is
    counted as eligible."""
    run = periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    assert (run.queued, run.eligible) == (0, 0)


def test_a_week_of_nothing_but_excused_days_is_skipped(conn, consenting, config):
    """A summary reading "0 of 0 school days" tells a parent nothing. Reachable
    because excused days leave the denominator."""
    record(conn, consenting, "2026-08-17", "excused")
    record(conn, consenting, "2026-08-18", "excused")

    run = periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    assert run.queued == 0
    assert run.eligible == 0
    assert "no attendance recorded" in run.skipped


def test_a_guardian_without_consent_is_refused_and_counted(conn, make_student, config):
    """The consent gate holds here as everywhere, and the run reports it rather than
    reporting a silent success."""
    a = make_student()
    conn.execute("UPDATE students SET consent_on_file = 0 WHERE id = ?", (a,))
    record(conn, a, "2026-08-17", "present")

    run = periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    assert run.eligible == 1
    assert run.queued == 0
    assert run.skipped == {"no consent on file": 1}


def test_the_summary_body_fits_one_segment(conn, consenting, config):
    for index, day in enumerate(("2026-08-17", "2026-08-18", "2026-08-19",
                                 "2026-08-20", "2026-08-21")):
        record(conn, consenting, day, "late" if index % 2 else "present")
    periodic.weekly_summaries(conn, config, week_of="2026-08-19")

    body = bodies(conn, Trigger.SUMMARY)[0]
    assert gsm7.segments(body) == 1, f"{len(body)} chars: {body}"
    assert gsm7.is_gsm7(body)


# --- the absence reminder ---------------------------------------------------

def test_nothing_is_said_below_the_threshold(conn, consenting, config):
    absences(conn, consenting, 1)

    assert periodic.absence_reminder(
        conn, config, consenting, "2026-08-17", at(16, 0)) is None
    assert bodies(conn, Trigger.REMINDER) == []


def test_the_warning_fires_at_the_warn_threshold(conn, consenting, config):
    absences(conn, consenting, 2)
    result = periodic.absence_reminder(conn, config, consenting, "2026-08-18",
                                       at(16, 0))

    assert result is not None and result.queued
    body = bodies(conn, Trigger.REMINDER)[0]
    assert "2 absences in August" in body
    assert "1 more is allowed this month" in body


def test_the_limit_message_says_it_is_the_limit(conn, consenting, config):
    absences(conn, consenting, 3)
    periodic.absence_reminder(conn, config, consenting, "2026-08-19", at(16, 0))

    body = bodies(conn, Trigger.REMINDER)[0]
    assert "3 absences in August" in body
    assert "limit of 3" in body


def test_it_goes_quiet_past_the_limit(conn, consenting, config):
    """A text on every further absence is nagging, and by then the conversation
    belongs to a person."""
    absences(conn, consenting, 5)

    assert periodic.absence_reminder(
        conn, config, consenting, "2026-08-21", at(16, 0)) is None
    assert bodies(conn, Trigger.REMINDER) == []


def test_each_threshold_texts_once(conn, consenting, config):
    absences(conn, consenting, 2)
    periodic.absence_reminder(conn, config, consenting, "2026-08-18", at(16, 0))
    periodic.absence_reminder(conn, config, consenting, "2026-08-18", at(16, 5))

    assert len(bodies(conn, Trigger.REMINDER)) == 1


def test_a_new_month_starts_the_count_again(conn, consenting, config):
    absences(conn, consenting, 2)
    periodic.absence_reminder(conn, config, consenting, "2026-08-18", at(16, 0))
    record(conn, consenting, "2026-09-01", "absent")
    record(conn, consenting, "2026-09-02", "absent")
    periodic.absence_reminder(conn, config, consenting, "2026-09-02", at(16, 0))

    texts = bodies(conn, Trigger.REMINDER)
    assert len(texts) == 2
    assert any("August" in b for b in texts)
    assert any("September" in b for b in texts)


def test_an_excused_absence_does_not_count_towards_the_limit(conn, consenting, config):
    """The correction is the whole point of having one."""
    absences(conn, consenting, 1)
    new = record(conn, consenting, "2026-08-18", "excused")
    record(conn, consenting, "2026-08-18", "absent", superseded_by=new)

    assert periodic.absences_in_month(conn, consenting, "2026-08-18") == 1
    assert periodic.absence_reminder(
        conn, config, consenting, "2026-08-18", at(16, 0)) is None


def test_the_thresholds_come_from_config(conn, consenting, config):
    strict = dataclasses.replace(
        config, notifications=dataclasses.replace(
            config.notifications, monthly_absence_limit=2, absence_warn_at=1))
    absences(conn, consenting, 1)

    assert periodic.absence_reminder(
        conn, config, consenting, "2026-08-17", at(16, 0)) is None
    assert periodic.absence_reminder(
        conn, strict, consenting, "2026-08-17", at(16, 0)).queued


def test_reminders_can_be_turned_off(conn, consenting, config):
    off = dataclasses.replace(
        config, notifications=dataclasses.replace(
            config.notifications, absence_reminders=False))
    absences(conn, consenting, 2)

    assert periodic.absence_reminder(
        conn, off, consenting, "2026-08-18", at(16, 0)) is None


def test_the_reminder_body_fits_one_segment(conn, consenting, config):
    absences(conn, consenting, 2)
    periodic.absence_reminder(conn, config, consenting, "2026-08-18", at(16, 0))

    body = bodies(conn, Trigger.REMINDER)[0]
    assert gsm7.segments(body) == 1, f"{len(body)} chars: {body}"
    assert gsm7.is_gsm7(body)


def test_the_reminder_names_no_consequence_it_cannot_back_up(conn, consenting, config):
    """It says contact the school. What happens next is the school's decision and
    belongs to a person, not to an SMS template."""
    absences(conn, consenting, 3)
    periodic.absence_reminder(conn, config, consenting, "2026-08-19", at(16, 0))

    body = bodies(conn, Trigger.REMINDER)[0].lower()
    for word in ("drop", "fail", "expel", "suspend", "penalty", "sanction"):
        assert word not in body


# --- the reminder rides the end-of-day close --------------------------------

def test_closing_a_day_queues_the_threshold_warning(conn, consenting, config, service):
    """A warning that arrives a fortnight late is not a warning."""
    absences(conn, consenting, 1)
    service.close_day("2026-08-18", at=at(16, 30, day="2026-08-18"))

    triggers = {row["trigger"] for row in
                conn.execute("SELECT trigger FROM notifications")}
    assert triggers == {"absent", "reminder"}, "the absence and its warning"
    assert "1 more is allowed" in bodies(conn, Trigger.REMINDER)[0]
