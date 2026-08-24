"""Kiosk UI, driven headlessly.

Typing into the hidden input is exactly what a USB HID scanner does, so these tests
exercise the real input path with no hardware.
"""

import dataclasses

import pytest

pytest.importorskip("qtpy")

from trackify.core.qrcodes import encode
from trackify.core.service import ScanService
from trackify.ui.worker import QueueStats

SECRET = "test-secret"


@pytest.fixture
def kiosk(qtbot, conn, config, student):
    from trackify.ui.kiosk import KioskWindow

    cfg = dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, qr_secret=SECRET)
    )
    window = KioskWindow(ScanService(conn, cfg), windowed=True)
    qtbot.addWidget(window)
    window.show()          # isVisible() stays False until shown, even offscreen
    window.activateWindow()
    return window


def scan(qtbot, kiosk, payload):
    """Simulate the scanner: type the payload, press Enter."""
    kiosk.scan_input.setText(payload)
    kiosk.scan_input.returnPressed.emit()
    qtbot.wait(10)


def test_starts_in_waiting_state(kiosk):
    assert kiosk.stage.property("state") == "neutral"
    assert kiosk.waiting.isVisible()
    assert not kiosk.result.isVisible()


def test_scan_shows_student_and_in(qtbot, kiosk, student):
    scan(qtbot, kiosk, encode(student, SECRET))

    assert kiosk.stage.property("state") == "in"
    assert kiosk.headline.text() == "IN"
    assert kiosk.name_label.text() == "Juan Dela Cruz"
    assert kiosk.avatar.text() == "JD"
    assert kiosk.time_text.text()
    assert kiosk.result.isVisible()


def test_second_scan_shows_out(qtbot, kiosk, student, conn):
    scan(qtbot, kiosk, encode(student, SECRET))
    # Move the first scan outside the debounce window. Must stay on the SAME date:
    # scan_events.date is today's, so a different date would orphan the lookup.
    from datetime import datetime, timedelta
    conn.execute("UPDATE scan_events SET scanned_at = ?",
                 ((datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),))
    scan(qtbot, kiosk, encode(student, SECRET))

    assert kiosk.stage.property("state") == "out"
    assert kiosk.headline.text() == "OUT"


def test_rapid_rescan_shows_already(qtbot, kiosk, student):
    scan(qtbot, kiosk, encode(student, SECRET))
    scan(qtbot, kiosk, encode(student, SECRET))

    assert kiosk.stage.property("state") == "already"
    assert "Already recorded" in kiosk.headline.text()


def test_forged_code_shows_red_unknown(qtbot, kiosk, student):
    forged = encode(student, SECRET).replace(f"-{student}-", f"-{student + 77}-")
    scan(qtbot, kiosk, forged)

    assert kiosk.stage.property("state") == "unknown"
    assert kiosk.headline.text() == "Code not recognised"


def test_garbage_shows_misfire_not_unknown(qtbot, kiosk):
    scan(qtbot, kiosk, "asdfgh")
    assert kiosk.stage.property("state") == "neutral"
    assert kiosk.headline.text() == "Scan not read"


def test_empty_input_ignored(qtbot, kiosk):
    scan(qtbot, kiosk, "")
    assert kiosk.waiting.isVisible()


def test_input_rate_limit_engages(qtbot, kiosk, student):
    """A stuck scanner must not flood the queue."""
    payload = encode(student, SECRET)
    for _ in range(30):
        kiosk.scan_input.setText(payload)
        kiosk.scan_input.returnPressed.emit()
    qtbot.wait(10)
    assert kiosk.headline.text() == "Scanning too fast"


def test_screen_returns_to_waiting(qtbot, kiosk, student):
    scan(qtbot, kiosk, encode(student, SECRET))
    assert kiosk.result.isVisible()
    kiosk._reset_timer.stop()
    kiosk._show_waiting()
    assert kiosk.waiting.isVisible()
    assert kiosk.stage.property("state") == "neutral"


def test_input_stays_focused_after_scan(qtbot, kiosk, student):
    """A dead input means the next student cannot scan."""
    scan(qtbot, kiosk, encode(student, SECRET))
    kiosk._show_waiting()
    assert kiosk.focusWidget() is kiosk.scan_input


def test_status_bar_reflects_unsent_backlog(kiosk):
    kiosk.on_stats(QueueStats(unsent=0, provider="console"))
    assert kiosk.status_unsent.property("alert") == "false"

    kiosk.on_stats(QueueStats(unsent=7, provider="console"))
    assert "7 unsent" in kiosk.status_unsent.text()
    assert kiosk.status_unsent.property("alert") == "true", \
        "a backlog must be visibly flagged, not silently counted"


def test_breaker_alarm_shows_halted(kiosk):
    kiosk.on_alarm("Daily SMS cap of 1000 reached")
    assert "HALTED" in kiosk.status_provider.text()
