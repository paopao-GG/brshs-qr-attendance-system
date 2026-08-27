"""Periodic notifications: the weekly attendance summary and the absence reminder.

Two different shapes, deliberately.

  Summary    A batch. Every consenting guardian gets one message covering the school
             week. Nobody is singled out, and a week of perfect attendance is worth
             saying out loud -- most parents never hear from a school unless something
             is wrong. Sent when a person presses the button, because 70-odd texts
             leaving at once is an event somebody should decide on.

  Reminder   A single student crossing a threshold, sent the day it happens. A warning
             that arrives a fortnight late is not a warning. It rides the end-of-day
             close, where the absence is detected and where the absence notification
             already goes out, so it costs no new scheduling.

Both bodies contain counts of the guardian's OWN child and nothing else -- no rank, no
comparison with classmates, no risk band. sms-notifications.md section 6.

Nothing here sends. Everything is queued, and the worker drains it -- the same rule as
a scan.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta

from ..core import corrections
from ..core.attendance import Trigger
from ..core.config import Config
from ..core.dates import MONTHS
from . import queue

# One definition, in core. An excused day is not a day the student failed to attend, so
# it leaves the denominator -- see corrections.NON_OPPORTUNITY.
PRESENT = corrections.PRESENT_STATUSES
COUNTED = corrections.COUNTED_STATUSES


@dataclass(frozen=True)
class PeriodStats:
    """One student's attendance over a date range."""

    student_id: int
    present: int = 0
    late: int = 0
    absent: int = 0

    @property
    def days(self) -> int:
        """Attendance opportunities. Excused days are not among them."""
        return self.present + self.late + self.absent

    @property
    def attended(self) -> int:
        return self.present + self.late

    @property
    def rate(self) -> float | None:
        return self.attended / self.days if self.days else None


@dataclass
class SummaryRun:
    """What one press of the button did."""

    period: str = ""
    start: str = ""
    end: str = ""
    queued: int = 0
    eligible: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


# --- periods ----------------------------------------------------------------

def school_week(day: Date | str) -> tuple[str, str, str]:
    """The Monday-to-Friday week containing `day`, and a label for it.

    Monday-based rather than the calendar week, because that is what a school week is
    and because it makes the label stable however late in the week the button is
    pressed.
    """
    day = Date.fromisoformat(day) if isinstance(day, str) else day
    monday = day - timedelta(days=day.weekday())
    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat(), _week_label(monday, friday)


def _week_label(monday: Date, friday: Date) -> str:
    if monday.month == friday.month:
        return f"{MONTHS[monday.month - 1][:3]} {monday.day}-{friday.day}"
    return (f"{MONTHS[monday.month - 1][:3]} {monday.day}-"
            f"{MONTHS[friday.month - 1][:3]} {friday.day}")


def month_range(day: Date | str) -> tuple[str, str, str]:
    """The calendar month containing `day`, and its name."""
    day = Date.fromisoformat(day) if isinstance(day, str) else day
    last = calendar.monthrange(day.year, day.month)[1]
    return (day.replace(day=1).isoformat(),
            day.replace(day=last).isoformat(),
            MONTHS[day.month - 1])


# --- counting ---------------------------------------------------------------

def stats_for(conn: sqlite3.Connection, start: str, end: str, *,
              section_id: int | None = None) -> dict[int, PeriodStats]:
    """Attendance counts per student over a range. Live rows only.

    superseded_by IS NULL so a corrected day is counted once, at its corrected value --
    a parent must never be told about an absence an adviser has already excused.
    """
    sql = ["""SELECT a.student_id AS sid,
                     SUM(a.status = 'present') AS present,
                     SUM(a.status = 'late')    AS late,
                     SUM(a.status = 'absent')  AS absent
              FROM attendance_days a
              JOIN students s ON s.id = a.student_id
              WHERE a.superseded_by IS NULL AND s.active = 1
                AND a.date BETWEEN ? AND ?"""]
    params: list = [start, end]
    if section_id is not None:
        sql.append("AND s.section_id = ?")
        params.append(section_id)
    sql.append("GROUP BY a.student_id")

    return {
        row["sid"]: PeriodStats(
            student_id=row["sid"], present=row["present"] or 0,
            late=row["late"] or 0, absent=row["absent"] or 0,
        )
        for row in conn.execute(" ".join(sql), params)
    }


def absences_in_month(conn: sqlite3.Connection, student_id: int, day: str) -> int:
    """How many absences this student has in `day`'s calendar month."""
    start, end, _ = month_range(day)
    return conn.execute(
        """SELECT COUNT(*) FROM attendance_days
           WHERE student_id = ? AND superseded_by IS NULL
             AND status = 'absent' AND date BETWEEN ? AND ?""",
        (student_id, start, end),
    ).fetchone()[0]


# --- the weekly summary -----------------------------------------------------

def weekly_summaries(conn: sqlite3.Connection, config: Config, *,
                     week_of: Date | str | None = None,
                     section_id: int | None = None,
                     dry_run: bool = False) -> SummaryRun:
    """Queue one summary per consenting guardian for the school week.

    dry_run counts without writing, so the confirmation dialog can say how many texts
    are about to leave before anybody agrees to it.

    Idempotent by week: dedupe_extra carries the week label, so pressing the button
    twice queues nothing the second time rather than texting every parent again.
    """
    start, end, label = school_week(week_of or Date.today())
    run = SummaryRun(period=label, start=start, end=end)
    counts = stats_for(conn, start, end, section_id=section_id)

    for student_id, stat in sorted(counts.items()):
        if not stat.days:
            # Nothing recorded -- a suspended week, or a student enrolled mid-week.
            # A summary reading "0 of 0 days" tells a parent nothing.
            run.note("no attendance recorded")
            continue
        run.eligible += 1
        if dry_run:
            continue

        result = queue.enqueue(
            conn, student_id, Trigger.SUMMARY,
            datetime.fromisoformat(f"{end}T16:00:00"), config,
            dedupe_extra=f"week:{start}",
            extra={"period": label, "present": stat.present, "late": stat.late,
                   "absent": stat.absent, "days": stat.days},
        )
        if result.queued:
            run.queued += 1
        else:
            run.note(result.reason)
    return run


# --- the absence reminder ---------------------------------------------------

def absence_reminder(conn: sqlite3.Connection, config: Config, student_id: int,
                     day: str, at: datetime) -> queue.EnqueueResult | None:
    """Warn a guardian when a student reaches the month's absence threshold.

    Fires at exactly two counts and no others: the warning level, and the limit. Past
    the limit it goes quiet -- a text on every further absence is nagging, and the
    conversation has already moved to a person by then.

    Returns None when no threshold was crossed, so the caller can tell "nothing to say"
    from "tried and was refused".
    """
    policy = config.notifications
    if not policy.absence_reminders:
        return None

    count = absences_in_month(conn, student_id, day)
    limit, warn_at = policy.monthly_absence_limit, policy.absence_warn_at
    if count not in (warn_at, limit):
        return None

    _, _, month = month_range(day)
    remaining = limit - count
    if remaining > 0:
        clause = (f"{remaining} more is allowed this month."
                  if remaining == 1 else f"{remaining} more are allowed this month.")
    else:
        clause = f"That is the month's limit of {limit}."

    # The count is in the key, so each threshold texts once and a re-run of the
    # end-of-day job cannot repeat it.
    return queue.enqueue(
        conn, student_id, Trigger.REMINDER, at, config,
        dedupe_extra=f"absences:{month}:{count}",
        extra={"period": month, "absent": count, "clause": clause},
    )
