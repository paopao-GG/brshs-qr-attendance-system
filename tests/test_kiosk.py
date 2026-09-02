"""Kiosk UI, driven headlessly.

Typing into the hidden input is exactly what a USB HID scanner does, so these tests
exercise the real input path with no hardware.
"""

import dataclasses

import pytest

pytest.importorskip("qtpy")

from trackify.core.service import ScanService
from trackify.ui.worker import QueueStats

from .conftest import lrn_for, payload_for

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


def answer(qtbot, kiosk, button=None):
    """Clear the screening prompt, the way an operator does before the next student.

    The gate BLOCKS until a screening is answered, so any test that scans twice has
    to do this in between -- exactly as a real operator would.
    """
    if kiosk._awaiting_scan is not None:
        (button or kiosk.btn_no_metal).click()
        qtbot.wait(10)


def test_starts_in_waiting_state(kiosk):
    assert kiosk.stage.property("state") == "waiting"
    assert kiosk.waiting.isVisible()
    assert not kiosk.result.isVisible()


def test_scan_shows_student_and_in(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))

    assert kiosk.stage.property("state") == "in"
    assert kiosk.headline.text() == "IN"
    assert kiosk.name_label.text() == "Juan Dela Cruz"
    assert kiosk.avatar.text() == "JD"
    assert kiosk.time_text.text()
    assert kiosk.result.isVisible()


def test_second_scan_shows_out(qtbot, kiosk, student, conn):
    scan(qtbot, kiosk, payload_for(student))
    # Move the first scan outside the debounce window. Must stay on the SAME date:
    # scan_events.date is today's, so a different date would orphan the lookup.
    from datetime import datetime, timedelta
    conn.execute("UPDATE scan_events SET scanned_at = ?",
                 ((datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),))
    answer(qtbot, kiosk)
    scan(qtbot, kiosk, payload_for(student))

    assert kiosk.stage.property("state") == "out"
    assert kiosk.headline.text() == "OUT"


def test_rapid_rescan_shows_already(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))
    answer(qtbot, kiosk)
    scan(qtbot, kiosk, payload_for(student))

    assert kiosk.stage.property("state") == "already"
    assert "Already recorded" in kiosk.headline.text()


def test_forged_code_shows_red_unknown(qtbot, kiosk, student):
    """Editing the digits in a card is the proxy-attendance attack the signature
    exists to stop -- the LRN changes, the HMAC no longer covers it."""
    lrn = lrn_for(student)
    forged = payload_for(student).replace(f"-{lrn}-", f"-{int(lrn) + 77}-")
    scan(qtbot, kiosk, forged)

    assert kiosk.stage.property("state") == "unknown"
    assert kiosk.headline.text() == "Code not recognised"


def test_garbage_shows_misfire_not_unknown(qtbot, kiosk):
    scan(qtbot, kiosk, "asdfgh")
    assert kiosk.stage.property("state") == "neutral"
    assert kiosk.headline.text() == "Scan not read"


def test_a_misfire_result_is_not_the_waiting_state(qtbot, kiosk):
    """The two must stay separate. "waiting" is transparent so media/scan.jpg shows
    through at full strength, and that page carries dark ink; a MISFIRE is a RESULT,
    with light text, so it needs the opaque "neutral" ground. Collapse them and the
    result view puts light text on a pale photograph."""
    scan(qtbot, kiosk, "asdfgh")

    assert kiosk.result.isVisible()
    assert kiosk.stage.property("state") == "neutral"
    assert kiosk.stage.property("state") != "waiting"


def test_empty_input_ignored(qtbot, kiosk):
    scan(qtbot, kiosk, "")
    assert kiosk.waiting.isVisible()


def test_input_rate_limit_engages(qtbot, kiosk, student):
    """A stuck scanner must not flood the queue."""
    payload = payload_for(student)
    for _ in range(30):
        kiosk.scan_input.setText(payload)
        kiosk.scan_input.returnPressed.emit()
    qtbot.wait(10)
    assert kiosk.headline.text() == "Scanning too fast"


def test_screen_returns_to_waiting(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))
    assert kiosk.result.isVisible()
    kiosk._reset_timer.stop()
    kiosk._show_waiting()
    assert kiosk.waiting.isVisible()
    assert kiosk.stage.property("state") == "waiting"


def test_input_stays_focused_after_scan(qtbot, kiosk, student):
    """A dead input means the next student cannot scan."""
    scan(qtbot, kiosk, payload_for(student))
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


def test_halted_survives_the_next_stats_tick(kiosk):
    """The breaker fires once and the next stats update lands four seconds later.
    Overwriting it made the alarm invisible in practice, which is the opposite of what
    a spend cap is for."""
    kiosk.on_alarm("Daily SMS cap of 1000 reached")
    kiosk.on_stats(QueueStats(unsent=3, provider="gsm"))

    assert "HALTED" in kiosk.status_provider.text()
    assert kiosk.status_provider.property("alert") == "true"


def test_a_missing_module_is_named_in_the_status_bar(kiosk):
    """Otherwise the bar reads a confident "SMS: gsm" all day while the queue waits for
    hardware nobody has noticed is unplugged."""
    kiosk.on_stats(QueueStats(
        unsent=2, provider="gsm", provider_available=False,
        provider_detail="No serial port found. Is the SIM800C plugged in?",
    ))

    assert "unavailable" in kiosk.status_provider.text()
    assert "gsm" in kiosk.status_provider.text()
    assert kiosk.status_provider.property("alert") == "true",         "a module that cannot send must be visibly flagged, not quietly noted"
    assert "plugged in" in kiosk.status_provider.toolTip(),         "the reason is what tells the operator which cable to look at"


def test_the_indicator_clears_when_the_module_comes_back(kiosk):
    kiosk.on_stats(QueueStats(provider="gsm", provider_available=False,
                              provider_detail="not answering"))
    kiosk.on_stats(QueueStats(provider="gsm"))

    assert kiosk.status_provider.text() == "SMS: gsm"
    assert kiosk.status_provider.property("alert") == "false"
    assert kiosk.status_provider.toolTip() == ""


def test_a_healthy_provider_is_not_flagged(kiosk):
    """console and null are always available; nothing about them should turn amber.

    Amber in this bar means a fault to act on. A provider picked at startup is a
    setting, and colouring it like a dead camera teaches operators to read past the
    colour that does matter. It says so in words instead -- see the test below.
    """
    kiosk.on_stats(QueueStats(unsent=0, provider="console", provider_sends=False))

    assert kiosk.status_provider.property("alert") == "false"


def test_a_provider_that_sends_nothing_says_so(kiosk):
    """Otherwise the bar reads a confident "SMS: console" while the gate records
    arrivals and not one guardian is told anything."""
    kiosk.on_stats(QueueStats(unsent=0, provider="console", provider_sends=False))

    assert kiosk.status_provider.text() == "SMS: console (not sending)"


def test_the_bar_shows_the_reason_the_worker_gave(kiosk):
    """Why nothing is sending is computed in the worker, which is the only place that
    holds both the provider and the config. The bar renders it rather than guessing --
    the two causes leave different rows behind and the operator needs to know which."""
    kiosk.on_stats(QueueStats(
        provider="gsm", provider_sends=False,
        provider_detail="SMS_LIVE is false in .env, so this station is not sending.",
    ))

    assert kiosk.status_provider.text() == "SMS: gsm (not sending)"
    assert "SMS_LIVE" in kiosk.status_provider.toolTip()
    assert kiosk.status_provider.property("alert") == "false", \
        "a station switched off on purpose is not a fault"


def test_the_real_transport_is_not_labelled_not_sending(kiosk):
    """The guard on the branch above. A bar telling the operator that a working GSM
    module is not sending would be quietly alarming, every morning."""
    kiosk.on_stats(QueueStats(unsent=0, provider="gsm"))

    assert kiosk.status_provider.text() == "SMS: gsm"
    assert "not sending" not in kiosk.status_provider.text()
    assert kiosk.status_provider.property("alert") == "false"


# --- student photo (flow.md 3 step 5) ---------------------------------------

def _write_photo(path, size=(240, 320)):
    """A real image on disk, portrait so the square crop is exercised."""
    from PIL import Image
    Image.new("RGB", size, (120, 160, 200)).save(path)
    return path


def test_photo_fills_the_avatar_when_one_exists(qtbot, kiosk, conn, student, tmp_path):
    photo = _write_photo(tmp_path / "12.jpg")
    conn.execute("UPDATE students SET photo_path = ? WHERE id = ?", (str(photo), student))

    scan(qtbot, kiosk, payload_for(student))

    assert not kiosk.avatar.pixmap().isNull()
    assert kiosk.avatar.text() == ""
    assert kiosk.avatar.pixmap().width() == 180      # cropped square, not squashed


def test_unreadable_photo_falls_back_to_initials(qtbot, kiosk, conn, student, tmp_path):
    """A school roster will contain broken paths. An empty circle is worse than
    initials -- the guard needs something to compare against a face."""
    broken = tmp_path / "not-an-image.jpg"
    broken.write_text("this is not a JPEG")
    conn.execute("UPDATE students SET photo_path = ? WHERE id = ?", (str(broken), student))

    scan(qtbot, kiosk, payload_for(student))

    assert kiosk.avatar.text() == "JD"
    assert kiosk.avatar.pixmap().isNull()


def test_missing_photo_file_falls_back_to_initials(qtbot, kiosk, conn, student):
    conn.execute("UPDATE students SET photo_path = 'data/photos/nope.jpg' WHERE id = ?",
                 (student,))
    scan(qtbot, kiosk, payload_for(student))
    assert kiosk.avatar.text() == "JD"


def test_a_photo_does_not_linger_onto_the_next_student(
    qtbot, kiosk, conn, student, make_student, tmp_path
):
    """The failure this prevents is the worst one on this screen: one student's face
    shown beside another student's name."""
    photo = _write_photo(tmp_path / "a.jpg")
    conn.execute("UPDATE students SET photo_path = ? WHERE id = ?", (str(photo), student))
    other = make_student(first="Ana", last="Reyes")

    scan(qtbot, kiosk, payload_for(student))
    assert not kiosk.avatar.pixmap().isNull()

    answer(qtbot, kiosk)
    scan(qtbot, kiosk, payload_for(other))
    assert kiosk.avatar.pixmap().isNull()
    assert kiosk.avatar.text() == "AR"


# --- end-of-day close (flow.md 5.5) -----------------------------------------

def _scanned_today(conn, student_id, hour=7):
    """One scan on today's date, which is what makes it a day the gate actually ran."""
    from datetime import datetime, time

    today = datetime.now().date()
    conn.execute(
        """INSERT INTO scan_events (student_id, scanned_at, date, direction, method)
           VALUES (?, ?, ?, 'in', 'scan')""",
        (student_id, datetime.combine(today, time(hour, 0)).isoformat(timespec="seconds"),
         today.isoformat()),
    )


def test_day_is_closed_once_past_dismissal(qtbot, kiosk, conn, student, make_student):
    """One student scanned, one did not. The one who did not is absent.

    The scan is what makes this a day the gate actually ran -- the automatic job now
    skips a day with no scans at all, since that is the kiosk being opened rather than
    the school being attended. This test is about the dismissal-time comparison and the
    latch, so it gets a day that really happened.
    """
    from datetime import datetime, time

    make_student(guardian_mobile="639181112222")
    today = datetime.now().date()
    _scanned_today(conn, student)
    kiosk._closed_for = None
    after_dismissal = datetime.combine(today, time(17, 0))

    kiosk._maybe_close_day(after_dismissal)

    absent = conn.execute(
        "SELECT COUNT(*) FROM attendance_days WHERE status = 'absent'"
    ).fetchone()[0]
    assert absent == 1, "the one who never scanned"


def test_a_day_nobody_scanned_is_not_closed(qtbot, kiosk, conn, make_student):
    """Opening the kiosk in the evening must not invent a day of absences.

    Every student having no scan is only evidence of absence when OTHER students do
    have one. With zero scans the gate simply was not running, and marking the whole
    roster absent puts a fabricated 0% day into the attendance trend -- where it acts
    as a leverage point and multiplied the reported slope by 7.7 on real data.
    """
    from datetime import datetime, time

    make_student()
    conn.execute("DELETE FROM attendance_days")
    kiosk._closed_for = None

    kiosk._maybe_close_day(datetime.combine(datetime.now().date(), time(17, 0)))

    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_the_skip_still_latches(qtbot, kiosk, conn):
    """Otherwise the count query re-runs on every clock tick for the rest of the day."""
    from datetime import datetime, time

    conn.execute("DELETE FROM attendance_days")
    kiosk._closed_for = None
    day = datetime.now().date()

    kiosk._maybe_close_day(datetime.combine(day, time(17, 0)))

    assert kiosk._closed_for == day.isoformat()


def test_day_is_not_closed_before_dismissal(qtbot, kiosk, conn):
    from datetime import datetime, time

    # KioskWindow.__init__ runs _tick_clock() once, which closes the day for real when
    # the suite happens to run after dismissal. Without this the test passes all morning
    # and fails every evening, which looks exactly like a regression and is not one.
    conn.execute("DELETE FROM attendance_days")
    kiosk._closed_for = None

    kiosk._maybe_close_day(datetime.combine(datetime.now().date(), time(9, 0)))

    assert conn.execute("SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 0
    assert kiosk._closed_for is None


def test_the_latch_stops_it_running_every_second(qtbot, kiosk, conn, student):
    from datetime import datetime, time

    _scanned_today(conn, student)      # the guard skips a day with no scans at all
    kiosk._closed_for = None
    after = datetime.combine(datetime.now().date(), time(17, 0))
    calls = []
    original = kiosk.service.close_day
    kiosk.service.close_day = lambda *a, **k: (calls.append(1), original(*a, **k))[1]

    for _ in range(5):
        kiosk._maybe_close_day(after)

    assert len(calls) == 1


def test_a_failing_close_never_takes_the_gate_down(qtbot, kiosk, conn, student):
    """The kiosk's job is the gate. An end-of-day job that raises must not stop
    students being scanned in."""
    from datetime import datetime, time

    _scanned_today(conn, student)      # the guard skips a day with no scans at all
    kiosk._closed_for = None
    def boom(*a, **k):
        raise RuntimeError("database is locked")
    kiosk.service.close_day = boom

    kiosk._maybe_close_day(datetime.combine(datetime.now().date(), time(17, 0)))

    assert "failed" in kiosk.status_session.text()
    assert kiosk.isVisible()


def test_photo_is_masked_to_a_circle(qtbot, kiosk, conn, student, tmp_path):
    """QSS border-radius rounds a widget's background, not the pixmap drawn on it, so
    an unmasked photo lands as a hard square beside the round initials avatar."""
    photo = _write_photo(tmp_path / "square.jpg", size=(400, 400))
    conn.execute("UPDATE students SET photo_path = ? WHERE id = ?", (str(photo), student))

    scan(qtbot, kiosk, payload_for(student))

    image = kiosk.avatar.pixmap().toImage()
    assert image.pixelColor(2, 2).alpha() == 0                    # corner clipped away
    assert image.pixelColor(90, 90).alpha() == 255                # centre kept


# --- screening at the gate (docs/prohibited-items.md) -----------------------

def _outcomes(conn):
    return [r["outcome"] for r in conn.execute(
        "SELECT outcome FROM screening_events ORDER BY id")]


def _screening_buttons(kiosk):
    return [kiosk.btn_no_metal, kiosk.btn_metal, kiosk.btn_not_screened,
            kiosk.btn_common, kiosk.btn_school_tool, kiosk.btn_unfinished,
            kiosk.btn_back, *kiosk.category_buttons.values()]


def test_an_arrival_asks_the_metal_question_on_the_result_screen(qtbot, kiosk, student):
    """The fast path stays on the result screen: no page change for the ~95% of
    students who have nothing in their bag."""
    scan(qtbot, kiosk, payload_for(student))

    assert kiosk.screening_row.isVisible()
    assert kiosk.result.isVisible()
    assert not kiosk.inspection.isVisible()
    assert kiosk._awaiting_scan is not None
    assert "Tray:" in kiosk.screening_prompt.text()


def test_no_metal_records_clear(qtbot, kiosk, conn, student):
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_no_metal.click()
    qtbot.wait(10)

    assert _outcomes(conn) == ["clear"]
    assert conn.execute(
        "SELECT metal_detected FROM screening_events").fetchone()[0] == 0
    assert kiosk._awaiting_scan is None
    assert kiosk.waiting.isVisible()


def test_metal_detected_opens_the_inspection_page(qtbot, kiosk, conn, student):
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    assert kiosk.inspection.isVisible()
    assert not kiosk.result.isVisible()
    assert kiosk.stage.property("state") == "inspect"
    assert conn.execute("SELECT COUNT(*) FROM screening_events").fetchone()[0] == 0
    assert kiosk._awaiting_scan is not None


def test_the_student_is_identified_on_the_inspection_page(qtbot, kiosk, student):
    """The reason this used to live inside the result block. Putting one student's
    knife on another student's record is the worst mistake available here, and a
    separate page is exactly how that happens."""
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    assert kiosk.inspect_student.text() == "Juan Dela Cruz"
    assert kiosk.inspect_student.isVisible()
    assert kiosk.inspect_section.text() == "7-Rizal"


def test_the_decision_rule_is_on_the_inspection_page(qtbot, kiosk, student):
    """Not only in the incident dialog, which is reached AFTER choosing a category --
    one step too late for the rule to help choose it."""
    from trackify.core import screening as scr

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    texts = [w.text() for w in kiosk.inspection.findChildren(type(kiosk.inspect_student))]
    assert scr.DECISION_RULE in texts


def test_every_card_carries_an_icon(qtbot, kiosk, student):
    """A blank card is what a missing font or a malformed path looks like."""
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    cards = [kiosk.btn_common, kiosk.btn_school_tool, *kiosk.category_buttons.values()]
    for card in cards:
        assert not card.icon().isNull(), card.text()


def test_back_returns_to_the_result_page_having_recorded_nothing(
    qtbot, kiosk, conn, student
):
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.btn_back.click()
    qtbot.wait(10)

    assert kiosk.result.isVisible()
    assert not kiosk.inspection.isVisible()
    assert kiosk.stage.property("state") == "in"        # the result ground restored
    assert conn.execute("SELECT COUNT(*) FROM screening_events").fetchone()[0] == 0


def test_a_refused_scan_is_visible_during_an_inspection(
    qtbot, kiosk, conn, student, make_student
):
    """The refusal used to be written only to the result page's label, which is
    hidden here -- so the operator would see nothing and think the scanner had died."""
    other = make_student(first="Ana", last="Reyes")
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    scan(qtbot, kiosk, payload_for(other))

    assert kiosk.inspect_prompt.isVisible()
    assert "Juan Dela Cruz" in kiosk.inspect_prompt.text()
    assert "NOT recorded" in kiosk.inspect_prompt.text()
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1


def test_common_items_records_metal_but_no_finding(qtbot, kiosk, conn, student):
    """The distinction that measures whether the declaration tray is working."""
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.btn_common.click()
    qtbot.wait(10)

    assert _outcomes(conn) == ["common_items"]
    assert conn.execute(
        "SELECT metal_detected FROM screening_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_the_screen_never_closes_on_its_own(qtbot, kiosk, conn, student):
    """The whole point of removing the timeout: the guard may still be holding the
    bag open. A screen that closed itself would record an outcome nobody chose."""
    scan(qtbot, kiosk, payload_for(student))

    assert not kiosk._reset_timer.isActive()

    qtbot.wait(300)
    assert kiosk.result.isVisible()
    assert kiosk._awaiting_scan is not None
    assert conn.execute("SELECT COUNT(*) FROM screening_events").fetchone()[0] == 0


def test_a_non_screening_result_still_returns_on_its_own(qtbot, kiosk, student):
    """Only a pending screening blocks. An unknown code still clears itself."""
    scan(qtbot, kiosk, "TRK-1-deadbeef")
    assert kiosk._reset_timer.isActive()


def test_a_scan_is_refused_while_a_screening_is_pending(
    qtbot, kiosk, conn, student, make_student
):
    other = make_student(first="Ana", last="Reyes")
    scan(qtbot, kiosk, payload_for(student))
    scans_before = conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]

    scan(qtbot, kiosk, payload_for(other))

    assert conn.execute(
        "SELECT COUNT(*) FROM scan_events").fetchone()[0] == scans_before
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance_days").fetchone()[0] == 1
    assert "Juan Dela Cruz" in kiosk.screening_prompt.text()   # names who is waiting
    assert "NOT recorded" in kiosk.screening_prompt.text()
    assert kiosk.screening_prompt.property("alert") == "true"
    assert kiosk.result.isVisible()          # and it did not clear the screen


def test_the_refused_student_can_scan_again_once_answered(
    qtbot, kiosk, conn, student, make_student
):
    """A refusal writes nothing, so the retry is a first scan -- not caught by the
    debounce window, and recorded as a normal arrival."""
    other = make_student(first="Ana", last="Reyes")
    scan(qtbot, kiosk, payload_for(student))
    scan(qtbot, kiosk, payload_for(other))          # refused

    kiosk.btn_no_metal.click()
    qtbot.wait(10)
    scan(qtbot, kiosk, payload_for(other))          # retry

    assert kiosk.headline.text() == "IN"
    assert kiosk.name_label.text() == "Ana Reyes"
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 2


def test_not_screened_stays_reachable_by_hand(qtbot, kiosk, conn, student):
    """With no timeout and no supersede, this button is the only honest way out of a
    student who genuinely cannot be screened -- a flat detector, an emergency. Without
    it the operator's only exits are a fabricated clear or a frozen gate."""
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_not_screened.click()
    qtbot.wait(10)

    assert _outcomes(conn) == ["not_screened"]
    assert kiosk.waiting.isVisible()


def test_no_button_can_steal_the_scanner_enter(qtbot, kiosk, student):
    """A focused QPushButton consumes Enter, and a HID scanner ends every payload
    with Enter. One focusable button and scanning silently stops working."""
    from qtpy.QtCore import Qt

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    for button in _screening_buttons(kiosk):
        assert button.focusPolicy() == Qt.NoFocus, button.objectName()


def test_the_scanner_input_keeps_focus_on_the_inspection_page(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)
    assert kiosk.scan_input.hasFocus()


def test_the_tool_category_is_not_in_the_prohibited_row(kiosk):
    """School tool IS the tool category. Showing both would make the operator guess
    between two buttons that mean the same thing."""
    assert "tool" not in kiosk.category_buttons
    assert set(kiosk.category_buttons) == {"bladed", "blunt", "pointed", "other"}


def test_a_departure_is_not_screened(qtbot, kiosk, conn, student, config):
    """Students are swept on the way in, not on the way out."""
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_no_metal.click()
    qtbot.wait(10)

    import datetime as _dt
    later = _dt.datetime.now() + _dt.timedelta(minutes=config.scanning.debounce_minutes + 1)
    kiosk._render(kiosk.service.handle_scan(payload_for(student), at=later))

    assert not kiosk.screening_row.isVisible()
    assert kiosk._awaiting_scan is None


def test_a_failing_screening_never_takes_the_gate_down(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    kiosk.service.record_screening = boom

    kiosk.btn_no_metal.click()
    qtbot.wait(10)

    assert "not recorded" in kiosk.status_session.text()
    assert kiosk.isVisible()


def test_screening_can_be_turned_off_entirely(qtbot, conn, config, student):
    import dataclasses

    from trackify.core.service import ScanService
    from trackify.ui.kiosk import KioskWindow

    cfg = dataclasses.replace(
        config,
        secrets=dataclasses.replace(config.secrets, qr_secret=SECRET),
        screening=dataclasses.replace(config.screening, enabled=False),
    )
    window = KioskWindow(ScanService(conn, cfg), windowed=True)
    qtbot.addWidget(window)
    window.show()
    window._submit(payload_for(student))
    qtbot.wait(10)

    assert not window.screening_row.isVisible()
    assert window._awaiting_scan is None
    assert window._reset_timer.isActive()          # and it still auto-returns


# --- the detail dialogs -----------------------------------------------------

def test_a_category_button_records_the_incident(qtbot, kiosk, conn, student, monkeypatch):
    from qtpy.QtWidgets import QDialog

    from trackify.ui import screening as scr

    def fake_exec(self):
        self.description.setText("folding knife, ~8 cm blade")
        return QDialog.Accepted

    monkeypatch.setattr(scr.IncidentDialog, "exec", fake_exec, raising=False)

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.category_buttons["bladed"].click()
    qtbot.wait(10)

    row = conn.execute("SELECT * FROM incidents").fetchone()
    assert row["category"] == "bladed"
    assert row["item_description"] == "folding knife, ~8 cm blade"
    assert _outcomes(conn) == ["prohibited"]


def test_the_category_button_preselects_it_in_the_dialog(qtbot, kiosk, student, monkeypatch):
    from qtpy.QtWidgets import QDialog

    from trackify.ui import screening as scr

    seen = {}

    def fake_exec(self):
        seen["category"] = self.category.currentData()
        seen["severity"] = self.severity.value()
        return QDialog.Rejected

    monkeypatch.setattr(scr.IncidentDialog, "exec", fake_exec, raising=False)

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.category_buttons["pointed"].click()
    qtbot.wait(10)

    assert seen["category"] == "pointed"
    assert seen["severity"] == 3          # the category default came with it


def test_cancelling_the_dialog_leaves_an_unfinished_inspection(
    qtbot, kiosk, conn, student, monkeypatch
):
    """Not 'clear', and not nothing at all: the inspection genuinely did not finish."""
    from qtpy.QtWidgets import QDialog

    from trackify.ui import screening as scr

    monkeypatch.setattr(scr.IncidentDialog, "exec",
                        lambda self: QDialog.Rejected, raising=False)

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.category_buttons["bladed"].click()
    qtbot.wait(10)

    assert _outcomes(conn) == ["pending_verification"]
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_school_tool_opens_custody_and_holds_the_item(
    qtbot, kiosk, conn, student, monkeypatch
):
    from qtpy.QtWidgets import QDialog

    from trackify.ui import screening as scr

    def fake_exec(self):
        self.description.setText("utility cutter, yellow handle")
        self.purpose.setText("for Art, 4th period")
        self.storage_ref.setText("TAG-014")
        return QDialog.Accepted

    monkeypatch.setattr(scr.CustodyDialog, "exec", fake_exec, raising=False)

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    kiosk.btn_school_tool.click()
    qtbot.wait(10)

    row = conn.execute("SELECT * FROM custody_items").fetchone()
    assert row["status"] == "held"
    assert row["storage_ref"] == "TAG-014"
    assert row["purpose"] == "for Art, 4th period"
    assert _outcomes(conn) == ["school_hazard"]


def test_the_incident_dialog_prefills_severity_from_the_category(qtbot):
    from trackify.ui.screening import IncidentDialog
    dialog = IncidentDialog("Juan Dela Cruz")
    qtbot.addWidget(dialog)

    dialog.category.setCurrentIndex(0)                       # bladed
    assert dialog.severity.value() == 4
    dialog.category.setCurrentIndex(3)                       # tool
    assert dialog.severity.value() == 1


def test_the_incident_dialog_refuses_an_empty_description(qtbot):
    from trackify.ui.screening import IncidentDialog
    dialog = IncidentDialog("Juan Dela Cruz")
    qtbot.addWidget(dialog)

    dialog.description.setText("")
    dialog._try_accept()

    assert dialog.isVisible() is False or dialog.result() == 0
    assert "item_description" in dialog.error.text()


def test_the_custody_dialog_requires_a_tag(qtbot):
    """An untagged item in a box of forty is effectively lost."""
    from trackify.ui.screening import CustodyDialog
    dialog = CustodyDialog("Juan Dela Cruz")
    qtbot.addWidget(dialog)

    dialog.description.setText("cutter")
    dialog.purpose.setText("art")
    dialog.storage_ref.setText("")
    dialog._try_accept()

    assert "tag" in dialog.error.text()


def test_the_strip_avatar_is_fitted_not_clipped(qtbot, kiosk, conn, student, tmp_path):
    """A 180px pixmap in a 52px label is clipped, not scaled -- the photo would show
    as a square crop of its top-left corner."""
    photo = _write_photo(tmp_path / "p.jpg", size=(400, 400))
    conn.execute("UPDATE students SET photo_path = ? WHERE id = ?", (str(photo), student))

    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    from trackify.ui.kiosk import AVATAR_STRIP_PX
    assert kiosk.inspect_avatar.pixmap().width() == AVATAR_STRIP_PX


def test_the_strip_falls_back_to_initials(qtbot, kiosk, student):
    scan(qtbot, kiosk, payload_for(student))
    kiosk.btn_metal.click()
    qtbot.wait(10)

    assert kiosk.inspect_avatar.text() == "JD"
    assert kiosk.inspect_avatar.pixmap().isNull()


def test_the_scanner_input_stays_invisible_under_the_stylesheet(qtbot, kiosk):
    """The hazard the records styling introduced: a general QLineEdit rule that sets
    padding or min-height fights this widget's max-height and puts a stray input box
    under the clock on the gate screen."""
    from pathlib import Path

    from qtpy.QtWidgets import QApplication

    QApplication.instance().setStyleSheet(
        Path("trackify/ui/style.qss").read_text(encoding="utf8")
    )
    kiosk.show()
    qtbot.wait(20)

    assert kiosk.scan_input.height() <= 2, kiosk.scan_input.height()
    assert kiosk.scan_input.sizeHint().height() <= 2 or kiosk.scan_input.height() <= 2
