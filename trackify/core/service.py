"""ScanService -- the seam between the UI and the domain.

The kiosk calls exactly one method and gets back everything the screen needs. It never
orchestrates transactions, never touches the notification queue, and never decides what
a scan means. That keeps the UI thin and the domain testable without Qt.

This is also where permissions belong when roles arrive: access control lives in the
service layer, not in hidden menu items.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..notify import queue
from . import qrcodes
from .attendance import Outcome, ScanResult, fmt_time, record_scan
from .config import Config
from .db import transaction
from .sessions import get_school_day


class Presentation(str, Enum):
    """What the kiosk should show. Deliberately smaller than Outcome -- the screen
    does not need to distinguish every domain case, only every visual state."""

    IN = "in"
    OUT = "out"
    ALREADY = "already"
    UNKNOWN_CODE = "unknown_code"
    MISFIRE = "misfire"
    NEEDS_OVERRIDE = "needs_override"
    NO_CLASSES = "no_classes"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class ScanPresentation:
    state: Presentation
    headline: str = ""
    detail: str = ""
    student_name: str = ""
    section: str = ""
    initials: str = ""
    photo_path: str | None = None
    time_text: str = ""
    notifications_queued: int = 0
    hold_ms: int = 3000

    @property
    def is_success(self) -> bool:
        return self.state in (Presentation.IN, Presentation.OUT)


def _initials(first: str, last: str) -> str:
    return (first[:1] + last[:1]).upper()


class ScanService:
    """One call from a scanned payload to something the screen can render."""

    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config

    # -- lookups ------------------------------------------------------------

    def student_row(self, student_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT s.*, sec.name AS section_name, sec.grade_level
               FROM students s JOIN sections sec ON sec.id = s.section_id
               WHERE s.id = ? AND s.active = 1""",
            (student_id,),
        ).fetchone()

    def session_label(self, at: datetime | None = None) -> str:
        at = at or datetime.now()
        day = get_school_day(self.conn, at.date().isoformat(), self.config)
        if not day.is_school_day:
            return day.suspension_reason or "No classes"
        return f"Gate {day.entry_open:%H:%M} - late after {day.late_threshold:%H:%M}"

    # -- the one call the kiosk makes ---------------------------------------

    def handle_scan(
        self,
        payload: str,
        *,
        at: datetime | None = None,
        operator_id: int | None = None,
        override_reason: str | None = None,
    ) -> ScanPresentation:
        at = at or datetime.now()

        # Distinguish a scanner misfire from a genuinely bad code: they deserve
        # different messages, because one is the operator's problem and the other
        # is the student's.
        if not qrcodes.is_wellformed(payload):
            return ScanPresentation(
                state=Presentation.MISFIRE,
                headline="Scan not read",
                detail="Try again, holding the code steady",
                hold_ms=2500,
            )

        try:
            student_id = qrcodes.decode(payload, self.config.secrets.qr_secret)
        except qrcodes.InvalidQRCode:
            return ScanPresentation(
                state=Presentation.UNKNOWN_CODE,
                headline="Code not recognised",
                detail="Please see the guard",
                hold_ms=5000,
            )

        student = self.student_row(student_id)
        if student is None:
            return ScanPresentation(
                state=Presentation.UNKNOWN_CODE,
                headline="Student not found",
                detail="This ID is not on the active roster",
                hold_ms=5000,
            )

        name = f"{student['first_name']} {student['last_name']}"
        section = f"{student['grade_level']}-{student['section_name']}"
        initials = _initials(student["first_name"], student["last_name"])

        with transaction(self.conn):
            result: ScanResult = record_scan(
                self.conn, student_id, at, self.config,
                operator_id=operator_id,
                raw_payload=payload,
                override_reason=override_reason,
            )
            queued = 0
            if result.recorded:
                queued = sum(
                    1 for r in queue.enqueue_for_scan(self.conn, result, self.config)
                    if r.queued
                )

        return self._present(result, name, section, initials, student, queued)

    # -- mapping domain outcome to screen state -----------------------------

    def _present(
        self, result: ScanResult, name: str, section: str, initials: str,
        student: sqlite3.Row, queued: int,
    ) -> ScanPresentation:
        common = {
            "student_name": name,
            "section": section,
            "initials": initials,
            "photo_path": student["photo_path"],
            "notifications_queued": queued,
        }

        if result.outcome is Outcome.RECORDED_IN:
            late = result.status == "late"
            return ScanPresentation(
                state=Presentation.IN,
                headline="IN",
                detail="Arrived late" if late else "Welcome",
                time_text=fmt_time(result.at),
                hold_ms=3000,
                **common,
            )

        if result.outcome is Outcome.RECORDED_OUT:
            early = "early_departure" in result.flags
            return ScanPresentation(
                state=Presentation.OUT,
                headline="OUT",
                detail="Early departure" if early else "Goodbye",
                time_text=fmt_time(result.at),
                hold_ms=3000,
                **common,
            )

        if result.outcome is Outcome.DEBOUNCED:
            return ScanPresentation(
                state=Presentation.ALREADY,
                headline="Already recorded",
                detail=result.message.replace("Already recorded at ", "at "),
                hold_ms=4000,
                **common,
            )

        if result.outcome is Outcome.NEEDS_OVERRIDE:
            return ScanPresentation(
                state=Presentation.NEEDS_OVERRIDE,
                headline="Already departed",
                detail="Supervisor override required",
                hold_ms=6000,
                **common,
            )

        if result.outcome is Outcome.SCAN_CAP_REACHED:
            return ScanPresentation(
                state=Presentation.NEEDS_OVERRIDE,
                headline="Scan limit reached",
                detail="Supervisor override required",
                hold_ms=6000,
                **common,
            )

        return ScanPresentation(
            state=Presentation.NO_CLASSES,
            headline="No classes today",
            detail=result.message,
            hold_ms=4000,
            **common,
        )

    @staticmethod
    def rate_limited() -> ScanPresentation:
        """Returned by the UI when the input token bucket rejects a scan."""
        return ScanPresentation(
            state=Presentation.RATE_LIMITED,
            headline="Scanning too fast",
            detail="Wait a moment and scan again",
            hold_ms=2000,
        )
