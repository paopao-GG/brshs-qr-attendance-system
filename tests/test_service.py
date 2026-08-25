"""ScanService: payload in, screen state out."""
import pytest

from trackify.core.qrcodes import encode
from trackify.core.service import Presentation, ScanService

from .conftest import at

SECRET = "test-secret"


@pytest.fixture
def service(conn, config):
    import dataclasses
    cfg = dataclasses.replace(config, secrets=dataclasses.replace(
        config.secrets, qr_secret=SECRET))
    return ScanService(conn, cfg)


def payload(student_id):
    return encode(student_id, SECRET)


def test_first_scan_presents_in(service, student):
    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.state is Presentation.IN
    assert p.headline == "IN"
    assert p.student_name == "Juan Dela Cruz"
    assert p.initials == "JD"
    assert p.is_success
    assert p.notifications_queued == 1


def test_second_scan_presents_out(service, student):
    service.handle_scan(payload(student), at=at(7, 0))
    p = service.handle_scan(payload(student), at=at(16, 0))
    assert p.state is Presentation.OUT
    assert p.headline == "OUT"
    assert p.detail == "Goodbye"


def test_late_arrival_shown_in_detail(service, student):
    p = service.handle_scan(payload(student), at=at(7, 45))
    assert p.state is Presentation.IN
    assert p.detail == "Arrived late"


def test_early_departure_shown_in_detail(service, student):
    service.handle_scan(payload(student), at=at(7, 0))
    p = service.handle_scan(payload(student), at=at(12, 0))
    assert p.detail == "Early departure"


def test_rescan_presents_already(service, student):
    service.handle_scan(payload(student), at=at(7, 0))
    p = service.handle_scan(payload(student), at=at(7, 1))
    assert p.state is Presentation.ALREADY
    assert "7:00 AM" in p.detail
    assert p.student_name == "Juan Dela Cruz"


def test_forged_code_presents_unknown(service, student):
    forged = payload(student).replace(f"-{student}-", f"-{student + 99}-")
    p = service.handle_scan(forged, at=at(7, 0))
    assert p.state is Presentation.UNKNOWN_CODE
    assert p.hold_ms >= 5000, "error states must hold long enough to read"


def test_garbage_input_presents_misfire_not_unknown(service):
    """A scanner misfire is the operator's problem; a bad code is the student's.
    They must not look the same."""
    p = service.handle_scan("qwerty", at=at(7, 0))
    assert p.state is Presentation.MISFIRE
    assert p.state is not Presentation.UNKNOWN_CODE


def test_valid_shape_unknown_student(service):
    p = service.handle_scan("TRK-999-00000000", at=at(7, 0))
    assert p.state is Presentation.UNKNOWN_CODE


def test_inactive_student_rejected(service, conn, student):
    conn.execute("UPDATE students SET active = 0 WHERE id = ?", (student,))
    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.state is Presentation.UNKNOWN_CODE


def test_third_scan_needs_override(service, student):
    service.handle_scan(payload(student), at=at(7, 0))
    service.handle_scan(payload(student), at=at(16, 0))
    p = service.handle_scan(payload(student), at=at(16, 30))
    assert p.state is Presentation.NEEDS_OVERRIDE


def test_suspended_day(service, conn, config, student):
    from trackify.core.sessions import suspend_day
    suspend_day(conn, "2026-09-01", "Typhoon signal no. 2", config)
    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.state is Presentation.NO_CLASSES
    assert "Typhoon" in p.detail


def test_scan_is_atomic(service, conn, student):
    """One transaction: the scan row and its notification commit together."""
    service.handle_scan(payload(student), at=at(7, 0))
    scans = conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]
    notifs = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert scans == 1 and notifs == 1


def test_student_without_guardian_still_scans(service, conn, make_student):
    sid = make_student(guardian_mobile=None, first="Beatriz", last="Cortez")
    p = service.handle_scan(payload(sid), at=at(7, 0))
    assert p.state is Presentation.IN
    assert p.notifications_queued == 0, "attendance recorded, nobody notified"


# --- adviser on screen (flow.md 3 step 5) -----------------------------------

def test_adviser_is_shown_for_identity_confirmation(service, conn, section, student):
    conn.execute(
        """INSERT INTO users (username, password_hash, role, full_name, created_at)
           VALUES ('adviser', 'x', 'adviser', 'Tricia San Jose', '2026-09-01T00:00:00')"""
    )
    adviser_id = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute("UPDATE sections SET adviser_id = ? WHERE id = ?", (adviser_id, section))

    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.adviser == "Adviser: Tricia San Jose"


def test_section_with_no_adviser_still_scans(service, student):
    """A LEFT JOIN, not an inner one: a section between advisers must not close the
    gate on the students in it."""
    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.state is Presentation.IN
    assert p.adviser == ""


# --- end of day (flow.md 5.5) -----------------------------------------------

def test_close_day_marks_absent_and_queues_one_text_each(service, conn, make_student):
    make_student()
    make_student(guardian_mobile="639181112222")

    result = service.close_day("2026-09-01", at=at(16, 30))

    assert result.absent == 2
    triggers = [r[0] for r in conn.execute("SELECT trigger FROM notifications")]
    assert triggers == ["absent", "absent"]


def test_close_day_is_idempotent(service, conn, make_student):
    make_student()
    service.close_day("2026-09-01", at=at(16, 30))
    second = service.close_day("2026-09-01", at=at(16, 35))

    assert second.absent == 0
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 1


def test_close_day_does_not_text_about_a_missing_out_scan(service, conn, student):
    """The rule that must not regress: 'no departure recorded for your child' reads
    as a missing-child alert."""
    service.handle_scan(payload(student), at=at(7, 0))
    conn.execute("DELETE FROM notifications")

    result = service.close_day("2026-09-01", at=at(16, 30))

    assert (result.absent, result.exit_missing) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_close_day_on_a_suspended_day_texts_nobody(service, conn, make_student):
    from trackify.core.sessions import suspend_day
    make_student()
    make_student(guardian_mobile="639181112222")
    suspend_day(conn, "2026-09-01", "Class suspension", service.config)

    result = service.close_day("2026-09-01", at=at(16, 30))

    assert result.absent == 0
    assert result.skipped == "Class suspension"
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_closing_a_past_day_dates_the_text_to_that_day(service, conn, student):
    """Not to the moment the job runs -- a parent reading yesterday's absence must
    not be told it happened today."""
    from datetime import datetime
    service.close_day("2026-09-01", at=datetime(2026, 9, 3, 8, 0))

    body = conn.execute("SELECT body FROM notifications").fetchone()[0]
    assert "2026-09-01" in body


# --- screening (docs/prohibited-items.md) -----------------------------------

def _scan_id(service, student):
    p = service.handle_scan(payload(student), at=at(7, 0))
    return p.scan_id


def test_the_presentation_carries_the_arming_scan(service, student):
    """A screening binds to a scan and to nothing else -- the UI needs the id."""
    p = service.handle_scan(payload(student), at=at(7, 0))
    assert p.scan_id is not None
    assert p.student_id == student


def test_clear_screening_writes_nothing_about_the_student(service, conn, student):
    """Rule 1: only a guard-confirmed finding is ever linked to a named minor."""
    from trackify.core.screening import Outcome

    service.record_screening(_scan_id(service, student), Outcome.CLEAR)

    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM custody_items").fetchone()[0] == 0
    row = conn.execute("SELECT outcome FROM screening_events").fetchone()
    assert row["outcome"] == "clear"


def test_a_screening_cannot_exist_without_its_scan(service, conn):
    """Rule 2, enforced by the schema rather than by convention."""
    import sqlite3
    from trackify.core.screening import Outcome

    with pytest.raises(sqlite3.IntegrityError):
        service.record_screening(99999, Outcome.CLEAR)


def test_screening_events_has_no_student_id_column(conn):
    """The structural half of Rule 2: there is nowhere to put a guessed attribution."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(screening_events)")}
    assert "student_id" not in cols
    assert "scan_event_id" in cols


def test_amending_an_outcome_replaces_it_and_is_audited(service, conn, student):
    """The realistic sequence: Clear pressed, then something found."""
    from trackify.core.screening import Outcome

    scan = _scan_id(service, student)
    service.record_screening(scan, Outcome.CLEAR)
    service.record_screening(scan, Outcome.PROHIBITED, metal_detected=True)

    rows = conn.execute("SELECT outcome FROM screening_events").fetchall()
    assert [r["outcome"] for r in rows] == ["prohibited"]      # replaced, not doubled

    audit_row = conn.execute(
        "SELECT action, old_value, new_value FROM audit_log"
    ).fetchone()
    assert audit_row["action"] == "screening.amended"
    assert (audit_row["old_value"], audit_row["new_value"]) == ("clear", "prohibited")


def test_overridden_screening_demands_a_reason(service, student):
    from trackify.core.screening import Outcome

    scan = _scan_id(service, student)
    with pytest.raises(ValueError, match="requires a reason"):
        service.record_screening(scan, Outcome.OVERRIDDEN)

    service.record_screening(scan, Outcome.OVERRIDDEN, override_reason="late for exam")


def test_unresolved_screenings_surface_for_the_guard(service, conn, student, make_student):
    from trackify.core.screening import Outcome

    other = make_student(first="Ana", last="Reyes")
    service.record_screening(_scan_id(service, student), Outcome.CLEAR)
    p = service.handle_scan(payload(other), at=at(7, 5))
    service.record_screening(p.scan_id, Outcome.PENDING_VERIFICATION)

    unresolved = service.unresolved_screenings("2026-09-01")
    assert [r["first_name"] for r in unresolved] == ["Ana"]


def test_coverage_counts_scans_nobody_answered_for(service, student, make_student):
    """A scan with no screening row at all is not the same as 'not_screened', and
    both belong in the denominator."""
    from trackify.core.screening import Outcome

    other = make_student(first="Ana", last="Reyes")
    service.record_screening(_scan_id(service, student), Outcome.CLEAR)
    service.handle_scan(payload(other), at=at(7, 5))          # never answered

    coverage = service.screening_coverage("2026-09-01")
    assert coverage["clear"] == 1
    assert coverage["unrecorded"] == 1


def test_screening_never_touches_attendance(service, conn, student):
    """flow.md 3 step 6 -- the rule that must not bend."""
    from trackify.core.screening import Outcome

    scan = _scan_id(service, student)
    before = conn.execute("SELECT status, flags FROM attendance_days").fetchone()
    service.record_screening(scan, Outcome.PROHIBITED, metal_detected=True)
    after = conn.execute("SELECT status, flags FROM attendance_days").fetchone()

    assert tuple(before) == tuple(after)
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1


# --- incidents (docs/prohibited-items.md 6, 8) ------------------------------

def _screened(service, student, outcome=None):
    from trackify.core.screening import Outcome
    p = service.handle_scan(payload(student), at=at(7, 0))
    sid = service.record_screening(p.scan_id, outcome or Outcome.PROHIBITED,
                                   metal_detected=True)
    return sid, p.student_id


def test_incident_records_and_audits(service, conn, student):
    sid, student_id = _screened(service, student)

    service.record_incident(sid, student_id, "bladed", "folding knife, ~8 cm blade",
                            confirmed_by=None)

    row = conn.execute("SELECT * FROM incidents").fetchone()
    assert row["category"] == "bladed"
    assert row["severity"] == 4                     # defaulted from the category
    assert row["visibility"] == "restricted"        # RA 10173
    action = conn.execute("SELECT action FROM audit_log").fetchone()["action"]
    assert action == "incident.recorded"


def test_incident_sms_never_names_the_item(service, conn, student):
    """The worst failure this system could produce is a text saying a named child was
    carrying a knife, arriving at a wrong number."""
    from trackify.notify.queue import INCIDENT_FORBIDDEN

    sid, student_id = _screened(service, student)
    service.record_incident(sid, student_id, "bladed", "folding knife, ~8 cm blade")

    body = conn.execute(
        "SELECT body FROM notifications WHERE trigger = 'incident'"
    ).fetchone()["body"]
    assert "knife" not in body.lower()
    assert "bladed" not in body.lower()
    for word in INCIDENT_FORBIDDEN:
        assert word not in body.lower(), word
    assert "contact the school" in body


def test_every_category_produces_a_safe_body(service, conn, make_student):
    from trackify.core import screening as scr
    from trackify.notify.queue import INCIDENT_FORBIDDEN

    for cat in scr.CATEGORIES:
        s = make_student(first=f"S{cat.code}", last="X")
        sid, sid_student = _screened(service, s)
        service.record_incident(sid, sid_student, cat.code, f"a {cat.code} thing")

    for row in conn.execute("SELECT body FROM notifications WHERE trigger='incident'"):
        low = row["body"].lower()
        assert not [w for w in INCIDENT_FORBIDDEN if w in low]


def test_two_incidents_in_one_day_both_notify(service, conn, student):
    """Without a per-incident dedupe key the second is swallowed as a duplicate and
    nobody is told about it."""
    from trackify.core.screening import Outcome

    first_sid, sid_student = _screened(service, student)
    service.record_incident(first_sid, sid_student, "bladed", "knife")

    # A second scan the same day, past the debounce window, with its own screening.
    p2 = service.handle_scan(payload(student), at=at(13, 0))
    second_sid = service.record_screening(p2.scan_id, Outcome.PROHIBITED,
                                          metal_detected=True)
    service.record_incident(second_sid, sid_student, "blunt", "metal bar")

    count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE trigger = 'incident'"
    ).fetchone()[0]
    assert count == 2


def test_incident_needs_a_description(service, student):
    sid, student_id = _screened(service, student)
    with pytest.raises(ValueError, match="item_description"):
        service.record_incident(sid, student_id, "bladed", "  ")


def test_changed_severity_needs_a_reason(service, student):
    sid, student_id = _screened(service, student)
    with pytest.raises(ValueError, match="reason is required"):
        service.record_incident(sid, student_id, "bladed", "penknife", severity=2)

    service.record_incident(sid, student_id, "bladed", "penknife", severity=2,
                            severity_reason="blunt tip, under 3 cm")


def test_incident_cannot_exist_without_a_screening(service, conn, student):
    """Rule 1 and Rule 2 together: nothing reaches a named minor except through a
    guard-confirmed screening, which itself required the arming scan."""
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        service.record_incident(99999, student, "bladed", "knife")
