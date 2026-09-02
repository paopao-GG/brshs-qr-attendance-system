"""scripts/clean_data.py: trims the simulated term to Aug 10-21 2026 and removes
leftover development rows dated after it, without touching anything inside the range.
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trackify.core import db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clean_data.py"

CUTOFF = "2026-08-21"
AFTER = "2026-08-27"


@pytest.fixture
def scratch_db(tmp_path):
    """A tiny database with rows on both sides of the cutoff: one clean attendance day
    inside the simulated term, and a stray manual scan/screening/attendance row dated
    after it -- the shape the live database was actually in."""
    path = tmp_path / "scratch.db"
    conn = db.connect(path)
    db.init_db(conn)

    section = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Rizal', 7)"
    ).lastrowid
    student = conn.execute(
        """INSERT INTO students
           (lrn, first_name, last_name, section_id, guardian_name,
            guardian_mobile, consent_on_file, created_at)
           VALUES ('136584120001', 'Juan', 'Dela Cruz', ?, 'Maria',
                   '639171234567', 1, ?)""",
        (section, db.utcnow()),
    ).lastrowid

    # Inside the term: one clean attendance day, nothing to remove.
    conn.execute(
        """INSERT INTO attendance_days (student_id, date, status, flags, created_at)
           VALUES (?, ?, 'present', '', ?)""",
        (student, CUTOFF, db.utcnow()),
    )
    conn.execute(
        "INSERT INTO school_days (date, entry_open, late_threshold, dismissal_time, "
        "early_departure_cutoff) VALUES (?, '06:15', '07:15', '16:00', '15:30')",
        (CUTOFF,),
    )

    # After the term: the stray manual test row and everything under it.
    scan = conn.execute(
        """INSERT INTO scan_events (student_id, scanned_at, date, direction, method)
           VALUES (?, ?, ?, 'in', 'manual')""",
        (student, AFTER + "T01:43:10", AFTER),
    ).lastrowid
    conn.execute(
        """INSERT INTO screening_events (scan_event_id, occurred_at, outcome)
           VALUES (?, ?, 'clear')""",
        (scan, AFTER + "T01:43:47"),
    )
    conn.execute(
        """INSERT INTO attendance_days
           (student_id, date, status, flags, entry_scan_id, created_at,
            correction_type, corrected_by_name, correction_reason)
           VALUES (?, ?, 'excused', 'out_of_window', ?, ?, 'data_error', 'dev', 'test')""",
        (student, AFTER, scan, db.utcnow()),
    )
    conn.execute(
        "INSERT INTO school_days (date, entry_open, late_threshold, dismissal_time, "
        "early_departure_cutoff) VALUES (?, '06:15', '07:15', '16:00', '15:30')",
        (AFTER,),
    )
    conn.execute(
        """INSERT INTO notifications
           (student_id, guardian_mobile, trigger, idempotency_key, body, status,
            event_at, queued_at)
           VALUES (?, '639171234567', 'summary', 'k1', 'body', 'pending', ?, ?)""",
        (student, AFTER, db.utcnow()),
    )
    conn.execute(
        "INSERT INTO sms_ledger (date, sent_count) VALUES (?, 0)", (AFTER,)
    )
    conn.execute(
        "INSERT INTO audit_log (action, occurred_at) VALUES ('attendance.correct', ?)",
        (AFTER + "T01:44:23",),
    )
    return path


def run(db_path, backup_path, *extra):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path),
         "--backup", str(backup_path), *extra],
        capture_output=True, text=True, cwd=ROOT, encoding="utf8", errors="replace",
    )
    return result


def counts(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("attendance_days", "scan_events", "screening_events",
                  "school_days", "notifications", "sms_ledger", "audit_log")
    }
    conn.close()
    return out


def test_dry_run_reports_without_changing_anything(scratch_db, tmp_path):
    before = counts(scratch_db)
    result = run(scratch_db, tmp_path / "backup.db", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout
    assert counts(scratch_db) == before


def test_clean_removes_only_rows_after_the_cutoff(scratch_db, tmp_path):
    result = run(scratch_db, tmp_path / "backup.db")

    assert result.returncode == 0, result.stderr
    after = counts(scratch_db)
    assert after == {
        "attendance_days": 1, "scan_events": 0, "screening_events": 0,
        "school_days": 1, "notifications": 0, "sms_ledger": 0, "audit_log": 0,
    }


def test_clean_backs_up_before_touching_anything(scratch_db, tmp_path):
    backup = tmp_path / "backup.db"
    run(scratch_db, backup)

    assert backup.exists()
    backed_up = counts(backup)
    assert backed_up["scan_events"] == 1, "the backup is the pre-clean state"


def test_foreign_keys_stay_intact_after_cleaning(scratch_db, tmp_path):
    run(scratch_db, tmp_path / "backup.db")
    conn = sqlite3.connect(scratch_db)
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_clean_database_reports_nothing_to_do(scratch_db, tmp_path):
    backup = tmp_path / "backup.db"
    run(scratch_db, backup)
    result = run(scratch_db, backup)

    assert result.returncode == 0, result.stderr
    assert "nothing to clean" in result.stdout
