"""Generate a plausible two weeks of gate traffic so the exports have something in them.

    python scripts/simulate_term.py              # Aug 10-21 2026, backs up the DB first
    python scripts/simulate_term.py --clear      # remove the simulated range again
    python scripts/simulate_term.py --seed 7     # a different but equally reproducible run

============================================================================
THE DATA THIS WRITES IS INVENTED. It is for exercising the exports and the
UI. It produces a slope, a p-value, an R squared and an AUC that look exactly
like measurements and are not. None of it may be reported as a finding.
============================================================================

Everything goes through the same functions the kiosk calls -- record_scan, the screening
recorder, the custody chain, close_open_days -- rather than INSERTing rows directly. Two
reasons: the derived columns (status, flags, minutes_on_campus) then come out the way
production would compute them, and anything the simulator can provoke is a bug a real
scan could provoke too.

What makes the numbers behave like people rather than dice:

  Archetypes    Each student keeps one absence/lateness propensity for the whole run. A
                cohort of identical students has no between-student variance, so every
                composite risk score lands in a heap and the bands say nothing.

  Streaks       Absent yesterday makes absent today far more likely. A cold lasts three
                days. `consecutive_absences` is a model feature and needs runs to read.

  Day effects   Monday and Friday are worse; one rainy Tuesday is much worse. `is_monday`
                and `is_friday` are two of the five features -- without a real effect they
                are noise columns that the model correctly ignores.

  No drift      There is deliberately NO long-run trend baked in. Manufacturing one would
                be manufacturing the answer to research question 2, and the regression
                would faithfully find it. Whatever slope comes out is noise, and the
                honest result is most likely "no significant trend".
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import date as Date
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trackify.core import custody, db, screening
from trackify.core.attendance import Outcome, close_open_days, record_scan
from trackify.core.config import load_config
from trackify.core.screening import Outcome as ScreeningOutcome
from trackify.core.service import ScanService

DEFAULT_START = "2026-08-10"
DEFAULT_END = "2026-08-21"
DEFAULT_SEED = 20260810

MARKER = "simulated_data"
BACKUP = ROOT / "data" / "trackify.clean.db"

# The owner's own row is the only one with consent on file, so it is the only one whose
# incident would queue a real notification. Incidents skip him.
OWNER_LRN = "999900000018"

# name, share of cohort, daily absence probability, daily lateness probability
ARCHETYPES = (
    ("reliable", 0.70, 0.03, 0.05),
    ("borderline", 0.20, 0.12, 0.25),
    ("at-risk", 0.10, 0.25, 0.40),
)

# Absent yesterday? Then today is not an independent coin flip. Whatever kept them home
# -- illness, a family matter, the fare -- usually lasts more than one morning.
STREAK_ABSENCE = 0.45

MONDAY_ABSENCE = 1.30
FRIDAY_ABSENCE = 1.20

# Bicol in August. A wet Tuesday is the most ordinary thing in this dataset.
RAIN_DAY = "2026-08-18"
RAIN_ABSENCE = 2.00
RAIN_LATE = 2.50

EARLY_DEPARTURE_RATE = 0.02      # medical, family; before early_departure_cutoff
NO_EXIT_SCAN_RATE = 0.05         # real gates lose exit scans constantly

# Share of in-scans, in order. Sums to 1.0. `not_screened` is the gate rush -- a real
# coverage figure of ~92% rather than a fictional 100%.
SCREENING_MIX = (
    (ScreeningOutcome.CLEAR, 0.710),
    (ScreeningOutcome.COMMON_ITEMS, 0.190),
    (ScreeningOutcome.NOT_SCREENED, 0.080),
    (ScreeningOutcome.PENDING_VERIFICATION, 0.010),
    (ScreeningOutcome.PROHIBITED, 0.004),
    (ScreeningOutcome.SCHOOL_HAZARD, 0.003),
    (ScreeningOutcome.OVERRIDDEN, 0.003),
)

# What actually sets off a handheld detector at a school gate, in rough order of how
# often a guard sees it.
COMMON_ITEMS = (
    "phone", "phone, coins", "tumbler", "coins", "phone, tumbler",
    "belt buckle", "laptop", "phone, keys", "tablet", "umbrella",
)

# category -> descriptions. Severity comes from screening.default_severity() so no
# severity_reason is needed, except the one deliberate case below.
PROHIBITED_ITEMS = (
    ("bladed", "folding penknife"),
    ("pointed", "geometry compass, sharpened"),
    ("tool", "flat screwdriver"),
    ("blunt", "metal pipe offcut"),
    ("bladed", "box cutter"),
)

HAZARD_ITEMS = (
    ("cutter", "TLE cutter, blade retracted"),
    ("scissors", "dissecting scissors"),
    ("knife", "kitchen knife for cookery"),
)

# Teachers declaring in advance that a section needs hazardous tools. A release matched
# to one of these is the controlled path; a release without one is the exception.
HAZARD_REQUESTS = (
    ("2026-08-11", "TLE", "cutters", "Cutting foam board for the display"),
    ("2026-08-13", "Science", "dissecting kit", "Frog dissection, third period"),
    ("2026-08-19", "TLE", "cutters", "Continuation of the display boards"),
    ("2026-08-20", "Home Economics", "kitchen knife", "Cookery practical"),
)

OVERRIDE_REASONS = (
    "detector battery flat, visual inspection only",
    "queue backed up past the gate, waved through by the guard on duty",
    "student on crutches, screened at the office instead",
)


# --- the day ----------------------------------------------------------------

def school_days(start: str, end: str) -> list[str]:
    """Weekdays only. No holiday calendar -- Aug 10-21 2026 has none between them."""
    first, last = Date.fromisoformat(start), Date.fromisoformat(end)
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def arrival_time(rng: random.Random, day: str, late: bool) -> time:
    """On time clusters before the bell; late is right-skewed.

    Clipped at 06:15 rather than 06:00 on purpose: a scan before entry_open would be
    flagged out_of_window, and a student turning up before the gate opens is not the
    behaviour being modelled here.
    """
    if late:
        # Most late arrivals are a few minutes over. A few are badly late.
        minutes = 435 + int(rng.expovariate(1 / 9.0))         # 07:15 + skew
        minutes = min(minutes, 470)                            # cap at 07:50
    else:
        minutes = int(rng.gauss(410, 15))                      # about 06:50
        minutes = max(375, min(minutes, 434))                  # 06:15 .. 07:14
    return time(minutes // 60, minutes % 60)


def departure_time(rng: random.Random, early: bool) -> time:
    if early:
        minutes = rng.randint(13 * 60 + 30, 15 * 60 + 25)      # 13:30 .. 15:25
    else:
        minutes = int(rng.gauss(16 * 60 + 12, 11))             # about 16:12
        minutes = max(16 * 60, min(minutes, 17 * 60 + 10))
    return time(minutes // 60, minutes % 60)


def pick(rng: random.Random, weighted):
    roll, running = rng.random(), 0.0
    for value, share in weighted:
        running += share
        if roll < running:
            return value
    return weighted[-1][0]


def alarmed(outcome: ScreeningOutcome) -> bool:
    """Whether the handheld detector went off.

    The kiosk simplifies this to "anything that is not Clear", which would count a scan
    nobody screened as an alarm and inflate the alarm rate. Here silence means silence.
    """
    return outcome in (ScreeningOutcome.COMMON_ITEMS, ScreeningOutcome.PROHIBITED,
                       ScreeningOutcome.SCHOOL_HAZARD,
                       ScreeningOutcome.PENDING_VERIFICATION)


# --- the run ----------------------------------------------------------------

def simulate(conn: sqlite3.Connection, config, days: list[str], seed: int) -> dict:
    rng = random.Random(seed)
    service = ScanService(conn, config)

    students = conn.execute(
        """SELECT s.id, s.lrn, s.section_id, s.first_name, s.last_name
           FROM students s WHERE s.active = 1 ORDER BY s.id"""
    ).fetchall()
    if not students:
        sys.exit("No active students. Run: python scripts/seed_demo.py")

    adviser = conn.execute(
        "SELECT id FROM users WHERE role = 'adviser' ORDER BY id LIMIT 1").fetchone()
    adviser_id = adviser["id"] if adviser else None

    # Assigned once and kept. This is what gives the cohort a spread of risk scores
    # instead of 103 students who all look the same.
    profile = {}
    for row in students:
        name, _, absence, late = pick(
            rng, [(a, a[1]) for a in ARCHETYPES])
        profile[row["id"]] = (name, absence, late)

    # Teachers declaring in advance. Round-robin over the sections rather than random:
    # these need to be reproducible, and a random section makes the count of backed
    # releases a lottery.
    sections = sorted({r["section_id"] for r in students})
    for index, (day, subject, item, note) in enumerate(HAZARD_REQUESTS):
        if day not in days:
            continue
        custody.request_tools(conn, sections[index % len(sections)], day, subject, item,
                              notes=note, requested_by=adviser_id)

    stats = {"scans": 0, "absent": 0, "late": 0, "early": 0, "no_exit": 0,
             "screened": 0, "incidents": 0, "custody": 0, "refused": 0}
    absent_yesterday: set[int] = set()
    held: list[tuple[int, str, int]] = []          # custody id, date, section

    for day in days:
        weekday = Date.fromisoformat(day).weekday()
        absence_mult = 1.0
        late_mult = 1.0
        if weekday == 0:
            absence_mult *= MONDAY_ABSENCE
        elif weekday == 4:
            absence_mult *= FRIDAY_ABSENCE
        if day == RAIN_DAY:
            absence_mult *= RAIN_ABSENCE
            late_mult *= RAIN_LATE

        absent_today: set[int] = set()
        screenings: list[tuple] = []

        # record_scan opens no transaction of its own, so the scan loop is wrapped --
        # a thousand autocommits per day is needlessly slow. The screening and custody
        # writers below each BEGIN IMMEDIATE themselves and must stay outside it.
        with db.transaction(conn):
            for row in students:
                sid = row["id"]
                _, p_absent, p_late = profile[sid]

                if sid in absent_yesterday:
                    p_absent = max(p_absent, STREAK_ABSENCE)
                if rng.random() < min(p_absent * absence_mult, 0.95):
                    absent_today.add(sid)
                    stats["absent"] += 1
                    continue

                is_late = rng.random() < min(p_late * late_mult, 0.95)
                arrive = datetime.combine(Date.fromisoformat(day),
                                          arrival_time(rng, day, is_late))
                entry = record_scan(conn, sid, arrive, config)
                if not entry.recorded:
                    stats["refused"] += 1
                    continue
                stats["scans"] += 1
                if entry.status == "late":
                    stats["late"] += 1

                screenings.append((entry.scan_id, sid, arrive))

                if rng.random() < NO_EXIT_SCAN_RATE:
                    stats["no_exit"] += 1
                    continue
                early = rng.random() < EARLY_DEPARTURE_RATE
                leave = datetime.combine(Date.fromisoformat(day),
                                         departure_time(rng, early))
                exit_scan = record_scan(conn, sid, leave, config)
                if exit_scan.recorded:
                    stats["scans"] += 1
                    if early:
                        stats["early"] += 1

        for scan_id, sid, arrive in screenings:
            outcome = pick(rng, SCREENING_MIX)
            is_owner = any(r["id"] == sid and r["lrn"] == OWNER_LRN for r in students)
            if outcome is ScreeningOutcome.PROHIBITED and is_owner:
                outcome = ScreeningOutcome.CLEAR       # never queue a real notification

            at = arrive + timedelta(seconds=rng.randint(20, 90))
            event_id = service.record_screening(
                scan_id, outcome,
                metal_detected=alarmed(outcome),
                declared_items=(rng.choice(COMMON_ITEMS)
                                if outcome is ScreeningOutcome.COMMON_ITEMS else None),
                override_reason=(rng.choice(OVERRIDE_REASONS)
                                 if outcome is ScreeningOutcome.OVERRIDDEN else None),
                operator_id=adviser_id, at=at,
            )
            stats["screened"] += 1

            if outcome is ScreeningOutcome.PROHIBITED:
                category, description = rng.choice(PROHIBITED_ITEMS)
                # One case carries a severity above its category default, which is the
                # only path that requires a reason. Exercising it here means the
                # validation is not first met in production.
                bump = stats["incidents"] == 1
                severity = None
                reason = None
                if bump:
                    severity = min(screening.default_severity(category) + 1, 4)
                    reason = ("blade was exposed and the student was carrying it in a "
                              "trouser pocket")
                service.record_incident(
                    event_id, sid, category, description,
                    severity=severity, severity_reason=reason,
                    confirmed_by=adviser_id,
                    at=at + timedelta(minutes=rng.randint(2, 8)),
                )
                stats["incidents"] += 1

            elif outcome is ScreeningOutcome.SCHOOL_HAZARD:
                purpose, description = rng.choice(HAZARD_ITEMS)
                custody_id = custody.collect(
                    conn, sid, description, screening_event_id=event_id,
                    purpose=f"declared for {purpose}",
                    storage_ref=f"BOX-{day[5:7]}{day[8:10]}-{stats['custody'] + 1:02d}",
                    category="tool", collected_by=adviser_id,
                    at=at + timedelta(minutes=rng.randint(1, 5)),
                )
                stats["custody"] += 1
                section = next(r["section_id"] for r in students if r["id"] == sid)
                held.append((custody_id, day, section))

        close_open_days(conn, day, config)
        absent_yesterday = absent_today

    _work_the_custody_chain(conn, rng, held, adviser_id, stats)
    return stats


def _work_the_custody_chain(conn, rng, held, adviser_id, stats) -> None:
    """Move the held items along, including the one that goes wrong.

    A custody table where everything is still 'held' shows the collection working and
    nothing else. The interesting row is the release with no hazard request behind it:
    that is the control failure the screening summary reports first and unconditionally.
    """
    stats["released"] = 0
    stats["unbacked"] = 0
    stats["returned"] = 0

    for index, (custody_id, day, section) in enumerate(held):
        if index % 3 == 2:
            continue                                  # one in three stays in the box

        # The backed path only exists if a teacher actually declared for THIS student's
        # section on THIS date. Without arranging that, every release comes out
        # unbacked and the contrast the screening sheet reports on disappears.
        if index % 3 == 0 and custody.matching_request(conn, custody_id, day) is None:
            custody.request_tools(
                conn, section, day, "TLE", "cutters",
                notes="Declared the morning of the practical", requested_by=adviser_id)

        request = custody.matching_request(conn, custody_id, day)
        at = datetime.combine(Date.fromisoformat(day), time(9, 30))
        try:
            result = custody.release(
                conn, custody_id, adviser_id,
                reason=None if request is not None else
                       "adviser collected it for the practical; no request was filed",
                at=at, actor_id=adviser_id,
            )
        except custody.CustodyError:
            continue
        stats["released"] += 1
        if not result.backed_by_request:
            stats["unbacked"] += 1

        if index % 2 == 0:
            custody.give_back(
                conn, custody_id, "student",
                at=datetime.combine(Date.fromisoformat(day), time(16, 5)),
                actor_id=adviser_id,
            )
            stats["returned"] += 1


# --- housekeeping -----------------------------------------------------------

def back_up(target: Path, force: bool) -> None:
    if BACKUP.exists() and not force:
        print(f"  backup already exists, keeping it: {BACKUP.name}")
        return
    if BACKUP.exists():
        BACKUP.unlink()
    # VACUUM INTO, not a file copy: the database is in WAL mode and a copy of the .db
    # alone would miss everything still sitting in the write-ahead log.
    raw = sqlite3.connect(target)
    try:
        raw.execute("VACUUM INTO ?", (str(BACKUP),))
    finally:
        raw.close()
    print(f"  backed up clean database -> {BACKUP.name}")


def clear(conn: sqlite3.Connection, start: str, end: str) -> None:
    """Remove only what this script wrote, child tables first (all ON DELETE RESTRICT)."""
    marker = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (MARKER,)).fetchone()
    watermark = json.loads(marker["value"])["audit_from"] if marker else None

    in_range = """SELECT e.id FROM screening_events e
                  JOIN scan_events s ON s.id = e.scan_event_id
                  WHERE s.date BETWEEN ? AND ?"""
    counts = {}
    with db.transaction(conn):
        for label, sql, params in (
            ("incidents",
             f"DELETE FROM incidents WHERE screening_event_id IN ({in_range})",
             (start, end)),
            ("custody_items",
             f"DELETE FROM custody_items WHERE screening_event_id IN ({in_range})",
             (start, end)),
            ("screening_events",
             """DELETE FROM screening_events WHERE scan_event_id IN
                (SELECT id FROM scan_events WHERE date BETWEEN ? AND ?)""",
             (start, end)),
            ("attendance_days",
             "DELETE FROM attendance_days WHERE date BETWEEN ? AND ?", (start, end)),
            ("scan_events",
             "DELETE FROM scan_events WHERE date BETWEEN ? AND ?", (start, end)),
            ("hazard_requests",
             "DELETE FROM hazard_requests WHERE date BETWEEN ? AND ?", (start, end)),
            ("school_days",
             "DELETE FROM school_days WHERE date BETWEEN ? AND ?", (start, end)),
        ):
            counts[label] = conn.execute(sql, params).rowcount

        if watermark is not None:
            counts["audit_log"] = conn.execute(
                "DELETE FROM audit_log WHERE id > ?", (watermark,)).rowcount
        conn.execute("DELETE FROM app_settings WHERE key = ?", (MARKER,))

    for label, n in counts.items():
        print(f"  {label:18} -{n}")


def mark(conn: sqlite3.Connection, start: str, end: str, seed: int,
         audit_from: int) -> None:
    """Leave a record in the database that its attendance is invented.

    The exports do not read this -- they were deliberately left unwatermarked -- so it
    exists for whoever opens the database later and needs to know what they are holding.
    """
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (MARKER, json.dumps({
            "start": start, "end": end, "seed": seed, "audit_from": audit_from,
            "generated": db.utcnow(),
            "warning": "Attendance, screening, incident and custody rows in this range "
                       "were generated by scripts/simulate_term.py. They are not "
                       "observations and must not be reported as findings.",
        }), db.utcnow()),
    )


def report(conn: sqlite3.Connection, days: list[str], stats: dict) -> None:
    print("\n  rows now in the database")
    for table in ("school_days", "scan_events", "attendance_days", "screening_events",
                  "incidents", "custody_items", "hazard_requests", "notifications"):
        print(f"    {table:18} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")

    print("\n  daily attendance")
    for row in conn.execute(
        """SELECT date,
                  SUM(status IN ('present','late','online')) AS attended,
                  SUM(status IN ('present','late','online','absent')) AS eligible,
                  SUM(status = 'late') AS late
           FROM attendance_days WHERE superseded_by IS NULL
           GROUP BY date ORDER BY date"""
    ):
        rate = row["attended"] / row["eligible"]
        weekday = Date.fromisoformat(row["date"]).strftime("%a")
        note = "  <- rain" if row["date"] == RAIN_DAY else ""
        print(f"    {row['date']} {weekday}  {rate:6.1%}   "
              f"{row['late']:>3} late{note}")

    print(f"\n  screenings {stats['screened']}, incidents {stats['incidents']}, "
          f"custody {stats['custody']} "
          f"(released {stats.get('released', 0)}, "
          f"unbacked {stats.get('unbacked', 0)}, returned {stats.get('returned', 0)})")
    print(f"  early departures {stats['early']}, missing exit scans {stats['no_exit']}, "
          f"refused scans {stats['refused']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate simulated attendance so the exports have data in them")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="same seed, same dataset (default: %(default)s)")
    parser.add_argument("--clear", action="store_true",
                        help="remove the simulated range instead of generating it")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing clean-database backup")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    target = args.db or db.DEFAULT_DB
    if not target.exists():
        print(f"No database at {target}.\n  Run: python scripts/seed_demo.py",
              file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    db.init_db(conn)
    days = school_days(args.start, args.end)

    if args.clear:
        print(f"Clearing simulated data {args.start} .. {args.end}")
        clear(conn, args.start, args.end)
        print("\nDone. Students, sections and users were not touched.")
        return 0

    existing = conn.execute(
        "SELECT COUNT(*) FROM attendance_days WHERE date BETWEEN ? AND ?",
        (args.start, args.end)).fetchone()[0]
    if existing:
        print(f"{existing} attendance row(s) already exist in {args.start}..{args.end}.\n"
              "  Run with --clear first.", file=sys.stderr)
        return 1

    print("=" * 74)
    print("  SIMULATED DATA. Everything this writes is invented.")
    print("  It exists to exercise the exports and the UI. The trend, the p-value,")
    print("  the R squared and the AUC it produces are NOT measurements and must")
    print("  not be reported as findings.")
    print(f"  Undo with: python scripts/simulate_term.py --clear")
    print("=" * 74)
    print(f"\n  {len(days)} school days, {args.start} .. {args.end}, seed {args.seed}")

    back_up(target, args.force)
    audit_from = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]

    config = load_config()
    stats = simulate(conn, config, days, args.seed)
    mark(conn, args.start, args.end, args.seed, audit_from)
    report(conn, days, stats)

    print("\n  Export it: python app.py --windowed -> Attendance records")
    print("  Undo it:   python scripts/simulate_term.py --clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
