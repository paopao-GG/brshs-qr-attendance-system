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


@dataclass(frozen=True)
class ScanResult:
    outcome: Outcome
    student_id: int
    at: datetime
    direction: str | None = None
    status: str | None = None
    flags: tuple[str, ...] = ()
    scan_id: int | None = None
    triggers: tuple[Trigger, ...] = ()
    message: str = ""
    previous_at: datetime | None = None

    @property
    def recorded(self) -> bool:
        return self.outcome in (Outcome.RECORDED_IN, Outcome.RECORDED_OUT)


def fmt_time(at: datetime) -> str:
    """12-hour time without a leading zero. %-I is not portable to Windows."""
    text = at.strftime("%I:%M %p")
    return text[1:] if text.startswith("0") else text


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

    flags: list[str] = []
    if method == "manual":
        flags.append("manual_entry")
    if is_out_of_window(day, at):
        flags.append("out_of_window")

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
        status = "late" if is_late(day, at) else "present"
        triggers.append(Trigger.LATE if status == "late" else Trigger.ARRIVAL)
        _upsert_entry(conn, student_id, day_key, scan_id, status, flags)
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
    conn: sqlite3.Connection, student_id: int, day: str,
    scan_id: int, status: str, flags: list[str],
) -> None:
    row = conn.execute(
        """SELECT id, flags FROM attendance_days
           WHERE student_id = ? AND date = ? AND superseded_by IS NULL""",
        (student_id, day),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO attendance_days
               (student_id, date, entry_scan_id, status, flags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student_id, day, scan_id, status, ",".join(flags), utcnow()),
        )
    else:
        conn.execute(
            "UPDATE attendance_days SET entry_scan_id = ?, status = ?, flags = ? WHERE id = ?",
            (scan_id, status, _merge_flags(row["flags"], flags), row["id"]),
        )


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
            (student_id, day, scan_id, _merge_flags("", flags + ["entry_missing"]), utcnow()),
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


def close_open_days(
    conn: sqlite3.Connection, day: str, config: Config
) -> tuple[int, int]:
    """End-of-day job. Returns (marked_absent, flagged_exit_missing).

    Students with no scan at all are marked absent and an absence notification is
    queued by the caller. Students who arrived but never scanned out get the
    exit_missing flag and NO guardian notification -- "no departure recorded for
    your child" reads as a missing-child alert and causes panic. It is an adviser
    follow-up, not a parent message.
    """
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

    return len(absent_rows), len(missing)
