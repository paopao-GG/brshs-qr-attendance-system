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
from .attendance import (
    DayClose,
    Outcome,
    ScanResult,
    Trigger,
    close_open_days,
    fmt_time,
    record_scan,
)
from .config import Config
from .db import audit, transaction
from . import screening
from .screening import Outcome as Outcome_
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
    student_id: int | None = None
    # The arming scan. The screening panel needs it because a screening binds to a
    # scan and to nothing else (flow.md Rule 2); None means there is nothing to screen.
    scan_id: int | None = None
    section: str = ""
    adviser: str = ""
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

    def student_row(self, lrn: str) -> sqlite3.Row | None:
        """Resolve a scanned card to its student.

        Keyed on the LRN, NOT on students.id. A printed card is a physical object that
        outlives any particular database: keying it on an autoincrement row number means
        a reseed silently invalidates every card already handed out. The LRN follows the
        learner for life, so a card keyed on it stays valid across a rebuild.

        Callers must use the returned row's own `id` for anything that writes -- every
        downstream foreign key points at students(id), not at the LRN.
        """
        # LEFT JOIN on the adviser: a section between advisers must not make a student
        # unscannable. flow.md 3 step 5 wants the adviser on screen for identity
        # confirmation, but not at the price of the gate.
        return self.conn.execute(
            """SELECT s.*, sec.name AS section_name, sec.grade_level,
                      u.full_name AS adviser_name
               FROM students s
               JOIN sections sec ON sec.id = s.section_id
               LEFT JOIN users u ON u.id = sec.adviser_id
               WHERE s.lrn = ? AND s.active = 1""",
            (str(lrn),),
        ).fetchone()

    def school_day(self, day: str | None = None):
        """The configured windows for a date, creating the row on first use."""
        day = day or datetime.now().date().isoformat()
        return get_school_day(self.conn, day, self.config)

    def session_label(self, at: datetime | None = None) -> str:
        at = at or datetime.now()
        day = self.school_day(at.date().isoformat())
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
            # decode() verifies the signature and hands back the number the card carries,
            # which is the student's LRN. str(int(...)) is its exact inverse: the payload
            # was built by encode(int(lrn)), so a stored LRN that does not survive that
            # round trip -- one with a leading zero -- could never have produced a
            # matching signature in the first place. roster.lrn_note() flags those at
            # import so the failure is visible there rather than at the gate.
            lrn = str(qrcodes.decode(payload, self.config.secrets.qr_secret))
        except qrcodes.InvalidQRCode:
            return ScanPresentation(
                state=Presentation.UNKNOWN_CODE,
                headline="Code not recognised",
                detail="Please see the guard",
                hold_ms=5000,
            )

        student = self.student_row(lrn)
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
            # The row's own id, never the LRN: scan_events.student_id and every table
            # downstream of it is a foreign key to students(id).
            result: ScanResult = record_scan(
                self.conn, student["id"], at, self.config,
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

    def _adviser(self, student: sqlite3.Row) -> str:
        adviser = student["adviser_name"]
        return f"Adviser: {adviser}" if adviser else ""

    # -- end of day ---------------------------------------------------------

    def close_day(
        self, day: str | None = None, *, at: datetime | None = None
    ) -> DayClose:
        """Mark absences, flag missing out-scans, queue the absence notifications.

        Safe to call repeatedly. Two independent guards make it idempotent: the
        absent-row query skips students who already have an attendance_days row for
        the date, and idempotency_key turns a repeated enqueue into a no-op.

        Notifications are queued, never sent -- the same rule as a scan. Nothing here
        touches the modem.
        """
        at = at or datetime.now()
        day = day or at.date().isoformat()

        # The notification belongs to the day being closed, not to the moment the job
        # runs. Closing a past day must not stamp today's date into the parent's text
        # or into the idempotency key.
        event_at = at if day == at.date().isoformat() else datetime.fromisoformat(
            f"{day}T23:59:00"
        )

        with transaction(self.conn):
            result = close_open_days(self.conn, day, self.config)
            for student_id in result.absent_ids:
                queue.enqueue(
                    self.conn, student_id, Trigger.ABSENT, event_at, self.config
                )
        return result

    # -- screening (docs/prohibited-items.md) --------------------------------

    def record_screening(
        self,
        scan_id: int,
        outcome: Outcome_,
        *,
        metal_detected: bool = False,
        declared_items: str | None = None,
        override_reason: str | None = None,
        notes: str | None = None,
        operator_id: int | None = None,
        at: datetime | None = None,
    ) -> int:
        """Record what the operator concluded about one scan. Returns the row id.

        Attendance is already committed before this is ever called -- flow.md 3 step 6:
        the screening outcome must never affect whether attendance was recorded. This
        method therefore cannot touch attendance_days or scan_events at all.

        Re-recording an outcome for the same scan REPLACES it, because the realistic
        sequence is a guard pressing Clear and then finding something: the correction
        must win. The replacement is audited; the original outcome is in the audit row.
        """
        at = at or datetime.now()
        if override_reason is None and outcome is Outcome_.OVERRIDDEN:
            raise ValueError("an overridden screening requires a reason")

        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT id, outcome FROM screening_events WHERE scan_event_id = ?",
                (scan_id,),
            ).fetchone()

            if existing is None:
                cur = self.conn.execute(
                    """INSERT INTO screening_events
                       (scan_event_id, occurred_at, metal_detected, outcome,
                        declared_items, override_reason, notes, operator_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (scan_id, at.isoformat(timespec="seconds"), int(metal_detected),
                     outcome.value, declared_items, override_reason, notes, operator_id),
                )
                return cur.lastrowid

            self.conn.execute(
                """UPDATE screening_events
                   SET occurred_at = ?, metal_detected = ?, outcome = ?,
                       declared_items = ?, override_reason = ?, notes = ?, operator_id = ?
                   WHERE id = ?""",
                (at.isoformat(timespec="seconds"), int(metal_detected), outcome.value,
                 declared_items, override_reason, notes, operator_id, existing["id"]),
            )
            audit(
                self.conn, "screening.amended",
                actor_id=operator_id, entity_type="screening_events",
                entity_id=existing["id"],
                old_value=existing["outcome"], new_value=outcome.value,
            )
            return existing["id"]

    def record_incident(
        self,
        screening_event_id: int,
        student_id: int,
        category: str,
        item_description: str,
        *,
        severity: int | None = None,
        severity_reason: str | None = None,
        notes: str | None = None,
        confirmed_by: int | None = None,
        at: datetime | None = None,
    ) -> int:
        """A guard-confirmed prohibited item. Returns the incident id.

        This is the ONLY path by which anything about a prohibited item is attached to
        a named minor -- flow.md Rule 1. It requires a screening event, which in turn
        requires the arming scan, so an incident can never be attributed by guesswork.

        Writes the audit row and queues the guardian notification in the same
        transaction as the incident: an incident recorded but never notified, or
        notified but never recorded, are both worse than either alone.
        """
        at = at or datetime.now()
        severity = severity if severity is not None else screening.default_severity(category)
        screening.validate_incident(category, item_description, severity, severity_reason)

        with transaction(self.conn):
            cur = self.conn.execute(
                """INSERT INTO incidents
                   (student_id, screening_event_id, occurred_at, category,
                    item_description, severity, severity_reason, notes, confirmed_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (student_id, screening_event_id, at.isoformat(timespec="seconds"),
                 category, item_description.strip(), severity, severity_reason,
                 notes, confirmed_by),
            )
            incident_id = cur.lastrowid

            audit(
                self.conn, "incident.recorded",
                actor_id=confirmed_by, entity_type="incidents", entity_id=incident_id,
                new_value=f"{category} severity {severity}",
                reason=item_description.strip(),
            )

            # dedupe_extra keeps two incidents on the same day distinct. Without it the
            # second would be swallowed as a duplicate of the first and no one would be
            # told about it.
            queue.enqueue(
                self.conn, student_id, Trigger.INCIDENT, at, self.config,
                dedupe_extra=f"se{screening_event_id}",
            )
        return incident_id

    def unresolved_screenings(self, day: str | None = None) -> list[sqlite3.Row]:
        """Screenings nobody finished. Surfaced the way unsent SMS already are.

        pending_verification is an unfinished inspection, not a finding -- if these are
        never shown to anyone they quietly accumulate and then appear in the counts as
        though they meant something.
        """
        day = day or datetime.now().date().isoformat()
        return self.conn.execute(
            """SELECT sc.*, s.first_name, s.last_name, e.scanned_at
               FROM screening_events sc
               JOIN scan_events e ON e.id = sc.scan_event_id
               JOIN students s    ON s.id = e.student_id
               WHERE e.date = ? AND sc.outcome IN ('pending_verification', 'not_screened')
               ORDER BY e.scanned_at""",
            (day,),
        ).fetchall()

    def screening_coverage(self, day: str | None = None) -> dict[str, int]:
        """Counts by outcome for one day, plus the scans nobody recorded at all.

        `unrecorded` is not the same as not_screened: one is a scan the guard never
        answered for, the other is an answer of 'nobody screened this student'. Both
        are absences of screening and both belong in the coverage denominator.
        """
        counts = {
            row["outcome"]: row["n"]
            for row in self.conn.execute(
                """SELECT sc.outcome, COUNT(*) AS n
                   FROM screening_events sc
                   JOIN scan_events e ON e.id = sc.scan_event_id
                   WHERE e.date = ? GROUP BY sc.outcome""",
                (day or datetime.now().date().isoformat(),),
            )
        }
        counts["unrecorded"] = self.conn.execute(
            """SELECT COUNT(*) FROM scan_events e
               WHERE e.date = ? AND e.direction = 'in'
                 AND NOT EXISTS (SELECT 1 FROM screening_events sc
                                 WHERE sc.scan_event_id = e.id)""",
            (day or datetime.now().date().isoformat(),),
        ).fetchone()[0]
        return counts

    # -- mapping domain outcome to screen state -----------------------------

    def _present(
        self, result: ScanResult, name: str, section: str, initials: str,
        student: sqlite3.Row, queued: int,
    ) -> ScanPresentation:
        common = {
            "student_name": name,
            "student_id": result.student_id,
            "scan_id": result.scan_id,
            "section": section,
            "adviser": self._adviser(student),
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
