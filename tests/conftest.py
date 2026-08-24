import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trackify.core import db
from trackify.core.config import load_config


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(db.SCHEMA.read_text(encoding="utf8"))
    yield connection
    connection.close()


@pytest.fixture
def section(conn):
    cur = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Rizal', 7)"
    )
    return cur.lastrowid


@pytest.fixture
def make_student(conn, section):
    counter = {"n": 0}

    def _make(guardian_mobile="639171234567", first="Juan", last="Dela Cruz"):
        counter["n"] += 1
        cur = conn.execute(
            """INSERT INTO students
               (lrn, first_name, last_name, section_id, guardian_name,
                guardian_mobile, consent_on_file, created_at)
               VALUES (?, ?, ?, ?, 'Maria', ?, 1, ?)""",
            (f"13658412{counter['n']:04d}", first, last, section,
             guardian_mobile, db.utcnow()),
        )
        return cur.lastrowid

    return _make


@pytest.fixture
def student(make_student):
    return make_student()


def at(hour, minute=0, day="2026-09-01"):
    """Build a datetime on a fixed school day. Minutes may exceed 59 and roll over."""
    base = datetime.fromisoformat(f"{day}T00:00:00")
    return base + timedelta(hours=hour, minutes=minute)
