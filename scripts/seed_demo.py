"""Load the real roster from the school's spreadsheet into a fresh database.

    python scripts/seed_demo.py --reset
    python scripts/seed_demo.py --reset --roster path/to/other.xlsx
    python scripts/seed_demo.py --reset --consent      # only once consent is collected

Reads data/student-list.xlsx -- one worksheet per section, each split into a MALE and a
FEMALE block -- and seeds every row carrying an LRN. That is the same rule qr-generator
uses, deliberately: when the two disagreed, the generator printed cards for students the
database had never heard of. The banners are read as data, not skipped: they are the only
place the sheet records a student's sex, and DepEd SF2 cannot be built without it. Missing guardian
details are reported, not fatal -- the roster screen in the app is where they get filled
in, and refusing the student here would mean the only way to fix them is Excel.

NOTHING SEEDED FROM THE SPREADSHEET CAN BE TEXTED. See --consent below.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trackify.core import db, roster
from trackify.core.config import load_config
from trackify.core.mobile import normalise
from trackify.core.qrcodes import encode

ROOT = Path(__file__).resolve().parents[1]
# The roster lives under data/, which is gitignored in full. It held the repo root once
# and reached a public GitHub repo from there; see .gitignore.
DEFAULT_ROSTER = ROOT / "data" / "student-list.xlsx"

# Role placeholders, not people. sections.adviser_id needs a row to point at, but
# inventing named staff puts fictional employees in a database that now holds real
# students -- and someone reading the audit log later cannot tell which is which.
USERS = [
    ("operator", "operator", "Operator"),
    ("adviser",  "adviser",  "Class Adviser"),
    ("admin",    "admin",    "Administrator"),
]

# The researcher's own record. He imports from the sheet like everyone else; this only
# attaches his own handset as the guardian number and grants the ONE consent in the
# database, because he is the only person here who has actually given it.
OWNER = {
    "lrn": "999900000018",
    "first": "Demo",
    "last": "Learner",
    "grade_level": 11,
    "section_name": "Ingenuity",
    "guardian_name": "Demo Learner (own handset - demo)",
    "guardian_mobile": "09171234567",
    "sex": "M",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete the existing data")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER,
                        help=f"roster workbook (default: {DEFAULT_ROSTER.name})")
    parser.add_argument("--consent", action="store_true",
                        help="mark imported students as having consent on file. "
                             "Only pass this once the school has actually collected it.")
    parser.add_argument("--db", type=Path, default=None,
                        help="database to write to (default: the configured one). "
                             "Mainly so the import can be exercised against a scratch file.")
    args = parser.parse_args()

    config = load_config()
    if not config.secrets.qr_secret:
        print("ERROR: TRACKIFY_QR_SECRET is not set.\n"
              "  Copy .env.example to .env and generate one:\n"
              '    python -c "import secrets; print(secrets.token_urlsafe(32))"', file=sys.stderr)
        return 1

    if not args.roster.exists():
        print(f"ERROR: roster not found: {args.roster}", file=sys.stderr)
        return 1

    students, rejected = roster.parse_workbook(args.roster)
    if not students:
        print(f"ERROR: no importable rows in {args.roster}", file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    db.init_db(conn)

    if args.reset:
        # Clear rows rather than unlinking the file. On Windows the DB is often
        # still held open (OneDrive, a running kiosk), and deleting it fails with
        # a PermissionError that looks like a bug. This works regardless.
        # Order matters: children before parents. incidents and custody_items point at
        # screening_events, which points at scan_events with ON DELETE RESTRICT, so
        # deleting scans first fails with a bare "FOREIGN KEY constraint failed".
        # app_settings is cleared too, so a reset returns to genuine first-run state:
        # the records password is unset and has to be chosen. A demo database handed
        # over with a password only I know would be worse than one with none.
        for table in ("risk_scores", "incidents", "custody_items", "hazard_requests",
                      "screening_events", "notifications", "attendance_days",
                      "scan_events", "audit_log", "ahp_weights", "sms_ledger",
                      "school_days", "students", "sections", "users", "app_settings"):
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

    section_ids: dict[tuple[int, str], int] = {}

    def section_for(grade_level: int, name: str) -> int:
        key = (grade_level, name)
        if key not in section_ids:
            cur = conn.execute(
                "INSERT INTO sections (name, grade_level, adviser_id) VALUES (?, ?, ?)",
                (name, grade_level, user_ids["adviser"]),
            )
            section_ids[key] = cur.lastrowid
        return section_ids[key]

    consent = 1 if args.consent else 0
    secret = config.secrets.qr_secret

    print(f"\n{'LRN':<14} {'PAYLOAD':<26} {'NAME':<34} {'SECTION':<15} GUARDIAN")
    print("-" * 116)

    for candidate in students:
        conn.execute(
            """INSERT INTO students
               (lrn, first_name, last_name, section_id, guardian_name,
                guardian_mobile, sex, consent_on_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            # sex comes from the MALE/FEMALE banner in the sheet. It is None for any
            # student listed above one, and the SF2 export reports how many.
            (candidate.lrn, candidate.first, candidate.last,
             section_for(candidate.grade_level, candidate.section_name),
             candidate.guardian_name or None, candidate.guardian_mobile,
             candidate.sex, consent, db.utcnow()),
        )
        _print_row(candidate.lrn, candidate.full_name, candidate.section_label,
                   candidate.guardian_mobile, secret)

    owner = _grant_owner_consent(conn, section_for)
    print("-" * 116)
    print(f"Consent granted to {owner['last_name']}, {owner['first_name']} "
          f"({OWNER['guardian_mobile']}) - the only textable row in this database.")

    _report(students, rejected)
    _summary(conn, len(section_ids), students,
             encode(int(OWNER["lrn"]), secret), consent)
    return 0


def _grant_owner_consent(conn, section_for) -> sqlite3.Row:
    """Give the researcher his own number and the only consent in the database.

    He is now imported from the sheet like everyone else -- he has an LRN -- so this
    updates that row rather than inserting one, which would collide on students.lrn.
    consent_on_file = 1 is unconditional here and nowhere else: he is the one person
    who has actually consented, to his own handset, which is what makes a live SMS
    demo safe to run against a roster of real families.
    """
    mobile = normalise(OWNER["guardian_mobile"])
    existing = conn.execute("SELECT id FROM students WHERE lrn = ?",
                            (OWNER["lrn"],)).fetchone()
    if existing:
        conn.execute(
            """UPDATE students SET guardian_name = ?, guardian_mobile = ?,
               sex = COALESCE(sex, ?), consent_on_file = 1 WHERE id = ?""",
            # COALESCE, not a plain assignment: if the sheet's banner already placed
            # him, that is the school's record and this hardcoded value is not.
            (OWNER["guardian_name"], mobile, OWNER["sex"], existing["id"]),
        )
    else:
        # The sheet no longer lists him. Put the row back so the demo still works.
        conn.execute(
            """INSERT INTO students
               (lrn, first_name, last_name, section_id, guardian_name,
                guardian_mobile, sex, consent_on_file, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (OWNER["lrn"], OWNER["first"], OWNER["last"],
             section_for(OWNER["grade_level"], OWNER["section_name"]),
             OWNER["guardian_name"], mobile, OWNER["sex"], db.utcnow()),
        )
    return conn.execute("SELECT * FROM students WHERE lrn = ?",
                        (OWNER["lrn"],)).fetchone()


def _print_row(lrn: str, name: str, section: str,
               mobile: str | None, secret: str) -> None:
    # The payload is derived, never stored: it is a pure function of (lrn, secret).
    # Signed over the LRN, not the row id, so a printed card survives a reseed --
    # see ScanService.student_row().
    payload = encode(int(lrn), secret) if str(lrn).isdigit() else "(LRN not numeric)"
    who = mobile or "(no number)"
    print(f"{lrn:<14} {payload:<26} {name:<34} {section:<15} {who}")


def _report(students, rejected) -> None:
    """Say what was odd and what was refused. A silent filter looks like a bug."""
    noted = [c for c in students if c.notes]
    if noted:
        print("\nNOTES (imported, but check these in the spreadsheet)")
        for candidate in noted:
            for index, note in enumerate(candidate.notes):
                who = candidate.full_name if index == 0 else ""
                where = candidate.section_label if index == 0 else ""
                print(f"  {who:<34}{where:<15} {note}")

    if not rejected:
        return

    by_reason: dict[str, list[str]] = defaultdict(list)
    for row in rejected:
        by_reason["; ".join(row.reasons)].append(row.name)

    print(f"\nSKIPPED {len(rejected)} rows - fix the spreadsheet and re-run")
    for reason, names in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason} ({len(names)})")
        shown = " / ".join(names[:3])
        more = f" / ... and {len(names) - 3} more" if len(names) > 3 else ""
        print(f"      {shown}{more}")


def _summary(conn, sections: int, students,
             owner_payload: str, consent: int) -> None:
    seeded = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    print(f"\nSeeded {seeded} students into {sections} sections")
    for label, count in sorted(Counter(c.section_label for c in students).items()):
        print(f"  {label:<18} {count}")

    unreachable = conn.execute(
        "SELECT COUNT(*) FROM students WHERE guardian_mobile IS NULL").fetchone()[0]
    if unreachable:
        # Not an error and not a reason to refuse the student: they scan, their
        # attendance is recorded, and the roster screen is where somebody fills the
        # number in. Reported so it gets chased rather than discovered in November.
        print(f"\n{unreachable} student(s) have no guardian number on file. They scan "
              "normally;\nnobody can be texted about them. Fix in the app: Attendance "
              "records -> Student roster.")

    if consent:
        print("\n!!  --consent was passed: every imported student is marked as having")
        print("!!  consent on file and CAN be texted. Only correct if the school has")
        print("!!  actually collected those forms.")
    else:
        # The allowlist cannot be relied on here: an empty SMS_ALLOWLIST restricts
        # nothing, and .env is gitignored, so the client's copy has no allowlist at all.
        # consent_on_file = 0 is checked in queue.py before anything is enqueued and
        # travels with the database.
        print("\nNo imported student has consent on file, so none of them can be texted.")
        print(f"Only Learner (payload {owner_payload}) can receive SMS.")
        print("Pass --consent once the school has collected the consent forms.")

    shared = [number for number, count
              in Counter(c.guardian_mobile for c in students if c.guardian_mobile).items()
              if count > 1]
    if shared:
        names = [c.full_name for c in students if c.guardian_mobile == shared[0]]
        print(f"\n{' and '.join(names)} share a guardian number -- scan both")
        print("within 3 minutes to see guardian coalescing merge them into one message.")


if __name__ == "__main__":
    raise SystemExit(main())
