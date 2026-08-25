"""The three de-duplication mechanisms, plus the spend circuit breaker.

These are the tests the plan calls out as the important ones: a duplicate SMS to a
parent is the failure mode that erodes trust in the whole system.
"""

from datetime import datetime, timedelta

import pytest

from trackify.core.attendance import Trigger, record_scan
from trackify.notify import queue
from trackify.notify.limits import BreakerTripped, SpendBreaker, TokenBucket
from trackify.notify.provider import ConsoleProvider, NotificationProvider, SendResult

from .conftest import at


# --- helpers ----------------------------------------------------------------

class FlakyProvider(NotificationProvider):
    name = "flaky"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def send(self, recipient, body):
        self.calls.append((recipient, body))
        return self.results.pop(0) if self.results else SendResult(ok=True)


def breaker(conn, config):
    return SpendBreaker(conn, config.limits.daily_message_cap,
                        config.limits.per_recipient_daily_cap)


def past(minutes=10):
    """A queued_at old enough to clear the coalescing window."""
    return datetime.now() - timedelta(minutes=minutes)


def backdate(conn, minutes=10):
    conn.execute("UPDATE notifications SET queued_at = ?",
                 ((datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds"),))


# --- idempotency ------------------------------------------------------------

def test_enqueueing_the_same_event_twice_is_a_noop(conn, student, config):
    first = queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    second = queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")

    assert first.queued
    assert not second.queued
    assert "idempotent" in second.reason
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_arrival_and_departure_are_distinct_keys(conn, student, config):
    assert queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config,
                         direction="in").queued
    assert queue.enqueue(conn, student, Trigger.DEPARTURE, at(16, 0), config,
                         direction="out").queued
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_crash_between_send_and_record_does_not_duplicate(conn, student, config):
    """Plan verification step 2. Worker dies mid-send; restart must not resend."""
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    backdate(conn)

    # Worker claims the row, then the process dies before recording the outcome.
    claimed = queue.claim_batch(conn, config)
    assert len(claimed) == 1
    assert conn.execute(
        "SELECT status FROM notifications").fetchone()[0] == "sending"

    # Simulate the claim having gone stale, then restart recovery.
    conn.execute("UPDATE notifications SET claimed_at = ?",
                 ((datetime.now() - timedelta(minutes=30)).isoformat(),))
    recovered = queue.reconcile_stale(conn)

    assert recovered == 1
    status = conn.execute("SELECT status FROM notifications").fetchone()[0]
    assert status == "unknown", "must not silently return to pending and resend"

    # A second drain must send nothing.
    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))
    assert stats["sent"] == 0
    assert provider.sent == []


def test_ambiguous_send_is_not_retried(conn, student, config):
    """A timeout after the write may have delivered. At-most-once is the right bias."""
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    backdate(conn)

    provider = FlakyProvider([SendResult(ok=False, ambiguous=True, error="read timeout")])
    stats = queue.drain(conn, provider, config, breaker(conn, config))

    assert stats["unknown"] == 1
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "unknown"

    # Draining again must not touch it.
    again = queue.drain(conn, ConsoleProvider(), config, breaker(conn, config))
    assert again["messages"] == 0


# --- guardian coalescing ----------------------------------------------------

def test_two_siblings_produce_one_message(conn, make_student, config):
    """Plan verification step 3. The mechanism parents actually notice."""
    juan = make_student(guardian_mobile="639171234567", first="Juan")
    maria = make_student(guardian_mobile="639171234567", first="Maria")

    queue.enqueue(conn, juan, Trigger.ARRIVAL, at(7, 12), config, direction="in")
    queue.enqueue(conn, maria, Trigger.ARRIVAL, at(7, 14), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))

    assert stats["messages"] == 1, "one outbound message, not two"
    assert stats["sent"] == 2, "both notification rows marked sent"

    _, body = provider.sent[0]
    assert "Juan" in body and "Maria" in body
    assert body.startswith("TRACKIFY:")

    groups = {r[0] for r in conn.execute(
        "SELECT coalesce_group FROM notifications")}
    assert len(groups) == 1 and None not in groups


def test_different_guardians_are_not_merged(conn, make_student, config):
    a = make_student(guardian_mobile="639171111111", first="Ana")
    b = make_student(guardian_mobile="639172222222", first="Ben")
    queue.enqueue(conn, a, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    queue.enqueue(conn, b, Trigger.ARRIVAL, at(7, 1), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))
    assert stats["messages"] == 2


def test_coalescing_window_holds_recent_rows_back(conn, student, config):
    """A row queued seconds ago waits, so a sibling scanning next can join it."""
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    # queued_at is now; window is 3 minutes, so nothing should be claimable yet.
    assert queue.claim_batch(conn, config) == []


def test_coalesced_body_stays_within_one_segment(conn, make_student, config):
    from trackify.notify import gsm7
    mobile = "639171234567"
    for i in range(5):
        sid = make_student(guardian_mobile=mobile, first=f"Child{i}",
                           last="Verylongsurnamehere")
        queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, i), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    queue.drain(conn, provider, config, breaker(conn, config))

    _, body = provider.sent[0]
    assert gsm7.septets(body) <= 160
    gsm7.validate(body)


# --- consent and policy gates -----------------------------------------------

def test_no_consent_means_no_notification(conn, make_student, config):
    sid = make_student()
    conn.execute("UPDATE students SET consent_on_file = 0 WHERE id = ?", (sid,))
    result = queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    assert not result.queued
    assert "consent" in result.reason


def test_missing_guardian_mobile_is_attendance_only(conn, make_student, config):
    sid = make_student(guardian_mobile=None)
    result = queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    assert not result.queued
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_optout_respected(conn, make_student, config):
    sid = make_student()
    conn.execute("UPDATE students SET notify_optin = 0 WHERE id = ?", (sid,))
    assert not queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 0), config,
                             direction="in").queued


# --- circuit breaker --------------------------------------------------------

def test_loop_bug_trips_the_daily_cap(conn, make_student, config):
    """Plan verification step 4. A runaway trigger must cost PHP 20, not PHP 3,000."""
    import dataclasses
    tight = dataclasses.replace(config, limits=dataclasses.replace(
        config.limits, daily_message_cap=5, per_recipient_daily_cap=100))

    for i in range(50):
        sid = make_student(guardian_mobile=f"6391712{i:05d}", first=f"S{i}")
        queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 0), tight, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    brk = breaker(conn, tight)
    with pytest.raises(BreakerTripped, match="Daily SMS cap"):
        queue.drain(conn, provider, tight, brk)

    assert len(provider.sent) <= 5, "must halt at the cap, not send all 50"
    assert brk.state().tripped


def test_per_recipient_cap_suppresses_without_halting(conn, make_student, config):
    import dataclasses
    tight = dataclasses.replace(config, limits=dataclasses.replace(
        config.limits, per_recipient_daily_cap=1))

    mobile = "639171234567"
    a = make_student(guardian_mobile=mobile, first="Ana")
    queue.enqueue(conn, a, Trigger.ARRIVAL, at(7, 0), tight, direction="in")
    backdate(conn)
    queue.drain(conn, ConsoleProvider(), tight, breaker(conn, tight))

    b = make_student(guardian_mobile=mobile, first="Ben")
    queue.enqueue(conn, b, Trigger.DEPARTURE, at(16, 0), tight, direction="out")
    backdate(conn)
    stats = queue.drain(conn, ConsoleProvider(), tight, breaker(conn, tight))

    assert stats["suppressed"] == 1
    assert stats["sent"] == 0


def test_breaker_survives_restart(conn, config):
    """Persisted counts, so a crash loop cannot reset the budget."""
    brk = breaker(conn, config)
    brk.record_sent(10)
    fresh = breaker(conn, config)
    assert fresh.state().sent_today == 10


# --- token bucket -----------------------------------------------------------

def test_token_bucket_drops_a_flood():
    bucket = TokenBucket(rate_per_sec=5, capacity=5)
    accepted = sum(bucket.try_acquire() for _ in range(100))
    assert accepted == 5, "a stuck scanner cannot flood the queue"


def test_token_bucket_refills():
    import time
    bucket = TokenBucket(rate_per_sec=100, capacity=1)
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    time.sleep(0.05)
    assert bucket.try_acquire()


# --- integration with the scan path -----------------------------------------

def test_scan_queues_but_never_sends_inline(conn, student, config):
    """The kiosk must not wait on the network."""
    result = record_scan(conn, student, at(7, 0), config)
    queued = queue.enqueue_for_scan(conn, result, config)

    assert any(q.queued for q in queued)
    assert conn.execute(
        "SELECT status FROM notifications").fetchone()[0] == "pending"


def test_debounced_scan_queues_nothing(conn, student, config):
    record_scan(conn, student, at(7, 0), config)
    second = record_scan(conn, student, at(7, 1), config)
    assert queue.enqueue_for_scan(conn, second, config) == []


def test_late_arrival_sends_late_not_arrival(conn, student, config):
    result = record_scan(conn, student, at(7, 45), config)
    queue.enqueue_for_scan(conn, result, config)
    trigger = conn.execute("SELECT trigger FROM notifications").fetchone()[0]
    assert trigger == "late"


def test_unsent_count_surfaces_backlog(conn, make_student, config):
    for i in range(3):
        sid = make_student(guardian_mobile=f"6391711111{i:02d}")
        queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    assert queue.unsent_count(conn) == 3


# -- recipient allowlist ----------------------------------------------------

def allowlisted(config, *numbers):
    import dataclasses
    return dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, allowlist=numbers)
    )


def test_allowlist_suppresses_everyone_not_on_it(conn, config, make_student):
    """The demo roster holds 19 valid-format Philippine numbers, and an unli-text SIM
    has no cost brake. Without this, one stray run texts all of them."""
    student = make_student(guardian_mobile="639998887777")
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 10), config, direction="in")
    backdate(conn)

    cfg = allowlisted(config, "639171234567")
    provider = FlakyProvider([])
    stats = queue.drain(conn, provider, cfg, breaker(conn, cfg))

    assert provider.calls == [], "a number off the allowlist must not be texted"
    assert stats["suppressed"] == 1


def test_allowlisted_number_is_sent(conn, config, make_student):
    student = make_student(guardian_mobile="639171234567")
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 10), config, direction="in")
    backdate(conn)

    cfg = allowlisted(config, "639171234567")
    provider = FlakyProvider([])
    stats = queue.drain(conn, provider, cfg, breaker(conn, cfg))

    assert len(provider.calls) == 1
    assert stats["sent"] == 1


def test_empty_allowlist_restricts_nothing(conn, config, make_student):
    """Correct for production. It is the testing case that needs the list set."""
    student = make_student(guardian_mobile="639998887777")
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 10), config, direction="in")
    backdate(conn)

    provider = FlakyProvider([])
    assert queue.drain(conn, provider, config, breaker(conn, config))["sent"] == 1


def test_suppressed_recipient_does_not_consume_spend_budget(conn, config, make_student):
    """The allowlist check sits before the breaker, so a blocked number costs nothing
    against the daily cap -- and cannot mask a genuine runaway."""
    student = make_student(guardian_mobile="639998887777")
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 10), config, direction="in")
    backdate(conn)

    cfg = allowlisted(config, "639171234567")
    brk = SpendBreaker(conn, daily_cap=5, per_recipient_cap=5)
    queue.drain(conn, FlakyProvider([]), cfg, brk)

    assert brk.state().sent_today == 0


def test_config_fixture_does_not_inherit_the_local_allowlist(config):
    """Regression: the config fixture calls load_config(), which reads the real .env.
    Setting SMS_ALLOWLIST there for live testing suppressed every queue test that
    expected a send. A developer's machine-local setting must never decide whether the
    suite passes."""
    assert config.secrets.allowlist == ()


# --- retry backoff ----------------------------------------------------------

def test_a_failure_is_not_retried_on_the_very_next_tick(conn, student, config):
    """The gap this closes: without a delay the worker re-claims the row four seconds
    later, so retry_limit=5 is spent inside 20 seconds and a brief loss of GSM
    registration permanently fails a message that would have gone out a minute on."""
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    backdate(conn)
    provider = FlakyProvider([SendResult(ok=False, error="+CMS ERROR: 331")])

    now = datetime.now()
    queue.drain(conn, provider, config, breaker(conn, config), now=now)

    row = conn.execute("SELECT status, retry_count, next_attempt_at "
                       "FROM notifications").fetchone()
    assert (row["status"], row["retry_count"]) == ("pending", 1)

    # Four seconds on -- the worker's tick -- it must still be untouchable.
    assert queue.claim_batch(conn, config, now=now + timedelta(seconds=4)) == []
    # Past the first rung of the ladder, it is claimable again.
    delay = config.notifications.backoff_seconds[0]
    assert queue.claim_batch(conn, config, now=now + timedelta(seconds=delay + 1))


def test_backoff_ladder_lengthens_then_holds_at_its_last_rung(config):
    ladder = config.notifications.backoff_seconds
    assert [queue.backoff_for(n, config) for n in (1, 2, 3)] == ladder[:3]
    # Clamped, not wrapped: a long outage settles at the longest delay rather than
    # dropping back to 30 seconds.
    assert queue.backoff_for(len(ladder) + 5, config) == ladder[-1]


def test_retry_limit_still_ends_in_failed(conn, student, config):
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    backdate(conn)
    provider = FlakyProvider([SendResult(ok=False, error="nope")] * 10)

    now = datetime.now()
    for attempt in range(config.notifications.retry_limit):
        queue.drain(conn, provider, config, breaker(conn, config),
                    now=now + timedelta(hours=attempt))

    row = conn.execute("SELECT status, retry_count FROM notifications").fetchone()
    assert row["status"] == "failed"
    assert row["retry_count"] == config.notifications.retry_limit


def test_a_successful_send_clears_no_backoff_because_it_never_set_one(conn, student, config):
    queue.enqueue(conn, student, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    backdate(conn)
    queue.drain(conn, ConsoleProvider(), config, breaker(conn, config))

    row = conn.execute("SELECT status, next_attempt_at FROM notifications").fetchone()
    assert row["status"] == "sent"
    assert row["next_attempt_at"] is None


# --- incident notifications -------------------------------------------------

def test_incident_skips_the_coalescing_window(conn, student, config):
    """'Please contact the school today' must not sit in a queue for three minutes."""
    queue.enqueue(conn, student, Trigger.INCIDENT, datetime.now(), config,
                  dedupe_extra="se1")
    # No backdate: the row was queued a moment ago and would normally be held back.
    claimed = queue.claim_batch(conn, config)
    assert len(claimed) == 1


def test_an_arrival_queued_now_is_still_held_back(conn, student, config):
    """The contrast that proves the bypass is specific to incidents."""
    queue.enqueue(conn, student, Trigger.ARRIVAL, datetime.now(), config, direction="in")
    assert queue.claim_batch(conn, config) == []


def test_incident_is_never_merged_into_a_sibling_message(conn, make_student, config):
    """Burying 'contact the school' at the end of a cheerful arrival text loses the
    one sentence the parent needs to act on."""
    a = make_student(first="Juan", guardian_mobile="639171234567")
    b = make_student(first="Maria", guardian_mobile="639171234567")

    queue.enqueue(conn, a, Trigger.ARRIVAL, at(7, 0), config, direction="in")
    queue.enqueue(conn, b, Trigger.ARRIVAL, at(7, 1), config, direction="in")
    queue.enqueue(conn, a, Trigger.INCIDENT, at(7, 2), config, dedupe_extra="se1")
    backdate(conn)

    claimed = queue.claim_batch(conn, config)
    bodies = [body for _, body in claimed]

    incident = [b for b in bodies if "contact the school" in b]
    assert len(incident) == 1
    assert "arrived" not in incident[0]          # stands alone
    assert len(claimed) == 2                     # the two siblings still merged


def test_a_body_describing_the_item_is_refused(conn, student, config, monkeypatch):
    """If someone 'improves' the template to be more informative, enqueue refuses it."""
    monkeypatch.setitem(
        queue.TEMPLATES, Trigger.INCIDENT,
        "TRACKIFY: {first} ({section}) was found with a knife.",
    )
    result = queue.enqueue(conn, student, Trigger.INCIDENT, at(7, 0), config,
                           dedupe_extra="se1")

    assert not result.queued
    assert "must not describe the item" in result.reason
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_idempotency_keys_are_unchanged_by_the_new_parameter(conn, student, config):
    """Existing pending rows must not be orphaned by the dedupe_extra addition."""
    without = queue.idempotency_key(1, Trigger.ARRIVAL, "2026-09-01", "in")
    explicit_none = queue.idempotency_key(1, Trigger.ARRIVAL, "2026-09-01", "in", None)
    assert without == explicit_none
    assert without != queue.idempotency_key(1, Trigger.ARRIVAL, "2026-09-01", "in", "se1")
