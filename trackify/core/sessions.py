"""School-day resolution: which thresholds apply on a given date.

A row in school_days is the authority for that date. When none exists the config
defaults are used and a row is created, so the thresholds a record was judged
against are preserved even if config.toml is edited later. Without this, changing
`late_threshold` mid-study would retroactively reclassify past attendance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time

from .config import Config, _parse_time
from .db import utcnow


@dataclass(frozen=True)
class SchoolDay:
    date: str
    is_school_day: bool
    suspension_reason: str | None
    entry_open: time
    late_threshold: time
    dismissal_time: time
    early_departure_cutoff: time


def _row_to_day(row: sqlite3.Row) -> SchoolDay:
    return SchoolDay(
        date=row["date"],
        is_school_day=bool(row["is_school_day"]),
        suspension_reason=row["suspension_reason"],
        entry_open=_parse_time(row["entry_open"]),
        late_threshold=_parse_time(row["late_threshold"]),
        dismissal_time=_parse_time(row["dismissal_time"]),
        early_departure_cutoff=_parse_time(row["early_departure_cutoff"]),
    )


def get_school_day(conn: sqlite3.Connection, day: Date | str, config: Config) -> SchoolDay:
    """Fetch the day's thresholds, creating them from config on first use."""
    key = day if isinstance(day, str) else day.isoformat()
    row = conn.execute("SELECT * FROM school_days WHERE date = ?", (key,)).fetchone()
    if row is not None:
        return _row_to_day(row)

    s = config.school
    conn.execute(
        """INSERT INTO school_days
           (date, is_school_day, entry_open, late_threshold,
            dismissal_time, early_departure_cutoff)
           VALUES (?, 1, ?, ?, ?, ?)""",
        (key, s.entry_open.isoformat(timespec="minutes"),
         s.late_threshold.isoformat(timespec="minutes"),
         s.dismissal_time.isoformat(timespec="minutes"),
         s.early_departure_cutoff.isoformat(timespec="minutes")),
    )
    return SchoolDay(
        date=key,
        is_school_day=True,
        suspension_reason=None,
        entry_open=s.entry_open,
        late_threshold=s.late_threshold,
        dismissal_time=s.dismissal_time,
        early_departure_cutoff=s.early_departure_cutoff,
    )


def suspend_day(
    conn: sqlite3.Connection, day: Date | str, reason: str, config: Config
) -> None:
    """Mark a date as not a school day. Removes it from every attendance denominator."""
    get_school_day(conn, day, config)  # ensure the row exists
    key = day if isinstance(day, str) else day.isoformat()
    conn.execute(
        "UPDATE school_days SET is_school_day = 0, suspension_reason = ? WHERE date = ?",
        (reason, key),
    )


def is_late(day: SchoolDay, at: datetime) -> bool:
    return at.time() > day.late_threshold


def is_early_departure(day: SchoolDay, at: datetime) -> bool:
    return at.time() < day.early_departure_cutoff


def is_out_of_window(day: SchoolDay, at: datetime) -> bool:
    """Before the gate opens, or after a generous grace past dismissal."""
    return at.time() < day.entry_open
