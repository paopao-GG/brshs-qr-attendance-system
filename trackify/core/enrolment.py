"""Applying a parsed roster to the database.

roster.py reads a spreadsheet. This decides what that means for the students already in
the system, and writes it. The two are separate because the failure modes are different:
a parsing bug produces a wrong name, an enrolment bug produces the same child twice, one
row holding their attendance history and the other holding their card.

Three rules shape everything here.

**Nothing is written until a person confirms.** plan_import() reads; apply_import() writes.
An operator sees exactly what will change before it changes -- 103 rows arriving from a
file the adviser emailed is not something to apply on trust.

**Matching is by LRN first, then by name.** See match() -- this is the rule that stops
duplicates, and the reason it is not simply "match on LRN" is written out there.

**An import may not touch consent, notify_optin, active or photo_path.** A spreadsheet
cannot grant consent under RA 10173, and queue.py refuses to enqueue without it; letting
a column in a file flip that would route around the one guard that travels with the
database. active is excluded for the same shape of reason: a student deactivated in
October must not be silently readmitted by November's file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import roster
from .db import audit, transaction, utcnow
from .mobile import InvalidMobile, normalise

# Columns an import is allowed to write. Everything absent from this tuple is deliberate;
# see the module docstring before adding to it.
IMPORTABLE = ("lrn", "first_name", "last_name", "section_id",
              "guardian_name", "guardian_mobile", "sex")

NEW = "new"
UPDATED = "updated"
LRN_CHANGED = "lrn_changed"
UNCHANGED = "unchanged"

CARD_WARNING = (
    "Their printed card stops working. The QR payload is signed over the LRN, so the "
    "old card no longer resolves at the gate. Reprint it with the QR generator, which "
    "will report this student as UPDATED."
)


class EnrolmentError(ValueError):
    pass


@dataclass(frozen=True)
class Change:
    """What an import will do to one student."""

    kind: str
    candidate: roster.Candidate
    student_id: int | None = None
    # column -> (before, after). Empty for an unchanged row.
    fields: dict[str, tuple[object, object]] = field(default_factory=dict)
    old_lrn: str | None = None

    @property
    def breaks_the_card(self) -> bool:
        return self.kind == LRN_CHANGED

    @property
    def writes(self) -> bool:
        return self.kind in (NEW, UPDATED, LRN_CHANGED)


@dataclass(frozen=True)
class ImportPlan:
    changes: list[Change] = field(default_factory=list)
    # Students in the database that this file does not mention. Reported, never touched:
    # an adviser importing one section's list would otherwise wipe the other two.
    missing: list[sqlite3.Row] = field(default_factory=list)
    rejected: list[roster.Rejected] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]

    @property
    def counts(self) -> dict[str, int]:
        return {kind: len(self.of_kind(kind))
                for kind in (NEW, UPDATED, LRN_CHANGED, UNCHANGED)}

    @property
    def card_reprints(self) -> list[Change]:
        return [c for c in self.changes if c.breaks_the_card]

    @property
    def sex_recorded(self) -> list[Change]:
        """Students who will GAIN a sex -- not those who merely have one.

        Worth its own line on the preview because it is invisible in the others: most
        of the changes in a re-import of the office sheet are sex and nothing else, so
        a screen that only says "updated" would not mention the one thing the import
        was run for. A correction from M to F is an ordinary update; only None -> M or
        None -> F is counted here.
        """
        def gains_one(change: Change) -> bool:
            if change.kind == NEW:
                return bool(change.candidate.sex)
            before, after = change.fields.get("sex", (None, None))
            return before is None and after is not None

        return [c for c in self.changes if c.writes and gains_one(c)]

    @property
    def writes(self) -> int:
        return sum(1 for c in self.changes if c.writes)


# --- reading ----------------------------------------------------------------

def _existing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, sec.name AS section_name, sec.grade_level
           FROM students s JOIN sections sec ON sec.id = s.section_id"""
    ).fetchall()


def _name_key(last: str, first: str, grade_level: int, section_name: str) -> tuple:
    return (last.strip().casefold(), first.strip().casefold(),
            grade_level, section_name.strip().casefold())


def match(candidate: roster.Candidate, by_lrn: dict, by_name: dict) -> sqlite3.Row | None:
    """Find the student a spreadsheet row refers to, if they are already enrolled.

    LRN first, then name -- and the fallback is not optional.

    Matching on LRN alone looks obviously right and quietly corrupts the roster. A
    student already in the system whose LRN is CORRECTED in the spreadsheet would not
    match, would import as new, and the school would end up holding that child twice:
    one row with their attendance history, another with their working card. The name
    fallback catches exactly that case, and it is why LRN_CHANGED exists as its own
    outcome rather than being folded into UPDATED.

    qr-generator/generate.py matches in the same order, for the same reason.
    """
    if candidate.lrn in by_lrn:
        return by_lrn[candidate.lrn]
    key = _name_key(candidate.last, candidate.first,
                    candidate.grade_level, candidate.section_name)
    return by_name.get(key)


# Columns where an EMPTY cell in the spreadsheet means "no information", not "delete
# what you have". Everything else -- a name, an LRN, a section -- is always filled in
# for a candidate, so the distinction does not arise there.
#
# sex belongs here for the same reason as the guardian columns, one step removed: it
# comes from a MALE/FEMALE banner, and a sheet where somebody deleted the banner rows
# would otherwise blank a field staff had set in the roster screen.
FILL_ONLY = ("guardian_name", "guardian_mobile", "sex")


def _would_erase(column: str, current, incoming) -> bool:
    """True when applying `incoming` would blank a value a person had put there.

    The office spreadsheet is chronically incomplete -- that is the premise of the whole
    roster screen. If importing it nulled out every guardian number staff had typed in,
    the next import would undo an afternoon's work and nobody would see it happen. So an
    import may FILL a blank and never CLEAR one; clearing is a deliberate act in the
    edit dialog, by a person, with a reason.
    """
    return column in FILL_ONLY and current is not None and incoming is None


def _section_id(conn: sqlite3.Connection, grade_level: int, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM sections WHERE grade_level = ? AND name = ?",
        (grade_level, name),
    ).fetchone()
    return row["id"] if row else None


def plan_import(
    conn: sqlite3.Connection,
    candidates: Iterable[roster.Candidate],
    rejected: Iterable[roster.Rejected] = (),
) -> ImportPlan:
    """Work out what an import would do. Writes nothing."""
    candidates = list(candidates)
    existing = _existing(conn)
    by_lrn = {row["lrn"]: row for row in existing}
    by_name = {_name_key(row["last_name"], row["first_name"],
                         row["grade_level"], row["section_name"]): row
               for row in existing}

    changes: list[Change] = []
    seen: set[int] = set()

    for candidate in candidates:
        current = match(candidate, by_lrn, by_name)

        if current is None:
            changes.append(Change(kind=NEW, candidate=candidate))
            continue

        seen.add(current["id"])
        # section_id is resolved lazily: a section named in the file but absent from the
        # database is created at apply time, so planning must not depend on it existing.
        target_section = _section_id(conn, candidate.grade_level, candidate.section_name)

        wanted = {
            "lrn": candidate.lrn,
            "first_name": candidate.first,
            "last_name": candidate.last,
            "section_id": target_section,
            "guardian_name": candidate.guardian_name or None,
            "guardian_mobile": candidate.guardian_mobile,
            "sex": candidate.sex,
        }
        fields = {
            column: (current[column], value)
            for column, value in wanted.items()
            # A section that does not exist yet cannot be compared; apply() creates it
            # and the row is caught as a change there.
            if not (column == "section_id" and value is None)
            and not _would_erase(column, current[column], value)
            and current[column] != value
        }

        if not fields:
            changes.append(Change(kind=UNCHANGED, candidate=candidate,
                                  student_id=current["id"]))
        elif "lrn" in fields:
            changes.append(Change(kind=LRN_CHANGED, candidate=candidate,
                                  student_id=current["id"], fields=fields,
                                  old_lrn=current["lrn"]))
        else:
            changes.append(Change(kind=UPDATED, candidate=candidate,
                                  student_id=current["id"], fields=fields))

    missing = [row for row in existing if row["id"] not in seen]
    return ImportPlan(changes=changes, missing=missing, rejected=list(rejected))


# --- writing ----------------------------------------------------------------

def apply_import(conn: sqlite3.Connection, plan: ImportPlan, *,
                 actor_name: str) -> dict[str, int]:
    """Write the plan. Returns how many rows of each kind were written.

    ALL OR NOTHING. The connection runs in autocommit, so without this transaction each
    student landed on its own: failing at row 60 of 109 left 59 written, 50 not, and no
    way back -- after a preview dialog had already told the operator what would happen.
    This is the case db.transaction was written for.
    """
    actor_name = (actor_name or "").strip()
    if not actor_name:
        raise EnrolmentError("An import requires the name of the person doing it.")

    written = {NEW: 0, UPDATED: 0, LRN_CHANGED: 0}

    with transaction(conn):
        for change in plan.changes:
            if not change.writes:
                continue
            candidate = change.candidate
            section_id = _ensure_section(
                conn, candidate.grade_level, candidate.section_name)

            if change.kind == NEW:
                _insert(conn, candidate, section_id, actor_name)
            else:
                _update(conn, change, section_id, actor_name)
            written[change.kind] += 1

    return written


def _ensure_section(conn: sqlite3.Connection, grade_level: int, name: str) -> int:
    found = _section_id(conn, grade_level, name)
    if found is not None:
        return found
    return conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES (?, ?)", (name, grade_level)
    ).lastrowid


def _insert(conn: sqlite3.Connection, candidate: roster.Candidate,
            section_id: int, actor_name: str) -> int:
    cursor = conn.execute(
        """INSERT INTO students
           (lrn, first_name, last_name, section_id, guardian_name, guardian_mobile,
            sex, consent_on_file, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        # consent_on_file = 0, always. A spreadsheet cannot record consent under
        # RA 10173, and queue.py checks this column before enqueueing anything, so a
        # student created here is unreachable by SMS until a person says otherwise.
        (candidate.lrn, candidate.first, candidate.last, section_id,
         candidate.guardian_name or None, candidate.guardian_mobile, candidate.sex,
         utcnow()),
    )
    student_id = cursor.lastrowid
    audit(conn, "student.imported", entity_type="student", entity_id=student_id,
          new_value=f"{candidate.full_name} ({candidate.section_label}), "
                    f"LRN {candidate.lrn}",
          actor_name=actor_name, reason="roster import")
    return student_id


def _update(conn: sqlite3.Connection, change: Change,
            section_id: int, actor_name: str) -> None:
    fields = dict(change.fields)
    fields["section_id"] = section_id
    fields = {c: v for c, v in fields.items() if c in IMPORTABLE}

    current = conn.execute("SELECT * FROM students WHERE id = ?",
                           (change.student_id,)).fetchone()
    fields = {c: v[1] if isinstance(v, tuple) else v for c, v in fields.items()}
    fields = {c: v for c, v in fields.items() if current[c] != v}
    if not fields:
        return

    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(f"UPDATE students SET {assignments} WHERE id = ?",
                 (*fields.values(), change.student_id))

    before = ", ".join(f"{c}={current[c]!r}" for c in fields)
    after = ", ".join(f"{c}={v!r}" for c, v in fields.items())
    reason = "roster import"
    if change.kind == LRN_CHANGED:
        reason = f"roster import - LRN changed, card must be reprinted. {CARD_WARNING}"
    audit(conn, "student.updated", entity_type="student", entity_id=change.student_id,
          old_value=before, new_value=after,
          actor_name=actor_name, reason=reason)


# --- single-student edits ---------------------------------------------------

EDITABLE = ("first_name", "last_name", "lrn", "section_id",
            "guardian_name", "guardian_mobile", "sex", "consent_on_file",
            "notify_optin")


def update_student(conn: sqlite3.Connection, student_id: int, *,
                   actor_name: str, reason: str, **fields) -> dict:
    """Edit one student from the UI. Returns the columns that actually changed.

    Unlike an import, this MAY set consent_on_file -- a person ticking a box having seen
    the signed form is exactly the authority a spreadsheet column lacks.
    """
    actor_name = (actor_name or "").strip()
    reason = (reason or "").strip()
    if not actor_name:
        raise EnrolmentError("An edit requires the name of the person making it.")
    if not reason:
        raise EnrolmentError("An edit requires a reason.")

    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise EnrolmentError(f"Not editable here: {', '.join(sorted(unknown))}")

    if "guardian_mobile" in fields:
        try:
            fields["guardian_mobile"] = normalise(fields["guardian_mobile"])
        except InvalidMobile as exc:
            raise EnrolmentError(str(exc)) from exc

    if "sex" in fields:
        # "" from an unset combo box means "not recorded", which is a legitimate state
        # and must reach the column as NULL rather than as an empty string the CHECK
        # would reject.
        value = (fields["sex"] or "").strip().upper() or None
        if value not in (None, "M", "F"):
            raise EnrolmentError(f"sex must be M, F, or blank - not {fields['sex']!r}.")
        fields["sex"] = value

    for column in ("first_name", "last_name", "lrn"):
        if column in fields and not str(fields[column] or "").strip():
            raise EnrolmentError(f"{column.replace('_', ' ')} cannot be empty.")
        if column in fields:
            fields[column] = str(fields[column]).strip()

    current = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if current is None:
        raise EnrolmentError(f"No student with id {student_id}.")

    changed = {c: v for c, v in fields.items() if current[c] != v}
    if not changed:
        return {}

    if "lrn" in changed:
        clash = conn.execute("SELECT id FROM students WHERE lrn = ? AND id != ?",
                             (changed["lrn"], student_id)).fetchone()
        if clash:
            raise EnrolmentError(
                f"LRN {changed['lrn']} already belongs to another student.")

    # The edit and its audit row are one fact. Written separately, a crash between them
    # produces a changed record with no trail -- the exact thing the trail is for.
    with transaction(conn):
        assignments = ", ".join(f"{column} = ?" for column in changed)
        conn.execute(f"UPDATE students SET {assignments} WHERE id = ?",
                     (*changed.values(), student_id))

        note = f" {CARD_WARNING}" if "lrn" in changed else ""
        audit(conn, "student.edited", entity_type="student", entity_id=student_id,
              old_value=", ".join(f"{c}={current[c]!r}" for c in changed),
              new_value=", ".join(f"{c}={v!r}" for c, v in changed.items()),
              actor_name=actor_name, reason=reason + note)
    return changed


def set_active(conn: sqlite3.Connection, student_id: int, active: bool, *,
               actor_name: str, reason: str) -> None:
    """Deactivate or readmit a student.

    Not a delete: scan_events.student_id is ON DELETE RESTRICT, so a student who has ever
    scanned cannot be removed, and should not be -- their attendance history is the
    record. ScanService.student_row() filters active = 1, so deactivating stops their
    card at the gate immediately.
    """
    actor_name = (actor_name or "").strip()
    reason = (reason or "").strip()
    if not actor_name:
        raise EnrolmentError("This requires the name of the person doing it.")
    if not reason:
        raise EnrolmentError("This requires a reason.")

    current = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if current is None:
        raise EnrolmentError(f"No student with id {student_id}.")

    value = 1 if active else 0
    if current["active"] == value:
        return

    with transaction(conn):
        conn.execute("UPDATE students SET active = ? WHERE id = ?", (value, student_id))
        audit(conn, "student.readmitted" if active else "student.deactivated",
              entity_type="student", entity_id=student_id,
              old_value=f"active={current['active']}", new_value=f"active={value}",
              actor_name=actor_name, reason=reason)


def roster_rows(conn: sqlite3.Connection, *, section_id: int | None = None,
                search: str = "", include_inactive: bool = True) -> list[sqlite3.Row]:
    """The roster table's contents, newest sections first, students by name."""
    sql = ["""SELECT s.*, sec.name AS section_name, sec.grade_level
              FROM students s JOIN sections sec ON sec.id = s.section_id"""]
    where, params = [], []
    if section_id is not None:
        where.append("s.section_id = ?")
        params.append(section_id)
    if not include_inactive:
        where.append("s.active = 1")
    if search.strip():
        where.append("(s.last_name LIKE ? OR s.first_name LIKE ? OR s.lrn LIKE ?)")
        params += [f"%{search.strip()}%"] * 3
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY sec.grade_level, sec.name, s.last_name, s.first_name")
    return conn.execute(" ".join(sql), params).fetchall()
