"""SQLite access. WAL mode, per-thread connections, schema bootstrap.

Threading rule (see the plan): a sqlite3.Connection must never be shared across
threads. Every thread calls connect() and gets its own.
"""

from __future__ import annotations

import re
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


def ensure_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """Add missing columns to an existing table. Idempotent.

    schema.sql is all CREATE TABLE IF NOT EXISTS, which silently does nothing when the
    table already exists -- so a column added to the schema never reaches a database
    that predates it. That failure is quiet and shows up later as a confusing
    "no such column" at query time, on the deployed machine rather than here.

    Additive only: SQLite cannot drop or retype a column without rebuilding the table,
    and anything needing that deserves a real migration written by hand.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# Columns added after the first release. Keep the declaration identical to schema.sql.
MIGRATIONS: dict[str, dict[str, str]] = {
    "notifications": {"next_attempt_at": "TEXT"},
    "attendance_days": {"corrected_by_name": "TEXT", "correction_type": "TEXT"},
    "audit_log": {"actor_name": "TEXT"},
    # NOT NULL needs a non-null default for ALTER TABLE ADD COLUMN; existing rows read
    # back as an unfloored composite, which is what they were.
    "risk_scores": {"incidents": "INTEGER NOT NULL DEFAULT 0",
                    "band_source": "TEXT NOT NULL DEFAULT 'composite'"},
}


# Every trigger the current schema allows, in schema.sql's order. Widening this list
# is a table rebuild, not an ALTER -- see _widen_notification_triggers.
NOTIFICATION_TRIGGERS = ("arrival", "departure", "late", "absent", "incident",
                         "summary", "reminder")


def _widen_notification_triggers(conn: sqlite3.Connection) -> bool:
    """Bring an older database's trigger CHECK up to the current set.

    Ran first for trigger='incident' on databases created before screening existed,
    and again for 'summary' and 'reminder'. Driven off NOTIFICATION_TRIGGERS rather
    than a hardcoded pair, so the next addition is a one-line change here.

    This cannot be done with ALTER TABLE. SQLite has no way to modify a CHECK
    constraint, so widening the allowed set means rebuilding the table and copying
    every row -- the documented 12-step procedure. Returns True if it rebuilt.

    Everything here is deliberate:
      * foreign_keys OFF around the rename, or the child tables' references would be
        rewritten to point at the temporary table
      * legacy_alter_table OFF (the default) so the rename does NOT rewrite references
      * one transaction, so a crash halfway leaves the original table intact
      * indexes recreated afterwards, because dropping the table drops them too
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'"
    ).fetchone()
    if sql is None:
        return False

    missing = [name for name in NOTIFICATION_TRIGGERS if f"'{name}'" not in sql[0]]
    if not missing:
        return False

    # Rewrite the whole tuple rather than appending, so the result is the same however
    # many values were missing. The CHECK and the column set then match schema.sql; the
    # DDL text does not quite, because SQLite requotes the table name on RENAME and
    # ensure_columns appends its columns at the end. Cosmetic, and fixing it would mean
    # another full rebuild.
    present = [name for name in NOTIFICATION_TRIGGERS if f"'{name}'" in sql[0]]
    old_list = ", ".join(f"'{name}'" for name in present)
    # Wrapped after the fifth value to match schema.sql's own layout, so a migrated
    # database and a freshly created one produce byte-identical DDL.
    new_list = (", ".join(f"'{n}'" for n in NOTIFICATION_TRIGGERS[:5])
                + ",\n                             "
                + ", ".join(f"'{n}'" for n in NOTIFICATION_TRIGGERS[5:]))
    # The table name has to be matched with a pattern, not a literal. After the FIRST
    # rebuild SQLite stores the definition as CREATE TABLE "notifications" -- quoted,
    # and without the IF NOT EXISTS -- so a literal replace silently matches nothing
    # and the rebuild then fails with 'table notifications already exists'. Only the
    # second widening ever hits that, which is why it survived the first one.
    new_sql = re.sub(
        r'^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?"?notifications"?',
        "CREATE TABLE notifications_new",
        sql[0].replace(old_list, new_list, 1),
        count=1,
        flags=re.IGNORECASE,
    )
    if "notifications_new" not in new_sql:
        raise RuntimeError(
            "could not rename the notifications table in its own definition; "
            f"refusing to rebuild. Definition began: {sql[0][:60]!r}"
        )

    columns = [r["name"] for r in conn.execute("PRAGMA table_info(notifications)")]
    collist = ", ".join(columns)

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(new_sql)
        conn.execute(
            f"INSERT INTO notifications_new ({collist}) SELECT {collist} FROM notifications"
        )
        conn.execute("DROP TABLE notifications")
        conn.execute("ALTER TABLE notifications_new RENAME TO notifications")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_status "
                     "ON notifications(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_guardian "
                     "ON notifications(guardian_mobile, status)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # A rebuild that broke a reference is worse than no rebuild; say so loudly.
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(
            f"notifications rebuild left {len(broken)} broken foreign key reference(s)"
        )
    return True


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf8"))
    for table, columns in MIGRATIONS.items():
        ensure_columns(conn, table, columns)
    _widen_notification_triggers(conn)


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
    actor_name: str | None = None,
) -> None:
    """Append one audit row.

    actor_id and actor_name are deliberately separate. actor_id is a verified account;
    actor_name is what a person typed into a box behind a shared password. Storing a
    typed name in actor_id would make an unverified claim look like an authenticated
    one, which is exactly the confusion an audit trail exists to prevent.
    """
    conn.execute(
        """INSERT INTO audit_log
           (actor_id, actor_name, action, entity_type, entity_id,
            old_value, new_value, reason, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor_id, actor_name, action, entity_type,
         None if entity_id is None else str(entity_id),
         old_value, new_value, reason, utcnow()),
    )
