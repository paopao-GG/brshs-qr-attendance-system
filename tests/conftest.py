import sqlite3
from datetime import datetime, timedelta

import pytest

from trackify.core import db
from trackify.core.config import load_config
from trackify.core.qrcodes import encode

TEST_SECRET = "test-secret"

# Deliberately NOT equal to the row id. A card is keyed on the LRN, and if the fixture
# made the two the same number, every scan test would pass whether or not the lookup was
# keyed correctly -- the suite would be blind to the exact confusion it exists to catch.
LRN_BASE = 136584120000


def lrn_for(student_id: int) -> str:
    """The LRN make_student() gives the student with this row id."""
    return str(LRN_BASE + student_id)


def payload_for(student_id: int, secret: str = TEST_SECRET) -> str:
    """The QR payload printed on that student's card.

    Built the way qr-generator.exe builds it: signed over the LRN, never the row id.
    """
    return encode(int(lrn_for(student_id)), secret)


@pytest.fixture
def config():
    """Config with the developer's machine-local settings neutralised.

    load_config() reads the real .env, so without this a value there silently changes
    test outcomes on one machine and not another. Setting SMS_ALLOWLIST for live testing
    did exactly that: every queue test that expected a message to be sent started seeing
    it suppressed instead.

    Tests that care about the allowlist set it explicitly with dataclasses.replace.
    """
    import dataclasses

    cfg = load_config()
    return dataclasses.replace(
        cfg, secrets=dataclasses.replace(cfg.secrets, allowlist=())
    )


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
def adviser(conn):
    cur = conn.execute(
        """INSERT INTO users (username, password_hash, role, full_name, created_at)
           VALUES ('adviser', 'x', 'adviser', 'Tricia San Jose', ?)""",
        (db.utcnow(),),
    )
    return cur.lastrowid


@pytest.fixture
def make_student(conn, section):
    counter = {"n": 0}

    def _make(guardian_mobile="639171234567", first="Juan", last="Dela Cruz",
              sex=None):
        # sex defaults to None -- "not recorded" -- because that is what every student
        # created before the SF2 export existed actually is, and a fixture that quietly
        # filled it in would hide the case the export has to handle.
        counter["n"] += 1
        cur = conn.execute(
            """INSERT INTO students
               (lrn, first_name, last_name, section_id, guardian_name,
                guardian_mobile, sex, consent_on_file, created_at)
               VALUES (?, ?, ?, ?, 'Maria', ?, ?, 1, ?)""",
            (f"13658412{counter['n']:04d}", first, last, section,
             guardian_mobile, sex, db.utcnow()),
        )
        # Set from the real row id rather than the counter: ids are only sequential in a
        # fresh database, and payload_for() has to agree with what is stored no matter
        # what else a test inserted first.
        conn.execute("UPDATE students SET lrn = ? WHERE id = ?",
                     (lrn_for(cur.lastrowid), cur.lastrowid))
        return cur.lastrowid

    return _make


@pytest.fixture
def student(make_student):
    return make_student()


def at(hour, minute=0, day="2026-09-01"):
    """Build a datetime on a fixed school day. Minutes may exceed 59 and roll over."""
    base = datetime.fromisoformat(f"{day}T00:00:00")
    return base + timedelta(hours=hour, minutes=minute)
