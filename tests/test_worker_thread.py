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
from trackify.core.qrcodes import encode
from trackify.core.service import ScanService
from trackify.notify.provider import NotificationProvider, SendResult
from trackify.ui.worker import QueueStats, SmsWorker

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
            (f"100{i}", f"Student{i}", sec, f"63917000000{i}", db.utcnow()),
        )
    yield path
    db.close_all()


def test_network_call_runs_off_the_ui_thread(qtbot, db_path, config):
    """The actual contract: provider.send() must not execute on the UI thread."""
    from trackify.core.attendance import Trigger
    from trackify.notify import queue
    from datetime import datetime, timedelta

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
            window.scan_input.setText(encode(student_id, SECRET))
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
    running. stop_from_ui() hops onto the worker thread to do it properly."""
    worker = SmsWorker(SlowProvider(), config, db_path=db_path, interval_ms=100)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()

    qtbot.waitUntil(lambda: worker._timer is not None, timeout=4000)
    worker.stop_from_ui()

    assert not worker._timer.isActive(), "the drain timer is still running"
    thread.quit()
    assert thread.wait(3000)


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
