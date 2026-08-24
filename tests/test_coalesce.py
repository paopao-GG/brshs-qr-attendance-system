"""Regression tests for two defects an end-to-end run exposed.

Both were invisible to the unit tests because those only ever queued events at one
moment in time.
"""

from datetime import datetime, timedelta

from trackify.core.attendance import Trigger
from trackify.notify import coalesce, gsm7, queue
from trackify.notify.limits import SpendBreaker
from trackify.notify.provider import ConsoleProvider

from .conftest import at

WINDOW = timedelta(minutes=3)


def backdate(conn, minutes=10):
    conn.execute(
        "UPDATE notifications SET queued_at = ?",
        ((datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds"),),
    )


def breaker(conn, config):
    return SpendBreaker(conn, config.limits.daily_message_cap,
                        config.limits.per_recipient_daily_cap)


def pending(conn):
    return conn.execute(
        "SELECT * FROM notifications WHERE status='pending' ORDER BY event_at, id"
    ).fetchall()


# --- defect 1: morning and afternoon must not merge -------------------------

def test_arrival_and_departure_are_separate_messages(conn, make_student, config):
    """A 7am arrival merged with a 4pm departure is wrong for both."""
    juan = make_student(guardian_mobile="639171234567", first="Juan")
    queue.enqueue(conn, juan, Trigger.ARRIVAL, at(7, 12), config, direction="in")
    queue.enqueue(conn, juan, Trigger.DEPARTURE, at(16, 5), config, direction="out")
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))

    assert stats["messages"] == 2, "morning and afternoon must not be one text"
    bodies = [b for _, b in provider.sent]
    assert any("arrived" in b and "left school" not in b for b in bodies)
    assert any("left school" in b and "arrived" not in b for b in bodies)


def test_outage_backlog_still_splits_by_event_time(conn, make_student, config):
    """The exact production case: network down all morning, flushes at 4pm."""
    mobile = "639171234567"
    juan = make_student(guardian_mobile=mobile, first="Juan")
    maria = make_student(guardian_mobile=mobile, first="Maria")

    for sid, when, trig, d in [
        (juan, at(7, 12), Trigger.ARRIVAL, "in"),
        (maria, at(7, 14), Trigger.ARRIVAL, "in"),
        (juan, at(16, 5), Trigger.DEPARTURE, "out"),
        (maria, at(16, 7), Trigger.DEPARTURE, "out"),
    ]:
        queue.enqueue(conn, sid, trig, when, config, direction=d)
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))

    assert stats["sent"] == 4
    assert stats["messages"] == 2, "two clusters: morning pair, afternoon pair"

    morning = next(b for _, b in provider.sent if "arrived" in b)
    afternoon = next(b for _, b in provider.sent if "left school" in b)
    assert "Juan" in morning and "Maria" in morning
    assert "Juan" in afternoon and "Maria" in afternoon


def test_siblings_within_window_still_merge(conn, make_student, config):
    """The original behaviour must survive the fix."""
    mobile = "639171234567"
    a = make_student(guardian_mobile=mobile, first="Juan")
    b = make_student(guardian_mobile=mobile, first="Maria")
    queue.enqueue(conn, a, Trigger.ARRIVAL, at(7, 12), config, direction="in")
    queue.enqueue(conn, b, Trigger.ARRIVAL, at(7, 14), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))
    assert stats["messages"] == 1
    assert stats["sent"] == 2


# --- defect 2: overflow splits, never silently drops ------------------------

def test_overflow_splits_instead_of_truncating(conn, make_student, config):
    """Truncation deleted a child's departure from mid-message. Never again."""
    mobile = "639171234567"
    names = ["Bartolome", "Concepcion", "Maximiliano", "Purificacion", "Buenaventura"]
    for name in names:
        sid = make_student(guardian_mobile=mobile, first=name)
        queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 12), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    stats = queue.drain(conn, provider, config, breaker(conn, config))

    assert stats["sent"] == 5, "every queued row accounted for"
    assert stats["messages"] > 1, "must split rather than truncate"

    combined = " ".join(b for _, b in provider.sent)
    for name in names:
        assert name in combined, f"{name} was silently dropped"

    for _, body in provider.sent:
        assert gsm7.septets(body) <= gsm7.SINGLE_SEGMENT
        gsm7.validate(body)


def test_no_message_ends_mid_word(conn, make_student, config):
    mobile = "639171234567"
    for i in range(6):
        sid = make_student(guardian_mobile=mobile, first=f"Verylongname{i}")
        queue.enqueue(conn, sid, Trigger.ARRIVAL, at(7, 12), config, direction="in")
    backdate(conn)

    provider = ConsoleProvider()
    queue.drain(conn, provider, config, breaker(conn, config))
    for _, body in provider.sent:
        assert body.endswith("."), f"dangling body: {body!r}"


# --- grouping unit tests ----------------------------------------------------

def _row(event_at, body="TRACKIFY: X (7-A) arrived 7:00 AM on 2026-09-01.", rid=1):
    return {"event_at": event_at, "body": body, "id": rid}


def test_group_rows_clusters_by_event_proximity():
    rows = [
        _row("2026-09-01T07:12:00", rid=1),
        _row("2026-09-01T07:14:00", rid=2),
        _row("2026-09-01T16:05:00", rid=3),
    ]
    clusters = coalesce.group_rows(rows, WINDOW)
    assert [len(c) for c in clusters] == [2, 1]


def test_group_rows_single_row():
    assert len(coalesce.group_rows([_row("2026-09-01T07:12:00")], WINDOW)) == 1


def test_pack_keeps_everything():
    rows = [_row("2026-09-01T07:12:00", rid=i) for i in range(10)]
    packed = coalesce.pack(rows)
    assert sum(len(chunk) for chunk in packed) == 10
