"""The threading contract.

An SMS send taking seconds -- and a 2G submit always does -- must never freeze the scan
station. This is the failure mode that kills PyQt applications in the field, so it is
tested rather than assumed.
"""

import dataclasses
import time

import pytest

pytest.importorskip("qtpy")

from qtpy.QtCore import QThread

from trackify.core import db
from trackify.core.service import ScanService
from trackify.notify.provider import Availability, NotificationProvider, SendResult
from trackify.ui.worker import QueueStats, SmsWorker

from .conftest import lrn_for, payload_for

SECRET = "test-secret"


class SlowProvider(NotificationProvider):

    """Simulates a slow network. If this ran on the UI thread the app would hang."""

    name = "slow"

    def __init__(self, delay=0.25):
        self.delay = delay
        self.sent = []
        self.threads = []          # which thread each send() ran on

    def send(self, recipient, body):
        self.threads.append(QThread.currentThread())
        time.sleep(self.delay)
        self.sent.append((recipient, body))
        return SendResult(ok=True, provider_message_id=f"slow-{len(self.sent)}")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "worker.db"
    conn = db.connect(path)
    db.init_db(conn)
    sec = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Rizal', 7)"
    ).lastrowid
    for i in range(1, 4):
        conn.execute(
            """INSERT INTO students (lrn, first_name, last_name, section_id,
               guardian_name, guardian_mobile, consent_on_file, created_at)
               VALUES (?, ?, 'Dela Cruz', ?, 'Maria', ?, 1, ?)""",
            # lrn_for(i), not an arbitrary number: payload_for() below signs the LRN,
            # so the two have to agree or every scan lands on "Student not found".
            (lrn_for(i), f"Student{i}", sec, f"63917000000{i}", db.utcnow()),
        )
    yield path
    db.close_all()


def test_network_call_runs_off_the_ui_thread(qtbot, db_path, config):
    """The actual contract: provider.send() must not execute on the UI thread."""
    from datetime import datetime, timedelta

    from trackify.core.attendance import Trigger
    from trackify.notify import queue

    ui_thread = QThread.currentThread()

    conn = db.connect(db_path)
    queue.enqueue(conn, 1, Trigger.ARRIVAL, datetime.now(), config, direction="in")
    conn.execute("UPDATE notifications SET queued_at = ?",
                 ((datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds"),))

    provider = SlowProvider(delay=0.05)
    worker = SmsWorker(provider, config, db_path=db_path, interval_ms=100)

    thread = QThread()
    worker.moveToThread(thread)

    seen = []
    worker.stats_changed.connect(seen.append)
    thread.started.connect(worker.start)
    thread.start()

    qtbot.waitUntil(lambda: len(provider.threads) > 0, timeout=4000)

    worker.stop_from_ui()
    thread.quit()
    assert thread.wait(3000)

    assert provider.threads[0] is not ui_thread,         "provider.send() ran on the UI thread -- the kiosk would freeze"
    assert seen and isinstance(seen[0], QueueStats)
    assert seen[0].provider == "slow"


def test_ui_stays_responsive_while_queue_drains(qtbot, db_path, config):
    """Scan repeatedly against a slow provider; the kiosk must keep responding."""
    from trackify.ui.kiosk import KioskWindow

    cfg = dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, qr_secret=SECRET)
    )
    conn = db.connect(db_path)
    window = KioskWindow(ScanService(conn, cfg), windowed=True)
    qtbot.addWidget(window)
    window.show()

    provider = SlowProvider(delay=0.2)
    worker = SmsWorker(provider, cfg, db_path=db_path, interval_ms=50)
    thread = QThread()
    worker.moveToThread(thread)
    worker.stats_changed.connect(window.on_stats)
    thread.started.connect(worker.start)
    thread.start()

    try:
        # Scan while the worker is busy. Each of these must return promptly.
        for student_id in (1, 2, 3):
            started = time.perf_counter()
            window.scan_input.setText(payload_for(student_id))
            window.scan_input.returnPressed.emit()
            elapsed = time.perf_counter() - started

            assert elapsed < 0.15, (
                f"scan blocked for {elapsed:.3f}s -- the UI thread is waiting on I/O"
            )
            assert window.headline.text() == "IN"
            qtbot.wait(30)
    finally:
        worker.stop_from_ui()
        thread.quit()
        thread.wait(3000)


def test_stop_from_ui_does_not_cross_threads(qtbot, db_path, config):
    """Stopping the drain timer from the UI thread is a Qt threading violation --
    it logs 'Timers cannot be stopped from another thread' and leaves the timer
    running. stop_from_ui() hops onto the worker thread to do it properly.

    Asynchronously, hence waitUntil: the request is posted to the worker's event loop
    rather than waited on, so a send already in flight finishes first. What is asserted
    is that the timer does stop, and that it was stopped by the thread that owns it.
    """
    worker = SmsWorker(SlowProvider(), config, db_path=db_path, interval_ms=100)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()

    qtbot.waitUntil(lambda: worker._timer is not None, timeout=4000)
    worker.stop_from_ui()

    qtbot.waitUntil(lambda: not worker._timer.isActive(), timeout=4000)
    thread.quit()
    assert thread.wait(3000)


def test_stop_from_ui_returns_before_a_slow_send_finishes(qtbot, db_path, config):
    """The kiosk must close now, not when the modem is done.

    stop_from_ui() used to be a BlockingQueuedConnection, which held the UI thread
    until the worker returned to its event loop. Against a serial modem that is a
    60-second send, or minutes of AT timeouts on a port that never answers -- quitting
    froze for exactly as long as the hardware was broken.
    """
    worker = SmsWorker(SlowProvider(delay=2.0), config, db_path=db_path,
                       interval_ms=100)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()

    qtbot.waitUntil(lambda: worker._timer is not None, timeout=4000)

    started = time.monotonic()
    worker.stop_from_ui()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"stop_from_ui blocked the UI thread for {elapsed:.1f}s"
    assert worker._stopping, "no further drain may begin once stop has been asked for"

    thread.quit()
    thread.wait(5000)


def test_stale_sending_rows_recovered_on_start(qtbot, db_path, config):
    """A worker that died mid-send leaves rows claimed. Restart must park them as
    'unknown' and raise an alarm -- never silently resend."""
    from datetime import datetime, timedelta

    conn = db.connect(db_path)
    conn.execute(
        """INSERT INTO notifications
           (student_id, guardian_mobile, trigger, idempotency_key, body,
            status, claimed_at, event_at, queued_at)
           VALUES (1, '639170000001', 'arrival', 'stale-key', 'body',
                   'sending', ?, ?, ?)""",
        ((datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
         db.utcnow(), db.utcnow()),
    )

    provider = SlowProvider()
    worker = SmsWorker(provider, config, db_path=db_path, interval_ms=100)
    thread = QThread()
    worker.moveToThread(thread)

    alarms = []
    worker.alarm.connect(alarms.append)
    thread.started.connect(worker.start)
    thread.start()

    qtbot.waitUntil(lambda: len(alarms) > 0, timeout=4000)
    worker.stop_from_ui()
    thread.quit()
    thread.wait(3000)

    status = conn.execute(
        "SELECT status FROM notifications WHERE idempotency_key = 'stale-key'"
    ).fetchone()[0]
    assert status == "unknown"
    assert provider.sent == [], "must not resend an ambiguous message"
    assert "reconcil" in alarms[0].lower()


# --- the transport is not there ---------------------------------------------
#
# A missing module is not a failed message. Draining into one runs every queued
# notification through _retry_or_fail, and the backoff ladder reaches retry_limit in
# under two hours -- so the morning's notifications would be permanently 'failed' by
# mid-morning and plugging the module in at noon would send nothing.


class AbsentProvider(NotificationProvider):
    """Stands in for a GSM module that is unplugged."""

    name = "gsm"

    def __init__(self, reason="No serial port found. Is the SIM800C plugged in?"):
        self.reason = reason
        self.present = False
        self.sent = []

    def available(self):
        if self.present:
            return Availability(ok=True)
        return Availability(ok=False, reason=self.reason)

    def send(self, recipient, body):
        self.sent.append((recipient, body))
        return SendResult(ok=True, provider_message_id=f"late-{len(self.sent)}")


def _queue_one(db_path, config):
    from datetime import datetime, timedelta

    from trackify.core.attendance import Trigger
    from trackify.notify import queue

    conn = db.connect(db_path)
    queue.enqueue(conn, 1, Trigger.ARRIVAL, datetime.now(), config, direction="in")
    conn.execute("UPDATE notifications SET queued_at = ?",
                 ((datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds"),))
    return conn


def _run(qtbot, worker, until, timeout=4000):
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()
    try:
        qtbot.waitUntil(until, timeout=timeout)
    finally:
        worker.stop_from_ui()
        thread.quit()
        thread.wait(3000)


def test_the_queue_is_not_drained_while_the_module_is_absent(qtbot, db_path, config):
    """The row must be left exactly as it was: still pending, retry budget untouched."""
    conn = _queue_one(db_path, config)
    provider = AbsentProvider()
    worker = SmsWorker(provider, config, db_path=db_path, interval_ms=50)

    seen = []
    worker.stats_changed.connect(seen.append)
    _run(qtbot, worker, lambda: len(seen) > 0)

    assert provider.sent == [], "nothing may be attempted against a module that is absent"
    row = conn.execute(
        "SELECT status, retry_count FROM notifications").fetchone()
    assert row["status"] == "pending"
    assert row["retry_count"] == 0, "an absent module must not spend the retry budget"


def test_the_absent_module_is_reported_to_the_ui(qtbot, db_path, config):
    _queue_one(db_path, config)
    worker = SmsWorker(AbsentProvider(), config, db_path=db_path, interval_ms=50)

    seen = []
    worker.stats_changed.connect(seen.append)
    _run(qtbot, worker, lambda: len(seen) > 0)

    stats = seen[-1]
    assert stats.provider_available is False
    assert "plugged in" in stats.provider_detail
    assert stats.unsent == 1, "the waiting message is still counted"


def test_the_backlog_goes_out_when_the_module_comes_back(qtbot, db_path, config):
    """The point of holding rather than failing: the same message still sends."""
    conn = _queue_one(db_path, config)
    provider = AbsentProvider()
    worker = SmsWorker(provider, config, db_path=db_path, interval_ms=50)

    seen = []
    worker.stats_changed.connect(seen.append)

    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()
    try:
        qtbot.waitUntil(lambda: len(seen) > 0, timeout=4000)
        assert provider.sent == []

        provider.present = True            # the cable goes in
        qtbot.waitUntil(lambda: len(provider.sent) > 0, timeout=4000)
    finally:
        worker.stop_from_ui()
        thread.quit()
        thread.wait(3000)

    assert conn.execute("SELECT status FROM notifications").fetchone()["status"] == "sent"


def test_a_provider_with_no_opinion_is_always_drained(qtbot, db_path, config):
    """Console and Null inherit the default. They must not be gated by any of this."""
    conn = _queue_one(db_path, config)
    provider = SlowProvider(delay=0.01)
    worker = SmsWorker(provider, config, db_path=db_path, interval_ms=50)

    _run(qtbot, worker, lambda: len(provider.sent) > 0)
    assert conn.execute("SELECT status FROM notifications").fetchone()["status"] == "sent"


# --- why nothing is being sent ----------------------------------------------

def _worker(provider, sms_live):
    """A worker built without starting its thread. _sends and the reason are pure."""
    import dataclasses

    from trackify.core.config import load_config
    from trackify.ui.worker import SmsWorker

    cfg = load_config()
    cfg = dataclasses.replace(
        cfg, secrets=dataclasses.replace(cfg.secrets, sms_live=sms_live)
    )
    return SmsWorker(provider, cfg)


def test_a_live_station_on_a_real_transport_is_sending():
    from trackify.notify.gsm import GsmProvider

    assert _worker(GsmProvider("/dev/null"), sms_live=True)._sends is True


def test_a_real_transport_is_not_sending_when_the_station_is_not_live():
    """The case that matters at the gate: the module is plugged in and answering, and
    the bar must still say nothing is going out."""
    from trackify.notify.gsm import GsmProvider

    worker = _worker(GsmProvider("/dev/null"), sms_live=False)

    assert worker._sends is False
    assert "SMS_LIVE" in worker._not_sending_reason
    assert "suppressed" in worker._not_sending_reason, \
        "say which row the operator will find afterwards"


def test_a_software_provider_is_not_sending_even_when_live():
    """SMS_LIVE=true cannot make console send. Both halves have to hold."""
    from trackify.notify.provider import ConsoleProvider

    worker = _worker(ConsoleProvider(), sms_live=True)

    assert worker._sends is False
    assert "console" in worker._not_sending_reason
    assert "'sent'" in worker._not_sending_reason, \
        "console marks rows sent, not suppressed -- a different trap from SMS_LIVE"
