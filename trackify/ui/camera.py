"""Webcam capture, live preview, and off-thread decoding.

The scan station was built for a USB HID scanner, which IS a keyboard: it types the
payload and presses Enter. A webcam differs in three ways, and each one shapes this
module:

  It sees the same code ~10 times a second   -> ScanGate, on the decode thread
  It gives the student no feedback about aim -> the preview, with a reticle
  It costs CPU on every single frame         -> DecodeWorker, on its own QThread

Capture and preview use QtMultimedia, which ships with PySide6, so nothing here needs a
third-party capture library. Decoding uses zxing-cpp via trackify.scan.decoder.

Frame path, and the one rule that matters:

    QCamera -> QGraphicsVideoItem                (painted by Qt, no Python involved)
       |
       +- item.videoSink().videoFrameChanged     [UI thread]
             |  throttle to decode_fps; DROP the frame if the worker is still busy
             v  queued signal
          DecodeWorker on its own QThread
             |  decode_qr(QImage) -> ScanGate
             v  queued signal, only when the gate actually fires
          KioskWindow._submit(payload)           [UI thread]

Frames are dropped, never queued. Emitting every frame regardless would build an
unbounded backlog and the kiosk would end up reacting to what the camera saw seconds
ago -- indistinguishable, to anyone standing at the gate, from a hung application.
"""

from __future__ import annotations

import contextlib
import time

from qtpy.QtCore import QObject, QRectF, QSizeF, Qt, QThread, Signal, Slot
from qtpy.QtGui import QColor, QImage, QPainter, QPen
from qtpy.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from ..core.config import CameraConfig
from ..scan import decoder
from ..scan.gate import ScanGate

try:
    from qtpy.QtMultimedia import (
        QCamera,
        QMediaCaptureSession,
        QMediaDevices,
        QVideoFrameFormat,
    )
    from qtpy.QtMultimediaWidgets import QGraphicsVideoItem
    MULTIMEDIA = True
except ImportError:                                   # degrade, never crash
    MULTIMEDIA = False


def list_cameras():
    """Available video inputs, or an empty list if QtMultimedia is missing."""
    if not MULTIMEDIA:
        return []
    return list(QMediaDevices.videoInputs())


def pick_camera(devices, wanted: str = ""):
    """Choose a camera by case-insensitive substring of its name.

    An index-based API would be simpler and worse: this laptop reports two cameras and
    the order is not stable across reboots or across platforms, so "camera 1" is not a
    durable way to name the one bolted to the gate. A name is.
    """
    if not devices:
        return None
    if not wanted:
        return devices[0]
    needle = wanted.strip().lower()
    for device in devices:
        if needle in device.description().lower():
            return device
    return None


def is_compressed(fmt) -> bool:
    """True for MJPEG streams.

    Worth avoiding: every MJPEG frame has to be JPEG-decoded before it can be scanned,
    which is work done on our behalf on all thirty frames a second. The same camera
    almost always offers an uncompressed NV12 or YUYV format at the same resolution.
    """
    name = str(fmt.pixelFormat()).rsplit(".", 1)[-1]
    return name == "Format_Jpeg"


def choose_format(device, width: int, height: int, min_fps: float = 0):
    """The supported format closest to the requested resolution.

    Resolution first, then fast enough to decode at, then uncompressed over MJPEG, then
    frame rate. Resolution leads because it decides whether a small printed code resolves
    at all; the rest is efficiency.

    `min_fps` is the frame rate the decoder is configured to want. It exists because
    preferring uncompressed unconditionally was wrong on the gate camera: the SunplusIT
    unit on the Pi offers 1280x720 as MJPEG at 30 fps and as YUYV at *5*, and the old
    ranking took the 5 fps mode. That is half the configured decode rate, so the gate
    got fewer chances to read each card -- and it made the preview a student aims at
    visibly choppy, which costs framing time, the thing docs/hardware.md section 5
    identifies as the real throughput bottleneck.

    Saving the JPEG decode is not worth six times the frame rate. Frames are dropped
    rather than queued and only decode_fps of them are ever looked at, so the cost is
    bounded at a few ms on a handful of frames a second -- which a Pi 5 has in
    abundance. When both candidates clear min_fps this still prefers uncompressed,
    exactly as before.
    """
    formats = list(device.videoFormats())
    if not formats:
        return None

    def rank(fmt):
        size = fmt.resolution()
        return (
            abs(size.width() - width) + abs(size.height() - height),
            fmt.maxFrameRate() < min_fps,
            is_compressed(fmt),
            -fmt.maxFrameRate(),
        )

    return min(formats, key=rank)


class DecodeWorker(QObject):
    """Lives on a QThread. Decodes frames and applies sensor-level de-duplication.

    The ScanGate lives here rather than in the widget so suppressed frames -- the vast
    majority of them -- never cross a thread boundary at all.
    """

    code_detected = Signal(str)
    finished = Signal()

    def __init__(self, config: CameraConfig) -> None:
        super().__init__()
        self._gate = ScanGate(
            absence_frames=config.absence_frames,
            cooldown_ms=config.cooldown_ms,
        )
        self.decoded = 0
        self.last_ms = 0.0

    @Slot(QImage)
    def handle_frame(self, image: QImage) -> None:
        started = time.perf_counter()
        payload = decoder.decode_qr(image)
        self.last_ms = (time.perf_counter() - started) * 1000
        self.decoded += 1

        fired = self._gate.offer(payload)
        if fired:
            self.code_detected.emit(fired)
        self.finished.emit()          # tells the UI thread it may send another frame

    @Slot(int)
    def set_hold(self, ms: int) -> None:
        """Block firing while the kiosk is showing a result.

        Reached by a queued signal, so the gate is only ever touched from its own
        thread -- no lock, no shared mutable state.
        """
        self._gate.hold(ms)


class PreviewView(QGraphicsView):
    """The live picture, with four corner brackets drawn over it.

    Video goes through a QGraphicsVideoItem rather than the more obvious QVideoWidget,
    for one reason: a QVideoWidget is a native surface that the graphics stack
    composites ABOVE ordinary Qt content, so anything drawn on top of one -- a child
    widget, a sibling in a stacked layout -- simply disappears. This was verified on
    screen, not assumed. Scene content has no such problem, and drawForeground paints
    reliably above the video.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraView")
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.SmoothPixmapTransform)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.item = QGraphicsVideoItem()
        # ByExpanding: fill the panel and crop the overflow, rather than letterboxing a
        # 16:9 camera into a square panel and framing the picture in black bars.
        #
        # Cropping the PREVIEW does not crop the SCAN. Frames reach the decoder straight
        # from the video sink at full sensor resolution, so a code near the left or right
        # edge still reads even though the preview does not show it. The mismatch only
        # ever errs towards reading more than it displays, never less.
        self.item.setAspectRatioMode(Qt.KeepAspectRatioByExpanding)
        self._scene.addItem(self.item)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        size = self.viewport().size()
        self.item.setSize(QSizeF(size.width(), size.height()))
        self._scene.setSceneRect(QRectF(0, 0, size.width(), size.height()))
        # With ByExpanding the picture overflows the item on one axis. Centring the
        # scene rect keeps the crop even on both sides instead of chopping one edge.
        self.centerOn(self._scene.sceneRect().center())

    def drawForeground(self, painter: QPainter, rect) -> None:
        """Corner brackets. Scene and viewport coordinates coincide here -- the item is
        sized to the viewport and the view is never scaled or scrolled."""
        bounds = self.sceneRect()
        w, h = bounds.width(), bounds.height()
        if w <= 0 or h <= 0:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#4ADE80"), 4)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)

        inset = min(w, h) * 0.14
        arm = min(w, h) * 0.13
        left, right = inset, w - inset
        top, bottom = inset, h - inset

        for x, dx in ((left, arm), (right, -arm)):
            for y, dy in ((top, arm), (bottom, -arm)):
                painter.drawLine(int(x), int(y), int(x + dx), int(y))
                painter.drawLine(int(x), int(y), int(x), int(y + dy))
        painter.restore()


class CameraPanel(QWidget):
    """The live preview, and everything behind it.

    Owns its camera, its decode thread and its own shutdown, so the caller treats it as
    one device rather than assembling three objects in the right order.
    """

    code_detected = Signal(str)
    status_changed = Signal(str, str)      # (state: ok|off|error, human message)

    # Both of these cross into the decode thread. They are signals rather than direct
    # calls precisely so Qt queues them: calling worker.handle_frame() directly would
    # run the decode on the UI thread, which is the entire thing this design avoids.
    _frame_ready = Signal(QImage)
    _hold_requested = Signal(int)

    def __init__(self, config: CameraConfig, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CameraPanel")
        self.config = config

        self._camera = None
        self._session = None
        self._view = None
        self._thread = None
        self._worker = None
        self._busy = False
        self._next_decode = 0.0
        self._interval = 1.0 / max(config.decode_fps, 1)
        self._state = "off"

        self._build()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.stage = QWidget()
        self.stage.setObjectName("CameraStage")
        # One at a time: either the picture, or the message explaining its absence.
        self._stack = QStackedLayout(self.stage)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.message = QLabel("")
        self.message.setObjectName("CameraStatus")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self._stack.addWidget(self.message)

        layout.addWidget(self.stage, 1)

        self.hint = QLabel("Hold your ID inside the frame")
        self.hint.setObjectName("CameraHint")
        self.hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.hint)

    def set_preview_size(self, width: int, height: int) -> None:
        """Fix the picture area. The panel sizes itself around it, so the hint label
        below always gets the room its font actually needs."""
        self.stage.setFixedSize(width, height)
        self.adjustSize()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bring up the camera, or explain on screen why there isn't one.

        Every failure below leaves a working kiosk and a working keyboard path. A dead
        camera must degrade the scan station, never take it down.
        """
        if not self.config.enabled:
            return self._fail("off", "Camera off", "camera disabled in config")
        if not MULTIMEDIA:
            return self._fail("error", "QtMultimedia unavailable",
                              "QtMultimedia is not installed")
        if not decoder.available():
            return self._fail("error", "QR decoder\nnot installed",
                              "zxing-cpp is not installed")

        device = pick_camera(list_cameras(), self.config.device)
        if device is None:
            wanted = self.config.device
            return self._fail(
                "error",
                f"No camera matching\n{wanted!r}" if wanted else "No camera\ndetected",
                "no camera detected",
            )

        self._view = PreviewView()
        self._stack.insertWidget(0, self._view)
        self._stack.setCurrentWidget(self._view)

        self._camera = QCamera(device)
        fmt = choose_format(device, self.config.width, self.config.height,
                            self.config.decode_fps)
        if fmt is not None:
            self._camera.setCameraFormat(fmt)
        self._camera.errorOccurred.connect(self._on_camera_error)

        self._session = QMediaCaptureSession()
        self._session.setCamera(self._camera)
        self._session.setVideoOutput(self._view.item)

        self._start_worker()
        self._view.item.videoSink().videoFrameChanged.connect(self._on_frame)
        self._camera.start()

        self.message.setText("")
        self._set_state("ok", device.description())

    def _start_worker(self) -> None:
        self._worker = DecodeWorker(self.config)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.code_detected.connect(self.code_detected)
        self._worker.finished.connect(self._on_worker_free)
        self._frame_ready.connect(self._worker.handle_frame, Qt.QueuedConnection)
        self._hold_requested.connect(self._worker.set_hold, Qt.QueuedConnection)
        self._thread.start()

    def shutdown(self) -> None:
        """Stop the camera and join the decode thread. Safe to call twice."""
        if self._camera is not None:
            # Qt raises RuntimeError if the C++ object is already gone -- which is the
            # normal case on a second shutdown(), and this is documented as safe twice.
            with contextlib.suppress(RuntimeError):
                self._camera.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None

    # -- frames -------------------------------------------------------------

    @Slot(object)
    def _on_frame(self, frame) -> None:
        """Runs on the UI thread. Must stay cheap: throttle, convert, hand off.

        The camera is NOT stopped when the preview is hidden during a result, so this
        keeps running -- deliberately. Reopening a DirectShow or V4L2 device takes a
        second or more, and leaving it running means a student presenting a card at the
        tail of a hold is read the instant the screen returns.
        """
        if self._busy or self._worker is None:
            return
        now = time.monotonic()
        if now < self._next_decode:
            return
        if not frame.isValid():
            return

        image = frame.toImage()
        if image.isNull():
            return
        # Grayscale is what the decoder wants, and converting produces a deep copy --
        # which is what makes the result safe to hand to another thread.
        gray = image.convertToFormat(QImage.Format_Grayscale8)

        self._next_decode = now + self._interval
        self._busy = True
        self._frame_ready.emit(gray)

    @Slot()
    def _on_worker_free(self) -> None:
        self._busy = False

    # -- outward state ------------------------------------------------------

    @Slot(int)
    def hold(self, ms: int) -> None:
        """Ask the gate to suppress firing while a result is on screen."""
        if self._worker is not None:
            self._hold_requested.emit(ms)

    def _on_camera_error(self, error=None, message: str = "") -> None:
        self._fail("error", "Camera\ndisconnected", message or "camera error")

    def _fail(self, state: str, on_screen: str, status: str) -> None:
        self.message.setText(on_screen)
        if self._view is not None:
            self._view.hide()
        self.hint.setText("Type the code and press Enter")
        self._stack.setCurrentWidget(self.message)
        self._set_state(state, status)

    def _set_state(self, state: str, message: str) -> None:
        self._state = state
        self.status_changed.emit(state, message)

    @property
    def state(self) -> str:
        return self._state
