"""The camera path, driven with no camera attached.

Nothing here opens a hardware device: the panel is either disabled or pointed at a
device name that cannot match, and frames are synthesised. That keeps the suite runnable
on a build machine and on a laptop with the lid shut.
"""

import dataclasses

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("zxingcpp")
qrcode = pytest.importorskip("qrcode")

from PIL import Image
from qtpy.QtCore import QMetaObject, Q_ARG, Qt, QThread
from qtpy.QtGui import QImage

from trackify.core.config import CameraConfig
from trackify.core.qrcodes import encode
from trackify.core.service import ScanService
from trackify.ui.camera import (
    CameraPanel, DecodeWorker, choose_format, is_compressed, pick_camera,
)

SECRET = "test-secret"


# -- fakes ------------------------------------------------------------------

class FakeDevice:
    def __init__(self, name):
        self._name = name

    def description(self):
        return self._name


class FakeSize:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


class FakeFormat:
    def __init__(self, w, h, fps, pixel="Format_NV12"):
        self._size = FakeSize(w, h)
        self._fps = fps
        self._pixel = pixel

    def resolution(self):
        return self._size

    def maxFrameRate(self):
        return self._fps

    def pixelFormat(self):
        return f"PixelFormat.{self._pixel}"


def qimage_of(payload):
    """A 1280x720 grayscale QImage containing the payload, as a camera would deliver."""
    code = qrcode.make(payload).convert("L").resize((220, 220), Image.NEAREST)
    canvas = Image.new("L", (1280, 720), 210)
    canvas.paste(code, (500, 250))
    arr = np.ascontiguousarray(np.array(canvas))
    return QImage(
        arr.data, 1280, 720, arr.strides[0], QImage.Format_Grayscale8
    ).copy()


# -- device selection -------------------------------------------------------

def test_pick_camera_defaults_to_the_first():
    devices = [FakeDevice("BisonCam,NB Pro"), FakeDevice("Web Camera")]
    assert pick_camera(devices, "") is devices[0]


def test_pick_camera_matches_a_substring_case_insensitively():
    devices = [FakeDevice("BisonCam,NB Pro"), FakeDevice("Web Camera")]
    assert pick_camera(devices, "web") is devices[1]


def test_pick_camera_returns_none_when_nothing_matches():
    """Better a clear failure than silently using the wrong lens."""
    assert pick_camera([FakeDevice("BisonCam,NB Pro")], "logitech") is None


def test_pick_camera_handles_no_devices():
    assert pick_camera([], "") is None
    assert pick_camera(None, "web") is None


def test_choose_format_picks_the_closest_resolution():
    device = FakeDevice("x")
    device.videoFormats = lambda: [
        FakeFormat(640, 480, 30), FakeFormat(1280, 720, 30), FakeFormat(1920, 1080, 30)
    ]
    assert choose_format(device, 1280, 720).resolution().width() == 1280


def test_choose_format_prefers_the_higher_frame_rate_on_a_tie():
    device = FakeDevice("x")
    device.videoFormats = lambda: [FakeFormat(1280, 720, 15), FakeFormat(1280, 720, 30)]
    assert choose_format(device, 1280, 720).maxFrameRate() == 30


def test_choose_format_avoids_mjpeg_at_the_same_resolution():
    """An MJPEG frame must be JPEG-decoded before it can be scanned -- wasted work on
    every frame, when the same camera offers NV12 at the same size."""
    device = FakeDevice("x")
    device.videoFormats = lambda: [
        FakeFormat(1280, 720, 30, "Format_Jpeg"),
        FakeFormat(1280, 720, 30, "Format_NV12"),
    ]
    assert not is_compressed(choose_format(device, 1280, 720))


def test_choose_format_takes_mjpeg_over_a_worse_resolution():
    """Resolution decides whether a small code resolves at all; it wins."""
    device = FakeDevice("x")
    device.videoFormats = lambda: [
        FakeFormat(1280, 720, 30, "Format_Jpeg"),
        FakeFormat(320, 240, 30, "Format_NV12"),
    ]
    assert choose_format(device, 1280, 720).resolution().width() == 1280


# -- the decode worker ------------------------------------------------------

class RecordingWorker(DecodeWorker):
    """Records which thread each decode actually ran on."""

    def __init__(self, config):
        super().__init__(config)
        self.threads = []

    def handle_frame(self, image):
        self.threads.append(QThread.currentThread())
        super().handle_frame(image)


def test_decoding_runs_off_the_ui_thread(qtbot):
    """The contract that keeps the scan station responsive.

    Decoding every frame on the UI thread is the failure mode this whole module is
    shaped around, so it is asserted rather than assumed.
    """
    ui_thread = QThread.currentThread()
    worker = RecordingWorker(CameraConfig(cooldown_ms=0))
    thread = QThread()
    worker.moveToThread(thread)

    detected = []
    worker.code_detected.connect(detected.append)
    thread.start()
    try:
        payload = encode(1, SECRET)
        QMetaObject.invokeMethod(
            worker, "handle_frame", Qt.QueuedConnection,
            Q_ARG(QImage, qimage_of(payload)),
        )
        qtbot.waitUntil(lambda: bool(detected), timeout=4000)

        assert detected == [payload]
        assert worker.threads[0] is not ui_thread, \
            "decoding ran on the UI thread -- the kiosk would stutter on every frame"
    finally:
        thread.quit()
        assert thread.wait(3000)


def test_worker_suppresses_a_held_card(qtbot):
    """The gate is applied inside the worker, so repeats never cross the thread."""
    worker = DecodeWorker(CameraConfig(cooldown_ms=0, absence_frames=3))
    detected = []
    worker.code_detected.connect(detected.append)

    payload = encode(2, SECRET)
    image = qimage_of(payload)
    for _ in range(10):
        worker.handle_frame(image)

    assert detected == [payload]
    assert worker.decoded == 10, "every frame decoded; only the firing was suppressed"


def test_worker_hold_blocks_firing(qtbot):
    """What the kiosk calls while a result is on screen."""
    worker = DecodeWorker(CameraConfig(cooldown_ms=0, absence_frames=1))
    detected = []
    worker.code_detected.connect(detected.append)

    worker.set_hold(5000)
    worker.handle_frame(qimage_of(encode(3, SECRET)))
    assert detected == []


# -- the panel --------------------------------------------------------------

def panel(qtbot, **overrides):
    widget = CameraPanel(dataclasses.replace(CameraConfig(), **overrides))
    qtbot.addWidget(widget)
    return widget


def test_disabled_camera_says_so_and_does_not_open_a_device(qtbot):
    widget = panel(qtbot, enabled=False)
    states = []
    widget.status_changed.connect(lambda s, m: states.append(s))
    widget.start()

    assert widget.state == "off"
    assert "off" in widget.message.text().lower()
    assert states == ["off"]


def test_missing_camera_degrades_to_a_message(qtbot):
    """Points at a name nothing can match, so this stays deterministic whether or not
    a real webcam happens to be plugged into the machine running the suite."""
    widget = panel(qtbot, device="no-such-camera-8f3a")
    widget.start()

    assert widget.state == "error"
    assert "no camera matching" in widget.message.text().lower()
    assert "Type the code" in widget.hint.text(), \
        "the fallback must tell the operator what to do instead"


def test_shutdown_is_safe_without_a_camera(qtbot):
    widget = panel(qtbot, device="no-such-camera-8f3a")
    widget.start()
    widget.shutdown()
    widget.shutdown()          # idempotent: app.py and closeEvent may both call it


# -- convergence with the kiosk ---------------------------------------------

@pytest.fixture
def kiosk(qtbot, conn, config, student):
    from trackify.ui.kiosk import KioskWindow

    cfg = dataclasses.replace(
        config,
        secrets=dataclasses.replace(config.secrets, qr_secret=SECRET),
        camera=dataclasses.replace(config.camera, device="no-such-camera-8f3a"),
    )
    window = KioskWindow(ScanService(conn, cfg), windowed=True)
    qtbot.addWidget(window)
    window.show()
    return window


def test_kiosk_survives_a_camera_that_will_not_open(qtbot, kiosk, student):
    """A camera that cannot open must not close the gate."""
    kiosk.start_camera()

    assert kiosk.camera.state == "error"
    assert kiosk.waiting.isVisible()
    assert "error" in kiosk.status_camera.text()

    # The keyboard path must still record a scan.
    kiosk.scan_input.setText(encode(student, SECRET))
    kiosk.scan_input.returnPressed.emit()
    qtbot.wait(10)
    assert kiosk.headline.text() == "IN"


def test_camera_detection_records_a_scan(qtbot, kiosk, student):
    """The convergence proof: a camera detection and a typed payload are the same
    event as far as everything downstream is concerned."""
    kiosk.camera.code_detected.emit(encode(student, SECRET))
    qtbot.wait(10)

    assert kiosk.headline.text() == "IN"
    assert kiosk.name_label.text() == "Juan Dela Cruz"
    assert kiosk.stage.property("state") == "in"


def test_preview_is_hidden_while_a_result_shows(qtbot, kiosk, student):
    """The outcome colour must fill the screen with no video panel on top of it."""
    assert kiosk.camera.isVisible()
    kiosk.camera.code_detected.emit(encode(student, SECRET))
    qtbot.wait(10)

    assert not kiosk.camera.isVisible()
    kiosk._reset_timer.stop()
    kiosk._show_waiting()
    assert kiosk.camera.isVisible()


def test_camera_error_is_flagged_in_the_status_bar(kiosk):
    kiosk.on_camera_status("error", "camera unplugged")
    assert "error" in kiosk.status_camera.text()
    assert kiosk.status_camera.property("alert") == "true", \
        "a dead camera must be visibly flagged, not silently ignored"

    kiosk.on_camera_status("ok", "Web Camera")
    assert kiosk.status_camera.property("alert") == "false"
