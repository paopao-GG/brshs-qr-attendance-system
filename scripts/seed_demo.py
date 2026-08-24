"""Create a demo database for local testing. No Pi, no scanner, no SMS credits.

    python scripts/seed_demo.py [--reset]

Includes a sibling pair sharing one guardian number so coalescing is visible.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trackify.core import db
from trackify.core.config import load_config
from trackify.core.mobile import normalise
from trackify.core.qrcodes import encode

SECTIONS = [("Rizal", 7), ("Bonifacio", 8), ("Mabini", 9)]

# (first, last, section index, guardian name, guardian mobile)
# Juan and Maria deliberately share a mother -- that pair is what makes
# guardian coalescing observable in the kiosk demo.
STUDENTS = [
    ("Juan",       "Dela Cruz",  0, "Maria Dela Cruz",   "09171234567"),
    ("Maria",      "Dela Cruz",  0, "Maria Dela Cruz",   "09171234567"),
    ("Pedro",      "Santos",     0, "Ana Santos",        "09181112222"),
    ("Andrea",     "Reyes",      0, "Luz Reyes",         "09183334444"),
    ("Miguel",     "Bautista",   0, "Rosa Bautista",     "09185556666"),
    ("Sofia",      "Peña",       0, "Elena Peña",        "09187778888"),
    ("Gabriel",    "Villanueva", 0, "Nora Villanueva",   "09189990000"),
    ("Isabella",   "Ramos",      1, "Cora Ramos",        "09191112222"),
    ("Rafael",     "Aquino",     1, "Delia Aquino",      "09193334444"),
    ("Camila",     "Mendoza",    1, "Rita Mendoza",      "09195556666"),
    ("Diego",      "Learner",     1, "Cely Learner",       "09197778888"),
    ("Lucia",      "Domingo",    1, "Fely Domingo",      "09199990000"),
    ("Mateo",      "Castillo",   1, "Vilma Castillo",    "09201112222"),
    ("Valentina",  "Navarro",    2, "Tessie Navarro",    "09203334444"),
    ("Emilio",     "Salazar",    2, "Marites Salazar",   "09205556666"),
    ("Renata",     "Gutierrez",  2, "Baby Gutierrez",    "09207778888"),
    ("Tomas",      "Fernandez",  2, "Lorna Fernandez",   "09209990000"),
    ("Bianca",     "Ocampo",     2, "Susan Ocampo",      "09211112222"),
    ("Alonso",     "Rivera",     2, "Gina Rivera",       "09213334444"),
    ("Beatriz",    "Cortez",     2, "Dolor Cortez",      None),   # attendance only
]

USERS = [
    ("guard",   "operator", "Ramon Guardia"),
    ("adviser", "adviser",  "Tricia San Jose"),
    ("admin",   "admin",    "School Administrator"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the existing demo DB")
    args = parser.parse_args()

    config = load_config()
    if not config.secrets.qr_secret:
        print("ERROR: TRACKIFY_QR_SECRET is not set.\n"
              "  Copy .env.example to .env and generate one:\n"
              '    python -c "import secrets; print(secrets.token_urlsafe(32))"', file=sys.stderr)
        return 1

    conn = db.connect()
    db.init_db(conn)

    if args.reset:
        # Clear rows rather than unlinking the file. On Windows the DB is often
        # still held open (OneDrive, a running kiosk), and deleting it fails with
        # a PermissionError that looks like a bug. This works regardless.
        for table in ("risk_scores", "notifications", "attendance_days", "scan_events",
                      "audit_log", "ahp_weights", "sms_ledger", "school_days",
                      "students", "sections", "users"):
            conn.execute(f"DELETE FROM {table}")
        # No AUTOINCREMENT in the schema, so rowids restart at 1 on their own --
        # which keeps the demo QR payloads stable across resets.
        print("cleared existing data")

    if conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]:
        print("Database already has students. Use --reset to start over.")
        return 0

    user_ids = {}
    for username, role, full_name in USERS:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, role, full_name, created_at)
               VALUES (?, 'x-not-set-yet', ?, ?, ?)""",
            (username, role, full_name, db.utcnow()),
        )
        user_ids[username] = cur.lastrowid

    section_ids = []
    for name, grade in SECTIONS:
        cur = conn.execute(
            "INSERT INTO sections (name, grade_level, adviser_id) VALUES (?, ?, ?)",
            (name, grade, user_ids["adviser"]),
        )
        section_ids.append(cur.lastrowid)

    print(f"\n{'ID':>3}  {'PAYLOAD':<22} {'NAME':<24} {'SECTION':<12} GUARDIAN")
    print("-" * 92)
    for index, (first, last, sec_idx, guardian, mobile) in enumerate(STUDENTS, start=1):
        cur = conn.execute(
            """INSERT INTO students
               (lrn, first_name, last_name, section_id, guardian_name,
                guardian_mobile, consent_on_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (f"13658412{index:04d}", first, last, section_ids[sec_idx],
             guardian, normalise(mobile), db.utcnow()),
        )
        student_id = cur.lastrowid
        # The payload is derived, never stored: it is a pure function of
        # (student_id, secret). Persisting it would denormalise and could drift
        # if the secret were ever rotated.
        payload = encode(student_id, config.secrets.qr_secret)
        section = f"{SECTIONS[sec_idx][1]}-{SECTIONS[sec_idx][0]}"
        who = mobile or "(no number)"
        print(f"{student_id:>3}  {payload:<22} {first + ' ' + last:<24} {section:<12} {who}")

    print("-" * 92)
    print(f"\nSeeded {len(STUDENTS)} students, {len(SECTIONS)} sections into {db.DEFAULT_DB}")
    print("Juan and Maria Dela Cruz share a guardian number -- scan both within")
    print("3 minutes to see guardian coalescing merge them into one message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
