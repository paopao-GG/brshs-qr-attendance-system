"""End-to-end seeding, against a synthetic workbook and a scratch database.

The property being guarded is the one that matters most now that the roster is real:
**a student imported from a spreadsheet cannot be texted.** The SMS allowlist cannot carry
that guarantee -- an empty SMS_ALLOWLIST restricts nothing (config.py) and .env is
gitignored, so a fresh clone has no allowlist at all. consent_on_file = 0 is checked in
queue.py before anything is enqueued and travels with the database file itself.

The workbook here is fabricated. The real student-info.xlsx holds 124 real children's
records and no test opens it.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts" / "seed_demo.py"

ROWS = [
    ("MALE", None, None, None, None),
    ("LRN:", "NAME OF STUDENT:", "NAME OF PARENT:", "NO. OF PARENT:", "GMAIL OF PARENT:"),
    (111995150037, "Reyes, Ana M.", "Reyes, Edith M.", 9478179371, "e@example.com"),
    (111995150038, "Reyes, Ben M.", "Reyes, Edith M.", 9478179371, "e@example.com"),
    (None, "Nolrn, Carlo P.", None, None, None),
    ("FEMALE:", None, None, None, None),
    (403610150027, "Santos, Dina A.", "Santos, Rosa A.", 9389688288, None),
    (403610150028, "Cruz, Elena B.", "Cruz, Mario B.", "99430625693", None),
]


@pytest.fixture
def roster_file(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = "11-Testing"
    for row in ROWS:
        sheet.append(row)
    path = tmp_path / "roster.xlsx"
    book.save(path)
    return path


def seed(roster_file, tmp_path, *extra):
    """Run the seeder as the operator runs it, against a scratch DB."""
    db_path = tmp_path / "seeded.db"
    result = subprocess.run(
        [sys.executable, str(SEED), "--reset",
         "--roster", str(roster_file), "--db", str(db_path), *extra],
        capture_output=True, text=True, cwd=ROOT, encoding="utf8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return db_path, result.stdout


def students(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM students ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


# --- the guard ---------------------------------------------------------------

def test_no_imported_student_can_be_texted(roster_file, tmp_path):
    """The whole safety story. Real parents' numbers are in the database; none of them
    is reachable until a person decides otherwise."""
    db_path, _ = seed(roster_file, tmp_path)
    imported = [r for r in students(db_path) if r["last_name"] != "Learner"]

    assert imported, "the fixture must import somebody or this proves nothing"
    assert all(r["consent_on_file"] == 0 for r in imported)


def test_the_researchers_own_row_is_the_only_one_with_consent(roster_file, tmp_path):
    db_path, _ = seed(roster_file, tmp_path)
    consented = [r for r in students(db_path) if r["consent_on_file"] == 1]

    assert len(consented) == 1
    assert consented[0]["last_name"] == "Learner"
    assert consented[0]["guardian_mobile"] == "639171234567"


def test_consent_releases_the_imported_students(roster_file, tmp_path):
    """The flag exists so the school can switch it on once forms are collected."""
    db_path, output = seed(roster_file, tmp_path, "--consent")

    assert all(r["consent_on_file"] == 1 for r in students(db_path))
    assert "--consent was passed" in output


def test_the_summary_says_nobody_can_be_texted(roster_file, tmp_path):
    """Discoverable at the terminal, not only in a doc nobody opens on setup day."""
    _, output = seed(roster_file, tmp_path)
    assert "none of them can be texted" in output


# --- what lands --------------------------------------------------------------

def test_incomplete_rows_are_skipped_and_reported(roster_file, tmp_path):
    db_path, output = seed(roster_file, tmp_path)
    names = {r["last_name"] for r in students(db_path)}

    assert "Nolrn" not in names
    assert "SKIPPED 1 rows" in output
    assert "Nolrn, Carlo P." in output


def test_an_unparseable_number_still_seeds_the_student(roster_file, tmp_path):
    db_path, output = seed(roster_file, tmp_path)
    cruz = next(r for r in students(db_path) if r["last_name"] == "Cruz")

    assert cruz["guardian_mobile"] is None
    assert "not a PH mobile number" in output


def test_the_sheet_title_becomes_the_section(roster_file, tmp_path):
    import sqlite3
    db_path, _ = seed(roster_file, tmp_path)
    conn = sqlite3.connect(db_path)
    sections = conn.execute("SELECT grade_level, name FROM sections ORDER BY id").fetchall()
    conn.close()

    assert (11, "Testing") in sections


def test_banner_rows_do_not_become_students(roster_file, tmp_path):
    db_path, _ = seed(roster_file, tmp_path)
    names = {r["last_name"].upper() for r in students(db_path)}

    assert not names & {"MALE", "FEMALE", "FEMALE:", "LRN:"}


def test_a_shared_guardian_number_is_pointed_out(roster_file, tmp_path):
    """Guardian coalescing is invisible unless somebody knows which two IDs to scan."""
    _, output = seed(roster_file, tmp_path)
    assert "share a guardian number" in output


def test_seeding_twice_without_reset_refuses(roster_file, tmp_path):
    db_path, _ = seed(roster_file, tmp_path)
    result = subprocess.run(
        [sys.executable, str(SEED), "--roster", str(roster_file), "--db", str(db_path)],
        capture_output=True, text=True, cwd=ROOT, encoding="utf8", errors="replace",
    )
    assert "Use --reset" in result.stdout
