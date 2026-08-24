"""The notification queue: enqueue, coalesce, claim, drain.

Three de-duplication mechanisms operate here and they solve different problems:

  debounce   -- in core/attendance.py. Stops duplicate ATTENDANCE ROWS.
  idempotency -- this module. Stops duplicate SENDS across worker restarts.
  coalescing  -- this module. Stops a parent with two children getting two texts.

Nothing in the scan path waits on the network: record_scan writes a pending row and
returns. The worker drains it separately.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from ..core.attendance import Trigger, fmt_time
from ..core.config import Config
from ..core.db import utcnow
from . import coalesce, gsm7
from .limits import BreakerTripped, SpendBreaker, TokenBucket
from .provider import NotificationProvider

# Bodies are kept under one segment. Coalesced multi-child messages are the most
# likely to overflow, so they are truncated deterministically.
TEMPLATES = {
    Trigger.ARRIVAL: "TRACKIFY: {first} ({section}) arrived {time} on {date}.",
    Trigger.DEPARTURE: "TRACKIFY: {first} ({section}) left school {time} on {date}.",
    Trigger.LATE: "TRACKIFY: {first} ({section}) arrived late at {time} on {date}.",
    Trigger.ABSENT: (
        "TRACKIFY: {first} ({section}) was not recorded present on {date}. "
        "Please contact the school if unexpected."
    ),
}


def idempotency_key(student_id: int, trigger: Trigger, day: str, direction: str | None) -> str:
    """Stable across restarts, so re-enqueueing the same event is a no-op."""
    raw = f"{student_id}|{trigger.value}|{day}|{direction or ''}"
    return sha256(raw.encode("utf8")).hexdigest()


@dataclass(frozen=True)
class EnqueueResult:
    queued: bool
    reason: str = ""
    notification_id: int | None = None


def render(trigger: Trigger, student: sqlite3.Row, at: datetime) -> str:
    body = TEMPLATES[trigger].format(
        first=student["first_name"],
        section=student["section_name"],
        time=fmt_time(at),
        date=at.date().isoformat(),
    )
    return gsm7.truncate(body)


def _student(conn: sqlite3.Connection, student_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT s.*, sec.name AS section_name, sec.grade_level
           FROM students s JOIN sections sec ON sec.id = s.section_id
           WHERE s.id = ?""",
        (student_id,),
    ).fetchone()


def enqueue(
    conn: sqlite3.Connection,
    student_id: int,
    trigger: Trigger,
    at: datetime,
    config: Config,
    *,
    direction: str | None = None,
) -> EnqueueResult:
    """Write a pending notification. Never sends, never blocks."""
    student = _student(conn, student_id)
    if student is None:
        return EnqueueResult(False, "unknown student")

    if not student["consent_on_file"]:
        return EnqueueResult(False, "no consent on file")
    if not student["notify_optin"]:
        return EnqueueResult(False, "guardian opted out")
    if not student["guardian_mobile"]:
        return EnqueueResult(False, "no guardian mobile")

    policy = config.notifications
    if trigger is Trigger.ARRIVAL and not policy.notify_on_arrival:
        return EnqueueResult(False, "policy excludes arrival")
    if trigger is Trigger.DEPARTURE and not policy.notify_on_departure:
        return EnqueueResult(False, "policy excludes departure")

    body = render(trigger, student, at)
    try:
        gsm7.validate(body)
    except (gsm7.NotGSM7, ValueError) as exc:
        # Fail at enqueue time, not silently at double cost on every send.
        return EnqueueResult(False, f"invalid body: {exc}")

    key = idempotency_key(student_id, trigger, at.date().isoformat(), direction)
    cursor = conn.execute(
        """INSERT OR IGNORE INTO notifications
           (student_id, guardian_mobile, trigger, idempotency_key, body,
            status, event_at, queued_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (student_id, student["guardian_mobile"], trigger.value, key, body,
         at.isoformat(timespec="seconds"), utcnow()),
    )
    if cursor.rowcount == 0:
        return EnqueueResult(False, "already queued (idempotent)")
    return EnqueueResult(True, notification_id=cursor.lastrowid)


def enqueue_for_scan(
    conn: sqlite3.Connection, result, config: Config
) -> list[EnqueueResult]:
    """Queue whatever a ScanResult implies. Called right after record_scan."""
    if not result.recorded:
        return []
    return [
        enqueue(conn, result.student_id, trigger, result.at, config,
                direction=result.direction)
        for trigger in result.triggers
    ]


# --- coalescing -------------------------------------------------------------

def claim_batch(
    conn: sqlite3.Connection, config: Config, *, now: datetime | None = None
) -> list[tuple[list[sqlite3.Row], str]]:
    """Atomically claim pending rows and plan the outbound messages.

    Rows are only claimed once their coalescing window has elapsed, so siblings
    scanning a minute apart land in the same message. Grouping is bounded by event
    time and split to fit one segment -- see notify/coalesce.py.
    """
    now = now or datetime.now()
    window = timedelta(minutes=config.notifications.coalesce_window_minutes)
    cutoff = (now - window).isoformat(timespec="seconds")

    rows = conn.execute(
        """SELECT * FROM notifications
           WHERE status = 'pending' AND queued_at <= ?
           ORDER BY guardian_mobile, event_at, id""",
        (cutoff,),
    ).fetchall()

    by_guardian: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_guardian.setdefault(row["guardian_mobile"], []).append(row)

    planned: list[tuple[list[sqlite3.Row], str]] = []
    for group in by_guardian.values():
        planned.extend(coalesce.plan_messages(group, window))

    claimed: list[tuple[list[sqlite3.Row], str]] = []
    for chunk, body in planned:
        ids = [r["id"] for r in chunk]
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(
            f"""UPDATE notifications SET status = 'sending', claimed_at = ?
                WHERE id IN ({placeholders}) AND status = 'pending'""",
            (utcnow(), *ids),
        )
        if cursor.rowcount:
            claimed.append((chunk, body))
    return claimed


def drain(
    conn: sqlite3.Connection,
    provider: NotificationProvider,
    config: Config,
    breaker: SpendBreaker,
    bucket: TokenBucket | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Send one pass of the queue. Returns counts by outcome.

    Runs on a worker thread, never the Qt UI thread.
    """
    stats = {"sent": 0, "failed": 0, "unknown": 0, "suppressed": 0, "messages": 0}

    for group, body in claim_batch(conn, config, now=now):
        mobile = group[0]["guardian_mobile"]
        ids = [r["id"] for r in group]
        coalesce_group = f"cg-{ids[0]}" if len(ids) > 1 else None

        try:
            breaker.check(mobile)
        except BreakerTripped as exc:
            _mark(conn, ids, "suppressed", error=str(exc))
            stats["suppressed"] += len(ids)
            if "Daily SMS cap" in str(exc):
                raise
            continue

        if bucket is not None:
            import time as _time
            wait = bucket.time_until()
            if wait > 0:
                _time.sleep(wait)
            bucket.try_acquire()

        result = provider.send(mobile, body)
        stats["messages"] += 1

        if result.ok:
            _mark(conn, ids, "sent", message_id=result.provider_message_id,
                  coalesce_group=coalesce_group)
            breaker.record_sent(1)
            stats["sent"] += len(ids)
        elif result.ambiguous:
            # Do NOT retry. The request may have landed. A human reconciles this
            # against the PhilSMS dashboard from the queue monitor.
            _mark(conn, ids, "unknown", error=result.error)
            stats["unknown"] += len(ids)
        else:
            _retry_or_fail(conn, group, config, result.error)
            stats["failed"] += len(ids)

    return stats


def _mark(
    conn: sqlite3.Connection, ids: list[int], status: str, *,
    message_id: str | None = None, error: str | None = None,
    coalesce_group: str | None = None,
) -> None:
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"""UPDATE notifications
            SET status = ?, provider_message_id = ?, last_error = ?,
                coalesce_group = ?, sent_at = ?
            WHERE id IN ({placeholders})""",
        (status, message_id, error, coalesce_group,
         utcnow() if status == "sent" else None, *ids),
    )


def _retry_or_fail(
    conn: sqlite3.Connection, group: list[sqlite3.Row], config: Config, error: str | None
) -> None:
    for row in group:
        attempts = row["retry_count"] + 1
        status = "failed" if attempts >= config.notifications.retry_limit else "pending"
        conn.execute(
            """UPDATE notifications
               SET status = ?, retry_count = ?, last_error = ?, claimed_at = NULL
               WHERE id = ?""",
            (status, attempts, error, row["id"]),
        )


def reconcile_stale(conn: sqlite3.Connection, *, timeout_minutes: int = 10) -> int:
    """Rows stuck in 'sending' past a timeout become 'unknown', never 'pending'.

    This is the crash-recovery path. The worker died between the API call and
    recording the result, so we cannot know whether the message was delivered.
    Re-sending risks a duplicate; for SMS that is the worse error.
    """
    cutoff = (datetime.now() - timedelta(minutes=timeout_minutes)).isoformat(
        timespec="seconds"
    )
    cursor = conn.execute(
        """UPDATE notifications
           SET status = 'unknown',
               last_error = 'Worker stopped mid-send; delivery unconfirmed'
           WHERE status = 'sending' AND claimed_at IS NOT NULL AND claimed_at <= ?""",
        (cutoff,),
    )
    return cursor.rowcount


def unsent_count(conn: sqlite3.Connection) -> int:
    """Surfaced persistently on the kiosk status bar so an outage is noticed today."""
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE status IN ('pending','sending','failed','unknown')"
    ).fetchone()[0]
