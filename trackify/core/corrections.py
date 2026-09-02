"""Attendance corrections and the section register.

The rule this module exists to enforce, from docs/flow.md 4.2:

    Original records are NEVER overwritten. A correction is a new row that supersedes
    the original; both remain, and the audit log preserves the chain.

That is what protects the Phase III comparison against manually recorded attendance.
If a correction edited the original row in place, "what did the system record" and
"what did a human decide afterwards" would be the same value and the comparison would
be meaningless.

Qt-free, like the rest of core/.
"""

from __future__ import annotations

import sqlite3
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from enum import Enum

from .attendance import _merge_flags
from .db import audit, transaction, utcnow

# What the register shows in a cell, and what the XLSX legend explains.
LETTERS = {
    "present": "P",
    "late": "L",
    "absent": "A",
    "excused": "E",
    "online": "O",
}

# Counted as attendance in the numerator. Late is present: tardiness is a separate
# signal, modelled separately, and folding it in here would double-count it.
PRESENT_STATUSES = ("present", "late", "online")

# Everything that counts toward the rate DENOMINATOR: attendance plus absence, but not
# the non-opportunity days below. Row.eligible is this rule applied to one student.
#
# This lived in three other modules -- analytics/trend.py, notify/periodic.py and
# export/sf2.py -- each with a comment telling the reader to keep it in step with the
# others. Four copies of one rule is three chances to change it in the wrong place.
COUNTED_STATUSES = (*PRESENT_STATUSES, "absent")

# Statuses meaning the student was never given the chance to attend. Two consequences,
# and they are the same idea applied twice:
#
#   * the day leaves the RATE DENOMINATOR -- a student excused for 3 of 40 sessions and
#     present for the other 37 scores 37/37, not 37/40 (docs/analytics-model.md 1)
#   * the day is TRANSPARENT to a run of absences -- see absence_run()
#
# A school-wide suspension never reaches either rule, because the date drops out of the
# register entirely. A PER-SECTION suspension does: corrections.suspend_section writes
# 'excused' rows on a date that is still a column.
NON_OPPORTUNITY = ("excused",)


def _runs(statuses: Iterable[str | None]):
    """Absence-run lengths, in school terms, skipping non-opportunity days.

    The caller passes school days in order, so Friday and the following Monday are
    adjacent and the weekend is simply not in the sequence.

    A day with NO RECORD breaks a run. An unknown day is not evidence of absence, and
    overstating a streak is what triggers a home visitation under SF2 guideline 5.
    """
    run = 0
    for status in statuses:
        if status in NON_OPPORTUNITY:
            continue
        run = run + 1 if status == "absent" else 0
        yield run


def longest_absence_run(statuses: Iterable[str | None]) -> int:
    """The longest run anywhere in the sequence. SF2's five-consecutive-days rule."""
    return max(_runs(statuses), default=0)


def trailing_absence_run(statuses: Iterable[str | None]) -> int:
    """The run ending at the LAST entry -- the streak a student is currently on.

    This is the risk model's `consecutive` feature, and it is computed over every prior
    day rather than a window: a streak that started six days ago is still a streak.
    """
    runs = list(_runs(statuses))
    return runs[-1] if runs else 0


class CorrectionType(str, Enum):
    EXCUSED = "excused_absence"
    ONLINE = "online_participation"
    SUSPENSION = "class_suspension"
    DATA_ERROR = "data_error"


# The status each type produces. DATA_ERROR is the only one where the operator chooses,
# because it is the only one that means "the record is simply wrong" rather than
# naming a specific circumstance.
TYPE_STATUS = {
    CorrectionType.EXCUSED: "excused",
    CorrectionType.ONLINE: "online",
    CorrectionType.SUSPENSION: "excused",
    CorrectionType.DATA_ERROR: None,
}

DATA_ERROR_STATUSES = ("present", "late", "absent", "excused", "online")


class CorrectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cell:
    """One student-day in the register."""

    date: str
    status: str | None
    letter: str
    corrected: bool
    attendance_day_id: int | None
    flags: str = ""


@dataclass(frozen=True)
class Row:
    student_id: int
    name: str
    cells: dict[str, Cell]
    present: int
    late: int
    absent: int
    excused: int
    # Populated only when register() is called across every section (section_id=None):
    # "Name" alone is ambiguous once two sections can share the vertical header.
    section: str = ""

    @property
    def eligible(self) -> int:
        return self.present + self.late + self.absent

    @property
    def rate(self) -> float | None:
        """None rather than 0.0 when there is nothing to average.

        A section with no recorded days has an UNDEFINED rate, not a 0% one, and
        showing 0% would read as catastrophic attendance rather than as no data.
        """
        return None if self.eligible == 0 else (self.present + self.late) / self.eligible


def live_row(
    conn: sqlite3.Connection, student_id: int, day: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM attendance_days
           WHERE student_id = ? AND date = ? AND superseded_by IS NULL""",
        (student_id, day),
    ).fetchone()


def correct(
    conn: sqlite3.Connection,
    student_id: int,
    day: str,
    kind: CorrectionType,
    *,
    reason: str,
    actor_name: str,
    status: str | None = None,
    at: datetime | None = None,
) -> int:
    """Supersede a student's record for one day. Returns the new row's id.

    Both `reason` and `actor_name` are mandatory. A correction with no reason is
    indistinguishable from tampering after the fact, and one with no name leaves an
    audit trail that records everything except the only thing anyone will ask.
    """
    if not (reason or "").strip():
        raise CorrectionError("a correction requires a reason")
    if not (actor_name or "").strip():
        raise CorrectionError("a correction requires the name of the person making it")

    # CorrectionType subclasses str, and anything that round-trips a value through a
    # Qt variant (a combo box's userData, for one) hands back a plain str. An identity
    # check against the enum then silently fails and every correction is treated as
    # the wrong type. Coerce once, here, at the boundary.
    kind = CorrectionType(kind)

    new_status = status or TYPE_STATUS[kind]
    if kind is CorrectionType.DATA_ERROR:
        if status is None:
            raise CorrectionError("a data-error correction must say what the status is")
        if status not in DATA_ERROR_STATUSES:
            raise CorrectionError(
                f"{status!r} is not a valid status. "
                f"Valid: {', '.join(DATA_ERROR_STATUSES)}"
            )

    at = at or datetime.now()
    with transaction(conn):
        return _apply(conn, student_id, day, kind, new_status,
                      reason.strip(), actor_name.strip(), at)


def _apply(
    conn: sqlite3.Connection, student_id: int, day: str, kind: CorrectionType,
    new_status: str, reason: str, actor_name: str, at: datetime,
) -> int:
    """The supersede sequence. Caller holds the transaction."""
    kind = CorrectionType(kind)
    old = live_row(conn, student_id, day)

    flags = [] if kind is not CorrectionType.SUSPENSION else ["class_suspension"]

    if old is None:
        # No record for that day at all -- a student who never scanned and whose day
        # was never closed. A correction is still legitimate (that is exactly when
        # someone files an excuse slip), so create the row directly. Nothing is being
        # superseded, so there is no chain to preserve.
        cursor = conn.execute(
            """INSERT INTO attendance_days
               (student_id, date, status, flags, corrected_by_name, correction_reason,
                correction_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_id, day, new_status, _merge_flags("", flags),
             actor_name, reason, kind.value, utcnow()),
        )
        new_id = cursor.lastrowid
        _audit(conn, new_id, student_id, day, None, new_status, kind, reason, actor_name)
        return new_id

    # --- the ordering that keeps every intermediate state legal -----------------
    #
    # idx_attendance_live is UNIQUE on (student_id, date) WHERE superseded_by IS NULL,
    # so exactly one row per student-day may be live. That rules out both obvious
    # sequences:
    #
    #   insert the new row live first  -> two live rows      -> UNIQUE violation
    #   mark the old row superseded    -> points at no row   -> FOREIGN KEY violation
    #
    # So the new row is born ALREADY SUPERSEDED (pointing at the old one, which
    # exists), the old one then steps down to point at it, and only then is the new
    # row released as live. Three statements, no illegal state at any point.
    #
    # Do not "simplify" this.
    cursor = conn.execute(
        """INSERT INTO attendance_days
           (student_id, date, entry_scan_id, exit_scan_id, status, flags,
            minutes_on_campus, superseded_by, corrected_by_name, correction_reason,
            correction_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (student_id, day, old["entry_scan_id"], old["exit_scan_id"], new_status,
         _merge_flags(old["flags"], flags), old["minutes_on_campus"],
         old["id"],                       # born superseded
         actor_name, reason, kind.value, utcnow()),
    )
    new_id = cursor.lastrowid
    conn.execute("UPDATE attendance_days SET superseded_by = ? WHERE id = ?",
                 (new_id, old["id"]))
    conn.execute("UPDATE attendance_days SET superseded_by = NULL WHERE id = ?",
                 (new_id,))

    _audit(conn, new_id, student_id, day, old["status"], new_status,
           kind, reason, actor_name)
    return new_id


def _audit(
    conn: sqlite3.Connection, row_id: int, student_id: int, day: str,
    old_status: str | None, new_status: str, kind: CorrectionType,
    reason: str, actor_name: str,
) -> None:
    # The name is written INTO the audit row rather than joined at read time, on
    # purpose. An audit entry should be readable on its own years later, and it should
    # record the name as it stood when the change was made -- if a student is renamed
    # afterwards, the log should still say who the record was about at the time.
    student = conn.execute(
        "SELECT first_name, last_name FROM students WHERE id = ?", (student_id,)
    ).fetchone()
    who = (f"{student['last_name']}, {student['first_name']}"
           if student else f"student {student_id}")

    audit(
        conn, "attendance.corrected",
        entity_type="attendance_days", entity_id=row_id,
        old_value=f"{who} {day}: {old_status or 'no record'}",
        new_value=f"{new_status} ({kind.value})",
        reason=reason, actor_name=actor_name,
    )


def suspend_section(
    conn: sqlite3.Connection,
    section_id: int,
    day: str,
    *,
    reason: str,
    actor_name: str,
    at: datetime | None = None,
) -> list[int]:
    """Class suspension: excuse every student in one section for one date.

    school_days is keyed by DATE ALONE and has no section column, so
    sessions.suspend_day() is school-wide and cannot express "8-Bonifacio had no
    classes today". Rather than reshape that table, this writes one ordinary
    correction per student, each individually audited.

    The rate arithmetic is identical either way, because excused already leaves the
    denominator -- and a per-student trail is what anyone reviewing one child's record
    actually needs to see.
    """
    if not (reason or "").strip():
        raise CorrectionError("a class suspension requires a reason")
    if not (actor_name or "").strip():
        raise CorrectionError("a class suspension requires the name of the person "
                              "recording it")

    at = at or datetime.now()
    students = conn.execute(
        "SELECT id FROM students WHERE section_id = ? AND active = 1 ORDER BY id",
        (section_id,),
    ).fetchall()
    if not students:
        raise CorrectionError(f"section {section_id} has no active students")

    with transaction(conn):
        ids = [
            _apply(conn, row["id"], day, CorrectionType.SUSPENSION, "excused",
                   reason.strip(), actor_name.strip(), at)
            for row in students
        ]
        audit(
            conn, "attendance.section_suspended",
            entity_type="sections", entity_id=section_id,
            new_value=f"{len(ids)} student(s) excused on {day}",
            reason=reason.strip(), actor_name=actor_name.strip(),
        )
    return ids


# --- the register -----------------------------------------------------------

def month_days(year: int, month: int) -> list[str]:
    return [
        Date(year, month, d).isoformat()
        for d in range(1, monthrange(year, month)[1] + 1)
    ]


def register(
    conn: sqlite3.Connection, section_id: int | None, year: int, month: int
) -> tuple[list[str], list[Row]]:
    """One section's month, or every section's when section_id is None.

    Live rows only, superseded ones excluded. section_id=None is "All students" in the
    Records page -- the predicate is simply omitted, the same conditional-SQL shape
    risk._history and trend.daily_rates already use for the same reason.
    """
    days = month_days(year, month)
    first, last = days[0], days[-1]

    student_sql = ["""SELECT s.id, s.first_name, s.last_name,
                             sec.grade_level, sec.name AS section_name
                      FROM students s JOIN sections sec ON sec.id = s.section_id
                      WHERE s.active = 1"""]
    student_params: list = []
    if section_id is not None:
        student_sql.append("AND s.section_id = ?")
        student_params.append(section_id)
    student_sql.append("ORDER BY sec.grade_level, sec.name, s.last_name, s.first_name")
    students = conn.execute(" ".join(student_sql), student_params).fetchall()

    attendance_sql = ["""SELECT a.* FROM attendance_days a
                         JOIN students s ON s.id = a.student_id
                         WHERE a.date BETWEEN ? AND ? AND a.superseded_by IS NULL"""]
    attendance_params: list = [first, last]
    if section_id is not None:
        attendance_sql.append("AND s.section_id = ?")
        attendance_params.append(section_id)

    records: dict[tuple[int, str], sqlite3.Row] = {}
    for row in conn.execute(" ".join(attendance_sql), attendance_params):
        records[(row["student_id"], row["date"])] = row

    rows: list[Row] = []
    for student in students:
        cells: dict[str, Cell] = {}
        counts = {"present": 0, "late": 0, "absent": 0, "excused": 0, "online": 0}

        for day in days:
            record = records.get((student["id"], day))
            if record is None:
                cells[day] = Cell(day, None, "", False, None)
                continue
            status = record["status"]
            counts[status] = counts.get(status, 0) + 1
            cells[day] = Cell(
                date=day,
                status=status,
                letter=LETTERS.get(status, "?"),
                corrected=record["correction_type"] is not None,
                attendance_day_id=record["id"],
                flags=record["flags"] or "",
            )

        rows.append(Row(
            student_id=student["id"],
            name=f"{student['last_name']}, {student['first_name']}",
            cells=cells,
            # online counts with present in the numerator; keep it visible separately
            # in the totals so a reader can see how the rate was reached.
            present=counts["present"] + counts["online"],
            late=counts["late"],
            absent=counts["absent"],
            excused=counts["excused"],
            section=(f"{student['grade_level']}-{student['section_name']}"
                     if section_id is None else ""),
        ))
    return days, rows


def edit_log(
    conn: sqlite3.Connection, *, section_id: int | None = None, limit: int = 500
) -> list[sqlite3.Row]:
    """Correction history, newest first. What changed, when, who said so, and why."""
    if section_id is None:
        return conn.execute(
            """SELECT * FROM audit_log
               WHERE action LIKE 'attendance.%'
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    return conn.execute(
        """SELECT a.* FROM audit_log a
           LEFT JOIN attendance_days d
                  ON a.entity_type = 'attendance_days'
                 AND CAST(a.entity_id AS INTEGER) = d.id
           LEFT JOIN students s ON s.id = d.student_id
           WHERE a.action LIKE 'attendance.%'
             AND (s.section_id = ?
                  OR (a.entity_type = 'sections' AND CAST(a.entity_id AS INTEGER) = ?))
           ORDER BY a.id DESC LIMIT ?""",
        (section_id, section_id, limit),
    ).fetchall()
