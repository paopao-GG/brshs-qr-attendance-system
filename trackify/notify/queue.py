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
    # DELIBERATELY VAGUE, and it must stay that way. An SMS is unencrypted, passes
    # through the telco, and a wrong guardian number on a school roster sends a child's
    # data to a stranger -- the likeliest real privacy incident in this system.
    # "Your child was found carrying a knife" arriving on the wrong handset is the
    # worst thing this system could do. The item, the category and the severity are
    # delivered by a person, to a verified parent, in the school office.
    Trigger.INCIDENT: (
        "TRACKIFY: Please contact the school today regarding {first} ({section})."
    ),
    # Periodic. Both are deliberately plain counts of the guardian's OWN child -- no
    # rank, no risk band, no comparison with classmates. sms-notifications.md section 6:
    # minimise the payload.
    Trigger.SUMMARY: (
        "TRACKIFY: {first} ({section}) week of {period}: present {present}, "
        "late {late}, absent {absent} of {days} school days."
    ),
    # {clause} carries the "1 more allowed" or "that is the limit" wording, computed by
    # the caller so the template stays a template.
    Trigger.REMINDER: (
        "TRACKIFY: {first} ({section}): {absent} absences in {period}. {clause} "
        "Please contact the school if there is a difficulty at home."
    ),
}

# Words that must never reach an SMS body. Checked at enqueue time rather than trusted
# to a template that someone edits later without reading the comment above.
INCIDENT_FORBIDDEN = (
    "knife", "blade", "bladed", "dagger", "razor", "cutter", "weapon", "hammer",
    "knuckle", "pointed", "blunt", "severity", "prohibited", "incident",
)


def idempotency_key(
    student_id: int, trigger: Trigger, day: str, direction: str | None,
    dedupe_extra: str | None = None,
) -> str:
    """Stable across restarts, so re-enqueueing the same event is a no-op.

    dedupe_extra is appended only when given, so every key generated before it
    existed still hashes to the same value -- a pending row must not be orphaned by
    a code change and then re-sent as a "new" message.

    It exists for incidents: one student can have two on the same day, and without a
    discriminator the second would be silently swallowed as a duplicate of the first.
    """
    raw = f"{student_id}|{trigger.value}|{day}|{direction or ''}"
    if dedupe_extra:
        raw += f"|{dedupe_extra}"
    return sha256(raw.encode("utf8")).hexdigest()


@dataclass(frozen=True)
class EnqueueResult:
    queued: bool
    reason: str = ""
    notification_id: int | None = None


def render(trigger: Trigger, student: sqlite3.Row, at: datetime,
           extra: dict | None = None) -> str:
    """Build the body. `extra` supplies the fields only some templates need.

    Merged UNDER the standard fields, never over them, so a caller cannot accidentally
    rewrite {first} or {section} with something from a count query.
    """
    body = TEMPLATES[trigger].format(
        **{
            **(extra or {}),
            "first": student["first_name"],
            "section": student["section_name"],
            "time": fmt_time(at),
            "date": at.date().isoformat(),
        }
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
    dedupe_extra: str | None = None,
    extra: dict | None = None,
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

    body = render(trigger, student, at, extra)
    try:
        gsm7.validate(body)
    except (gsm7.NotGSM7, ValueError) as exc:
        # Fail at enqueue time, not silently at double cost on every send.
        return EnqueueResult(False, f"invalid body: {exc}")

    # A belt-and-braces check on the one message that must never describe anything.
    # The template is already vague; this catches the day someone "improves" it to be
    # more informative without reading why it was not.
    if trigger is Trigger.INCIDENT:
        lowered = body.lower()
        leaked = [w for w in INCIDENT_FORBIDDEN if w in lowered]
        if leaked:
            return EnqueueResult(
                False,
                f"incident body must not describe the item; found {leaked}. "
                "The detail is delivered by a person, not by SMS.",
            )

    key = idempotency_key(
        student_id, trigger, at.date().isoformat(), direction, dedupe_extra
    )
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

    ready = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT * FROM notifications
           WHERE status = 'pending'
             AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             AND (queued_at <= ? OR trigger = 'incident')
           ORDER BY guardian_mobile, event_at, id""",
        (ready, cutoff),
    ).fetchall()

    # Incidents skip the coalescing window entirely and are never merged with anything.
    # Two reasons, and both matter: "please contact the school today" should not sit in
    # a queue for three minutes, and merging it into a sibling's arrival message would
    # bury the one sentence the parent needs to act on at the end of a cheerful text.
    urgent = [r for r in rows if r["trigger"] == "incident"]
    rest = [r for r in rows if r["trigger"] != "incident"]

    planned: list[tuple[list[sqlite3.Row], str]] = [([r], r["body"]) for r in urgent]

    by_guardian: dict[str, list[sqlite3.Row]] = {}
    for row in rest:
        by_guardian.setdefault(row["guardian_mobile"], []).append(row)

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
    now = now or datetime.now()

    for group, body in claim_batch(conn, config, now=now):
        mobile = group[0]["guardian_mobile"]
        ids = [r["id"] for r in group]
        coalesce_group = f"cg-{ids[0]}" if len(ids) > 1 else None

        # Before the breaker, so a blocked recipient never consumes spend budget.
        # Empty allowlist means unrestricted; a populated one is what makes it safe to
        # run the real transport against a roster full of real-format numbers.
        if not config.secrets.allows(mobile):
            _mark(conn, ids, "suppressed",
                  error=f"{mobile} is not on SMS_ALLOWLIST")
            stats["suppressed"] += len(ids)
            continue

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
            # by a human from the queue monitor. With a GSM module there is no
            # provider dashboard to reconcile against, so this pile matters more.
            _mark(conn, ids, "unknown", error=result.error)
            stats["unknown"] += len(ids)
        else:
            _retry_or_fail(conn, group, config, result.error, now)
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


def backoff_for(attempts: int, config: Config) -> int:
    """Seconds to wait before attempt number `attempts` may be retried.

    The ladder is clamped at its last entry rather than wrapping, so a long outage
    settles at the longest delay instead of dropping back to 30 seconds.
    """
    ladder = config.notifications.backoff_seconds
    if not ladder:
        return 0
    return ladder[min(attempts - 1, len(ladder) - 1)]


def _retry_or_fail(
    conn: sqlite3.Connection, group: list[sqlite3.Row], config: Config,
    error: str | None, now: datetime,
) -> None:
    """A definite failure goes back to pending, but not immediately.

    Without a delay the row is re-claimed on the next drain tick -- four seconds --
    so retry_limit=5 is spent inside 20 seconds and a brief loss of GSM registration
    permanently fails a message that would have gone out a minute later.
    """
    for row in group:
        attempts = row["retry_count"] + 1
        exhausted = attempts >= config.notifications.retry_limit
        status = "failed" if exhausted else "pending"
        next_at = None
        if not exhausted:
            delay = backoff_for(attempts, config)
            next_at = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE notifications
               SET status = ?, retry_count = ?, last_error = ?, claimed_at = NULL,
                   next_attempt_at = ?
               WHERE id = ?""",
            (status, attempts, error, next_at, row["id"]),
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
