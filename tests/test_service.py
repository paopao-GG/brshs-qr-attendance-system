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
