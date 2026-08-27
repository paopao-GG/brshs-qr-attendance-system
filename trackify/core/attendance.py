"""The attendance engine: direction state machine, debounce, derived status.

Per student per day:

    not_arrived --(scan)--> present --(scan)--> departed --(scan)--> needs override

Debounce is deliberately keyed on the last scan of ANY direction, not per direction.
The dangerous case is a student scanning in and re-tapping seconds later because they
were not sure it registered: with a per-direction debounce that second tap records a
departure and texts the guardian "left school" at 7am.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .config import Config
from .db import utcnow
from .sessions import (
    get_school_day,
    is_early_departure,
    is_late,
    is_out_of_window,
)


class Outcome(str, Enum):
    RECORDED_IN = "recorded_in"
    RECORDED_OUT = "recorded_out"
    DEBOUNCED = "debounced"
    NEEDS_OVERRIDE = "needs_override"
    NOT_A_SCHOOL_DAY = "not_a_school_day"
    SCAN_CAP_REACHED = "scan_cap_reached"


class Trigger(str, Enum):
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    LATE = "late"
    ABSENT = "absent"
    INCIDENT = "incident"
    # Periodic, not event-driven. SUMMARY is the weekly attendance recap; REMINDER is
    # the monthly absence-limit warning. Both are in notifications.trigger's CHECK --
    # see db.NOTIFICATION_TRIGGERS.
    SUMMARY = "summary"
    REMINDER = "reminder"


@dataclass(frozen=True)
class ScanResult:
    outcome: Outcome
    student_id: int
    at: datetime
    direction: str | None = None
    status: str | None = None
    flags: tuple[str, ...] = ()
    scan_id: int | None = None
    # An authorised return after the student had already departed. Carried so the queue
    # can tell a re-entry apart from the day's first arrival -- they share an
    # idempotency key otherwise.
    reentry: bool = False
    triggers: tuple[Trigger, ...] = ()
    message: str = ""
    previous_at: datetime | None = None

    @property
    def recorded(self) -> bool:
        return self.outcome in (Outcome.RECORDED_IN, Outcome.RECORDED_OUT)


def fmt_time(at: datetime) -> str:
    """12-hour time without a leading zero, e.g. "7:00 AM".

    Assembled from the fields rather than strftime, because neither half of that format
    is portable. %-I strips the leading zero on Linux and is rejected on Windows -- and
    %p is not portable across LOCALES, which is the one that actually bit us.

    Qt calls setlocale(LC_ALL, "") when QApplication is constructed, so the process
    leaves the C locale the moment the kiosk starts. Under the Pi's default en_GB the
    glibc am_pm strings are lower case, and the same code that printed "arrived 3:00 PM"
    on Windows started printing "arrived 3:00 pm" -- inside a text message to a parent.
    Other locales define am_pm as empty, which would drop the meridiem from an arrival
    time altogether and leave "arrived 3:00" genuinely ambiguous.

    The wording of a guardian notification must not depend on $LANG.
    """
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def _last_scan(conn: sqlite3.Connection, student_id: int, day: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM scan_events
           WHERE student_id = ? AND date = ?
           ORDER BY scanned_at DESC, id DESC LIMIT 1""",
        (student_id, day),
    ).fetchone()


def _scan_count(conn: sqlite3.Connection, student_id: int, day: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM scan_events WHERE student_id = ? AND date = ?",
        (student_id, day),
    ).fetchone()[0]


def record_scan(
    conn: sqlite3.Connection,
    student_id: int,
    at: datetime,
    config: Config,
    *,
    method: str = "scan",
    operator_id: int | None = None,
    raw_payload: str | None = None,
    override_reason: str | None = None,
) -> ScanResult:
    """Apply one scan to the state machine and persist the result."""
    day_key = at.date().isoformat()
    day = get_school_day(conn, day_key, config)

    if not day.is_school_day and override_reason is None:
        return ScanResult(
            outcome=Outcome.NOT_A_SCHOOL_DAY,
            student_id=student_id,
            at=at,
            message=f"No classes today ({day.suspension_reason or 'suspended'})",
        )

    last = _last_scan(conn, student_id, day_key)

    # --- Debounce: any direction, configurable window -----------------------
    if last is not None:
        previous_at = datetime.fromisoformat(last["scanned_at"])
        if at - previous_at < timedelta(minutes=config.scanning.debounce_minutes):
            return ScanResult(
                outcome=Outcome.DEBOUNCED,
                student_id=student_id,
                at=at,
                direction=last["direction"],
                previous_at=previous_at,
                message=f"Already recorded at {fmt_time(previous_at)}",
            )

    # --- Hard per-day cap ---------------------------------------------------
    if (_scan_count(conn, student_id, day_key) >= config.scanning.max_scans_per_day
            and override_reason is None):
        return ScanResult(
            outcome=Outcome.SCAN_CAP_REACHED,
            student_id=student_id,
            at=at,
            message="Daily scan limit reached - supervisor override required",
        )

    # --- Direction from state -----------------------------------------------
    reentry = False
    if last is None:
        direction = "in"
    elif last["direction"] == "in":
        direction = "out"
    else:
        if override_reason is None:
            return ScanResult(
                outcome=Outcome.NEEDS_OVERRIDE,
                student_id=student_id,
                at=at,
                previous_at=datetime.fromisoformat(last["scanned_at"]),
                message="Already departed today - supervisor override required",
            )
        direction = "in"  # re-entry, explicitly authorised
        reentry = True

    flags: list[str] = []
    if method == "manual":
        flags.append("manual_entry")
    if is_out_of_window(day, at):
        flags.append("out_of_window")
    if reentry:
        flags.append("re_entry")

    cursor = conn.execute(
        """INSERT INTO scan_events
           (student_id, scanned_at, date, direction, method, raw_payload,
            operator_id, override_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (student_id, at.isoformat(timespec="seconds"), day_key, direction,
         method, raw_payload, operator_id, override_reason),
    )
    scan_id = cursor.lastrowid

    triggers: list[Trigger] = []
    if direction == "in":
        status = _upsert_entry(conn, student_id, day_key, scan_id, day, at, flags)
        # A re-entry is an ARRIVAL, never a LATE one. The day was classified by the
        # first scan and _upsert_entry preserved it; emitting LATE here would text the
        # guardian "arrived late at 3:00 PM" about a student who was at the gate before
        # seven -- the same failure the debounce rule at the top of this file exists to
        # prevent, one branch over.
        triggers.append(Trigger.ARRIVAL if reentry or status != "late"
                        else Trigger.LATE)
        outcome = Outcome.RECORDED_IN
    else:
        if is_early_departure(day, at):
            flags.append("early_departure")
        status = _close_out(conn, student_id, day_key, scan_id, flags, at)
        triggers.append(Trigger.DEPARTURE)
        outcome = Outcome.RECORDED_OUT

    return ScanResult(
        outcome=outcome,
        student_id=student_id,
        at=at,
        direction=direction,
        status=status,
        flags=tuple(flags),
        scan_id=scan_id,
        reentry=reentry,
        triggers=tuple(triggers),
        message=f"{'IN' if direction == 'in' else 'OUT'} {fmt_time(at)}",
    )


def _merge_flags(existing: str, new: list[str]) -> str:
    combined = [f for f in (existing or "").split(",") if f]
    for flag in new:
        if flag not in combined:
            combined.append(flag)
    return ",".join(combined)


def _upsert_entry(
    conn: sqlite3.Connection, student_id: int, day: str, scan_id: int,
    school_day, at: datetime, flags: list[str],
) -> str:
    """Open the day's attendance row, or fold a re-entry into the existing one.

    Returns the status now in force.

    LATENESS IS DECIDED ONCE, BY THE DAY'S FIRST ARRIVAL. On a re-entry the stored
    status is preserved and handed straight back. Recomputing it against the clock --
    which is what this used to do -- rewrote an authorised 3pm return as 'late' for a
    student who had scanned in at 06:50, and queued the guardian a text saying so.

    entry_scan_id is likewise left pointing at the FIRST entry, because _close_out
    measures minutes_on_campus from it; repointing it would report a 40-minute school
    day. A row whose entry_scan_id is NULL is the exception -- an out-scan opened it,
    so this really is the first entry and it is classified fresh.
    """
    row = conn.execute(
        """SELECT id, flags, status, entry_scan_id FROM attendance_days
           WHERE student_id = ? AND date = ? AND superseded_by IS NULL""",
        (student_id, day),
    ).fetchone()

    if row is None:
        status = "late" if is_late(school_day, at) else "present"
        conn.execute(
            """INSERT INTO attendance_days
               (student_id, date, entry_scan_id, status, flags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student_id, day, scan_id, status, ",".join(flags), utcnow()),
        )
        return status

    if row["entry_scan_id"] is None:
        status = "late" if is_late(school_day, at) else "present"
        conn.execute(
            """UPDATE attendance_days
               SET entry_scan_id = ?, status = ?, flags = ? WHERE id = ?""",
            (scan_id, status, _merge_flags(row["flags"], flags), row["id"]),
        )
        return status

    conn.execute(
        "UPDATE attendance_days SET flags = ? WHERE id = ?",
        (_merge_flags(row["flags"], flags), row["id"]),
    )
    return row["status"]


def _close_out(
    conn: sqlite3.Connection, student_id: int, day: str,
    scan_id: int, flags: list[str], at: datetime,
) -> str:
    row = conn.execute(
        """SELECT a.id, a.status, a.flags, s.scanned_at AS entry_at
           FROM attendance_days a
           LEFT JOIN scan_events s ON s.id = a.entry_scan_id
           WHERE a.student_id = ? AND a.date = ? AND a.superseded_by IS NULL""",
        (student_id, day),
    ).fetchone()

    if row is None:
        # Out-scan with no recorded entry. Should be rare; flag it rather than
        # inventing an arrival time.
        conn.execute(
            """INSERT INTO attendance_days
               (student_id, date, exit_scan_id, status, flags, created_at)
               VALUES (?, ?, ?, 'present', ?, ?)""",
            (student_id, day, scan_id, _merge_flags("", [*flags, "entry_missing"]), utcnow()),
        )
        return "present"

    minutes = None
    if row["entry_at"]:
        minutes = int((at - datetime.fromisoformat(row["entry_at"])).total_seconds() // 60)

    conn.execute(
        """UPDATE attendance_days
           SET exit_scan_id = ?, flags = ?, minutes_on_campus = ? WHERE id = ?""",
        (scan_id, _merge_flags(row["flags"], flags), minutes, row["id"]),
    )
    return row["status"]


@dataclass(frozen=True)
class DayClose:
    """Result of the end-of-day job.

    Absent students are returned by id rather than counted, because the caller has to
    queue one notification per student and a count cannot do that.
    """

    absent_ids: tuple[int, ...] = ()
    exit_missing: int = 0
    skipped: str = ""          # non-empty when the day was not a school day

    @property
    def absent(self) -> int:
        return len(self.absent_ids)


def close_open_days(
    conn: sqlite3.Connection, day: str, config: Config
) -> DayClose:
    """End-of-day job. Marks absences and flags missing out-scans.

    Students with no scan at all are marked absent and an absence notification is
    queued by the caller. Students who arrived but never scanned out get the
    exit_missing flag and NO guardian notification -- "no departure recorded for
    your child" reads as a missing-child alert and causes panic. It is an adviser
    follow-up, not a parent message.

    A suspended day is refused outright. Without that check, closing a day with no
    classes marks the ENTIRE roster absent and texts every guardian -- the single
    worst thing this job could do, and the reason the day is removed from every
    attendance denominator in the first place.
    """
    school_day = get_school_day(conn, day, config)
    if not school_day.is_school_day:
        return DayClose(skipped=school_day.suspension_reason or "not a school day")

    absent_rows = conn.execute(
        """SELECT s.id FROM students s
           WHERE s.active = 1
             AND NOT EXISTS (SELECT 1 FROM scan_events e
                             WHERE e.student_id = s.id AND e.date = ?)
             AND NOT EXISTS (SELECT 1 FROM attendance_days a
                             WHERE a.student_id = s.id AND a.date = ?
                               AND a.superseded_by IS NULL)""",
        (day, day),
    ).fetchall()

    for row in absent_rows:
        conn.execute(
            """INSERT INTO attendance_days
               (student_id, date, status, flags, created_at)
               VALUES (?, ?, 'absent', 'derived', ?)""",
            (row["id"], day, utcnow()),
        )

    missing = conn.execute(
        """SELECT id, flags FROM attendance_days
           WHERE date = ? AND superseded_by IS NULL
             AND entry_scan_id IS NOT NULL AND exit_scan_id IS NULL""",
        (day,),
    ).fetchall()

    for row in missing:
        conn.execute(
            "UPDATE attendance_days SET flags = ? WHERE id = ?",
            (_merge_flags(row["flags"], ["exit_missing"]), row["id"]),
        )

    return DayClose(
        absent_ids=tuple(row["id"] for row in absent_rows),
        exit_missing=len(missing),
    )
