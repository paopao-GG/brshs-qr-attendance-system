"""SQLite access. WAL mode, per-thread connections, schema bootstrap.

Threading rule (see the plan): a sqlite3.Connection must never be shared across
threads. Every thread calls connect() and gets its own.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT

DEFAULT_DB = PROJECT_ROOT / "data" / "trackify.db"
SCHEMA = Path(__file__).with_name("schema.sql")

_local = threading.local()


def utcnow() -> str:
    """Timestamps are ISO-8601 with explicit offset, so a DST or TZ change never
    silently reorders records."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return this thread's connection, creating it on first use."""
    path = Path(db_path) if db_path else DEFAULT_DB
    key = str(path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if key not in cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, isolation_level=None)  # explicit transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        cache[key] = conn
        _local.conns = cache
    return cache[key]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf8"))


def close_all() -> None:
    for conn in (getattr(_local, "conns", None) or {}).values():
        conn.close()
    _local.conns = {}


class transaction:
    """Context manager for an explicit transaction. Rolls back on exception.

    Used for anything that must be all-or-nothing, notably CSV import.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def audit(
    conn: sqlite3.Connection,
    action: str,
    *,
    actor_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    reason: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log
           (actor_id, action, entity_type, entity_id, old_value, new_value, reason, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor_id, action, entity_type,
         None if entity_id is None else str(entity_id),
         old_value, new_value, reason, utcnow()),
    )
