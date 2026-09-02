"""Trim the live database to the simulated Aug 10-21 2026 attendance window and remove
leftover development artefacts on top of it.

    python scripts/clean_data.py              # back up, then clean
    python scripts/clean_data.py --dry-run    # report what would be removed, change nothing

Surgical, not a re-simulation. scripts/simulate_term.py already generated exactly Aug
10-21 (its own DEFAULT_START/DEFAULT_END) -- 10 school days x every active student --
and every incidents row already falls inside that range. What is left on top is a
handful of rows a development session added afterwards: two manual attendance_days
rows dated 2026-08-27 (student 64, correction_type='data_error', corrected_by_name=
'dev'), the screening_event and scan_event underneath them, school_days rows the kiosk
auto-creates just by being opened past the simulated range, one queued notification
that never sent, one sms_ledger counter row, and the dev session's audit_log.

scripts/simulate_term.py --clear would NOT have caught any of this: its own delete is
`BETWEEN start AND end`, and everything here is dated strictly after 2026-08-21.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trackify.core import db

CUTOFF = "2026-08-21"                          # last day of the simulated term
CUTOFF_TIMESTAMP = CUTOFF + "T23:59:59"        # occurred_at is a full timestamp
BACKUP = ROOT / "data" / "trackify.pre-clean.db"


def back_up(target: Path, backup: Path, force: bool) -> None:
    if backup.exists() and not force:
        print(f"  backup already exists, keeping it: {backup.name}")
        return
    if backup.exists():
        backup.unlink()
    # VACUUM INTO, not a file copy: the database is in WAL mode, so a copy of the .db
    # file alone would miss whatever is still sitting in the write-ahead log. Same
    # approach as simulate_term.py's back_up().
    raw = sqlite3.connect(target)
    try:
        raw.execute("VACUUM INTO ?", (str(backup),))
    finally:
        raw.close()
    print(f"  backed up -> {backup.name}")


def plan(conn: sqlite3.Connection) -> dict[str, int]:
    """What would be removed, without removing it."""
    return {
        "attendance_days after the cutoff": conn.execute(
            "SELECT COUNT(*) FROM attendance_days WHERE date > ?", (CUTOFF,)
        ).fetchone()[0],
        "screening_events after the cutoff": conn.execute(
            "SELECT COUNT(*) FROM screening_events WHERE occurred_at > ?",
            (CUTOFF_TIMESTAMP,),
        ).fetchone()[0],
        "scan_events after the cutoff": conn.execute(
            "SELECT COUNT(*) FROM scan_events WHERE date > ?", (CUTOFF,)
        ).fetchone()[0],
        "school_days after the cutoff": conn.execute(
            "SELECT COUNT(*) FROM school_days WHERE date > ?", (CUTOFF,)
        ).fetchone()[0],
        "notifications": conn.execute(
            "SELECT COUNT(*) FROM notifications"
        ).fetchone()[0],
        "sms_ledger rows": conn.execute(
            "SELECT COUNT(*) FROM sms_ledger"
        ).fetchone()[0],
        "audit_log rows": conn.execute(
            "SELECT COUNT(*) FROM audit_log"
        ).fetchone()[0],
    }


def clean(conn: sqlite3.Connection) -> None:
    """Children before parents.

    scan_events <- screening_events <- incidents/custody_items, all ON DELETE RESTRICT
    except custody_items (SET NULL) -- same ordering rule seed_demo.py's --reset uses
    and documents. Nothing here needs incidents or custody_items touched first: the
    stray screening_event's outcome is 'clear', so no incident or custody row
    references it, and DELETE would raise "FOREIGN KEY constraint failed" immediately
    if that assumption were ever wrong.
    """
    conn.execute("DELETE FROM attendance_days WHERE date > ?", (CUTOFF,))
    conn.execute("DELETE FROM screening_events WHERE occurred_at > ?",
                (CUTOFF_TIMESTAMP,))
    # scan_events is documented APPEND ONLY, never updated or deleted (schema.sql).
    # Deleting the one stray row here is a deliberate exception -- it was never a real
    # gate scan, only a manual test row -- not a precedent for touching this table.
    conn.execute("DELETE FROM scan_events WHERE date > ?", (CUTOFF,))
    conn.execute("DELETE FROM school_days WHERE date > ?", (CUTOFF,))
    conn.execute("DELETE FROM notifications")
    conn.execute("DELETE FROM sms_ledger")
    conn.execute("DELETE FROM audit_log")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be removed; change nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing backup")
    parser.add_argument("--db", default=None,
                        help="database path (default: the live one)")
    parser.add_argument("--backup", default=None,
                        help=f"backup path (default: {BACKUP})")
    args = parser.parse_args(argv)

    target = Path(args.db) if args.db else db.DEFAULT_DB
    backup = Path(args.backup) if args.backup else BACKUP
    if not target.exists():
        print(f"No database at {target}.", file=sys.stderr)
        return 1

    conn = db.connect(target)

    counts = plan(conn)
    for label, n in counts.items():
        print(f"  {label}: {n}")

    if not any(counts.values()):
        print("nothing to clean")
        return 0

    if args.dry_run:
        print("dry run -- nothing changed")
        return 0

    back_up(target, backup, args.force)
    clean(conn)
    print("done")

    row = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM attendance_days"
    ).fetchone()
    print(f"attendance_days now spans {row[0]} .. {row[1]} ({row[2]} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
