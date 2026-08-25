"""Schema migrations that run against a database created by an earlier version.

schema.sql is all CREATE TABLE IF NOT EXISTS, so it silently does nothing to a table
that already exists. Every change below is therefore invisible to an existing
database unless something explicitly migrates it -- and the failure mode is a
"no such column" on the deployed machine, weeks later.
"""
import sqlite3

import pytest

from trackify.core import db

# The notifications table as it stood before screening existed.
V1_NOTIFICATIONS = """
CREATE TABLE notifications (
    id                  INTEGER PRIMARY KEY,
    student_id          INTEGER NOT NULL,
    guardian_mobile     TEXT    NOT NULL,
    trigger             TEXT    NOT NULL CHECK (trigger IN
                            ('arrival', 'departure', 'late', 'absent')),
    idempotency_key     TEXT    NOT NULL UNIQUE,
    body                TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    retry_count         INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    coalesce_group      TEXT,
    last_error          TEXT,
    event_at            TEXT    NOT NULL,
    queued_at           TEXT    NOT NULL,
    claimed_at          TEXT,
    sent_at             TEXT
)
"""


@pytest.fixture
def old_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(V1_NOTIFICATIONS)
    conn.execute(
        """INSERT INTO notifications (student_id, guardian_mobile, trigger,
               idempotency_key, body, status, retry_count, event_at, queued_at)
           VALUES (1, '639171234567', 'arrival', 'k1', 'hello', 'failed', 3,
                   '2026-08-20T07:00:00', '2026-08-20T07:00:01')"""
    )
    conn.execute(
        """INSERT INTO notifications (student_id, guardian_mobile, trigger,
               idempotency_key, body, status, event_at, queued_at)
           VALUES (2, '639181112222', 'absent', 'k2', 'bye', 'sent',
                   '2026-08-20T16:00:00', '2026-08-20T16:00:01')"""
    )
    return conn


def test_next_attempt_at_reaches_an_existing_database(old_db):
    assert "next_attempt_at" not in {
        r["name"] for r in old_db.execute("PRAGMA table_info(notifications)")
    }
    db.init_db(old_db)
    assert "next_attempt_at" in {
        r["name"] for r in old_db.execute("PRAGMA table_info(notifications)")
    }


def test_incident_trigger_needs_a_table_rebuild_and_gets_one(old_db):
    """SQLite cannot ALTER a CHECK constraint, so widening it means copying the table."""
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute(
            """INSERT INTO notifications (student_id, guardian_mobile, trigger,
                   idempotency_key, body, event_at, queued_at)
               VALUES (1, '639', 'incident', 'k3', 'x', 'a', 'b')"""
        )

    db.init_db(old_db)

    old_db.execute(
        """INSERT INTO notifications (student_id, guardian_mobile, trigger,
               idempotency_key, body, event_at, queued_at)
           VALUES (1, '639', 'incident', 'k3', 'x', 'a', 'b')"""
    )


def test_the_rebuild_keeps_every_row_exactly(old_db):
    """A migration that quietly drops a pending notification loses a parent's text."""
    before = [
        tuple(r) for r in old_db.execute(
            "SELECT id, trigger, status, retry_count, body FROM notifications ORDER BY id"
        )
    ]
    db.init_db(old_db)
    after = [
        tuple(r) for r in old_db.execute(
            "SELECT id, trigger, status, retry_count, body FROM notifications ORDER BY id"
        )
    ]
    assert before == after


def test_the_rebuild_keeps_the_idempotency_constraint(old_db):
    """The UNIQUE index is what stops a parent being texted twice. Dropping it during
    a rebuild would be a silent, much later failure."""
    db.init_db(old_db)
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute(
            """INSERT INTO notifications (student_id, guardian_mobile, trigger,
                   idempotency_key, body, event_at, queued_at)
               VALUES (9, '639', 'late', 'k1', 'dup', 'a', 'b')"""
        )


def test_the_rebuild_recreates_the_indexes(old_db):
    db.init_db(old_db)
    names = {
        r[0] for r in old_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='notifications'"
        )
    }
    assert {"idx_notif_status", "idx_notif_guardian"} <= names


def test_running_init_twice_rebuilds_only_once(old_db):
    assert db._widen_notification_triggers(old_db) is True
    assert db._widen_notification_triggers(old_db) is False


def test_a_fresh_database_is_never_rebuilt(conn):
    """A new database already has the widened CHECK, so the migration is a no-op."""
    assert db._widen_notification_triggers(conn) is False


def test_every_table_is_covered_by_the_demo_reset(conn):
    """seed_demo --reset must delete children before parents. A new table that nobody
    added to that list shows up here rather than as a bare 'FOREIGN KEY constraint
    failed' the first time someone reseeds after a demo."""
    import re
    from pathlib import Path

    source = Path("scripts/seed_demo.py").read_text(encoding="utf8")
    block = source.split("for table in (")[1].split("):")[0]
    listed = set(re.findall(r'"(\w+)"', block))

    in_db = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert in_db - listed == set(), f"not cleared by --reset: {in_db - listed}"
