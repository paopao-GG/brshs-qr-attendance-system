"""Chain of custody for hazardous school tools.

Students legitimately bring cutters, scissors and compasses for art, TLE and drafting.
Refusing them is not workable and confiscating them permanently is not fair, so the
school holds them at the gate and the adviser signs them out for the class that needs
them.

What that means for this module: these records concern **potentially dangerous objects
belonging to minors**, and if one goes missing the chain here is the school's entire
account of what happened to it. Every transition therefore records who did it, when,
and -- where a judgement was involved -- why. Nothing is deleted; a mistake is
corrected by a later transition, not by editing an earlier one.

Qt-free, like the rest of core/.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .db import audit, transaction, utcnow


class Status(str, Enum):
    HELD = "held"            # in the school's storage
    RELEASED = "released"    # signed out to an adviser for a class
    RETURNED = "returned"    # back in storage, or back with the student
    DISPOSED = "disposed"    # surrendered or destroyed, with a reason


class CustodyError(RuntimeError):
    """A transition that the chain does not allow."""


@dataclass(frozen=True)
class Release:
    """The result of signing an item out."""

    custody_id: int
    backed_by_request: bool
    request_id: int | None = None


def collect(
    conn: sqlite3.Connection,
    student_id: int,
    item_description: str,
    *,
    screening_event_id: int | None = None,
    purpose: str | None = None,
    storage_ref: str | None = None,
    category: str = "tool",
    collected_by: int | None = None,
    at: datetime | None = None,
) -> int:
    """Take an item into custody at the gate. Returns the custody id.

    `storage_ref` is the physical tag written on the item. It is optional in the
    schema only because a guard under pressure may take the item first and tag it
    seconds later -- but an untagged item in a box of forty is effectively lost, so
    the UI should always supply one.
    """
    at = at or datetime.now()
    if not (item_description or "").strip():
        raise ValueError("item_description is required: describe what was collected")

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO custody_items
               (student_id, screening_event_id, item_description, category, purpose,
                storage_ref, status, collected_at, collected_by)
               VALUES (?, ?, ?, ?, ?, ?, 'held', ?, ?)""",
            (student_id, screening_event_id, item_description.strip(), category,
             purpose, storage_ref, at.isoformat(timespec="seconds"), collected_by),
        )
        custody_id = cur.lastrowid
        audit(
            conn, "custody.collected",
            actor_id=collected_by, entity_type="custody_items", entity_id=custody_id,
            new_value=f"{item_description.strip()} (tag {storage_ref or 'none'})",
            reason=purpose,
        )
    return custody_id


def matching_request(
    conn: sqlite3.Connection, custody_id: int, day: str
) -> sqlite3.Row | None:
    """A teacher's declaration covering this student's section on this date.

    This is the control: a release backed by a request is an expected event, one
    without is a judgement call someone has to justify.
    """
    return conn.execute(
        """SELECT h.* FROM hazard_requests h
           JOIN students s ON s.section_id = h.section_id
           JOIN custody_items c ON c.student_id = s.id
           WHERE c.id = ? AND h.date = ?
           ORDER BY h.id LIMIT 1""",
        (custody_id, day),
    ).fetchone()


def release(
    conn: sqlite3.Connection,
    custody_id: int,
    released_to: int,
    *,
    reason: str | None = None,
    at: datetime | None = None,
    actor_id: int | None = None,
) -> Release:
    """Sign an item out to an adviser for the class that needs it.

    A release with no matching hazard_request is permitted -- a class changes, a
    teacher forgets, and blocking it would push the whole thing back to an unrecorded
    handover in a corridor. It requires a reason and is flagged `released_unbacked`,
    so the controlled path and the exception stay distinguishable in the data.
    """
    at = at or datetime.now()
    row = _require(conn, custody_id)

    if row["status"] != Status.HELD.value:
        raise CustodyError(
            f"item {custody_id} is {row['status']}, not held; only a held item "
            "can be released"
        )

    request = matching_request(conn, custody_id, at.date().isoformat())
    if request is None and not (reason or "").strip():
        raise CustodyError(
            "no hazard request covers this section today; a reason is required to "
            "release the item anyway"
        )

    with transaction(conn):
        conn.execute(
            """UPDATE custody_items
               SET status = 'released', released_at = ?, released_to = ?,
                   release_reason = ?, released_unbacked = ?
               WHERE id = ?""",
            (at.isoformat(timespec="seconds"), released_to, reason,
             0 if request is not None else 1, custody_id),
        )
        audit(
            conn, "custody.released",
            actor_id=actor_id, entity_type="custody_items", entity_id=custody_id,
            old_value=Status.HELD.value, new_value=Status.RELEASED.value,
            reason=reason or (
                f"hazard_request {request['id']}" if request is not None else None
            ),
        )
    return Release(custody_id, request is not None,
                   request["id"] if request is not None else None)


def give_back(
    conn: sqlite3.Connection,
    custody_id: int,
    to: str,
    *,
    at: datetime | None = None,
    actor_id: int | None = None,
) -> None:
    """Close the chain: back to storage after the period, or to the student at
    dismissal.

    `to` is recorded rather than inferred because the two mean different things -- an
    item back in storage is still the school's responsibility, one returned to a
    student is not.
    """
    if to not in ("storage", "student"):
        raise ValueError("to must be 'storage' or 'student'")

    at = at or datetime.now()
    row = _require(conn, custody_id)
    if row["status"] not in (Status.HELD.value, Status.RELEASED.value):
        raise CustodyError(f"item {custody_id} is {row['status']} and cannot be returned")

    with transaction(conn):
        conn.execute(
            """UPDATE custody_items
               SET status = 'returned', returned_at = ?, returned_to = ?
               WHERE id = ?""",
            (at.isoformat(timespec="seconds"), to, custody_id),
        )
        audit(
            conn, "custody.returned",
            actor_id=actor_id, entity_type="custody_items", entity_id=custody_id,
            old_value=row["status"], new_value=f"returned to {to}",
        )


def dispose(
    conn: sqlite3.Connection,
    custody_id: int,
    reason: str,
    *,
    at: datetime | None = None,
    actor_id: int | None = None,
) -> None:
    """Surrendered or destroyed. Always requires a reason -- this is the one
    transition after which the item does not exist to be accounted for."""
    if not (reason or "").strip():
        raise ValueError("disposing of an item requires a reason")

    at = at or datetime.now()
    row = _require(conn, custody_id)

    with transaction(conn):
        conn.execute(
            "UPDATE custody_items SET status = 'disposed' WHERE id = ?", (custody_id,)
        )
        audit(
            conn, "custody.disposed",
            actor_id=actor_id, entity_type="custody_items", entity_id=custody_id,
            old_value=row["status"], new_value=Status.DISPOSED.value, reason=reason,
        )


def held_for_section(conn: sqlite3.Connection, section_id: int) -> list[sqlite3.Row]:
    """What the adviser sees when they come to claim: this section's held items, and
    whether a request covers each of them today."""
    return conn.execute(
        """SELECT c.*, s.first_name, s.last_name,
                  EXISTS (SELECT 1 FROM hazard_requests h
                          WHERE h.section_id = s.section_id
                            AND h.date = date('now', 'localtime')) AS has_request
           FROM custody_items c
           JOIN students s ON s.id = c.student_id
           WHERE s.section_id = ? AND c.status = 'held'
           ORDER BY c.collected_at""",
        (section_id,),
    ).fetchall()


def outstanding(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything not yet back: held or signed out.

    An item still `released` at the end of the day is the one worth chasing -- it is
    out of storage and nobody has said where it went.
    """
    return conn.execute(
        """SELECT c.*, s.first_name, s.last_name
           FROM custody_items c
           JOIN students s ON s.id = c.student_id
           WHERE c.status IN ('held', 'released')
           ORDER BY c.status DESC, c.collected_at""",
    ).fetchall()


def chain(conn: sqlite3.Connection, custody_id: int) -> list[sqlite3.Row]:
    """Every audited transition for one item, oldest first. This is the record the
    school produces if an item goes missing."""
    return conn.execute(
        """SELECT * FROM audit_log
           WHERE entity_type = 'custody_items' AND entity_id = ?
           ORDER BY id""",
        (str(custody_id),),
    ).fetchall()


def request_tools(
    conn: sqlite3.Connection,
    section_id: int,
    day: str,
    subject: str,
    item_type: str,
    *,
    notes: str | None = None,
    requested_by: int | None = None,
) -> int:
    """A teacher declaring that a section needs hazardous tools for a subject."""
    cur = conn.execute(
        """INSERT INTO hazard_requests
           (section_id, date, subject, item_type, notes, requested_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (section_id, day, subject, item_type, notes, requested_by, utcnow()),
    )
    return cur.lastrowid


def _require(conn: sqlite3.Connection, custody_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM custody_items WHERE id = ?", (custody_id,)
    ).fetchone()
    if row is None:
        raise CustodyError(f"no custody item {custody_id}")
    return row
