"""The scan station.

This screen is the product. It has to be readable across a corridor by someone who is
not looking directly at it, so the whole background shifts colour on outcome rather
than relying on text alone.

Two input sources converge on one method, _submit():

  Camera    ui/camera.CameraPanel decodes off the UI thread and emits code_detected.
  Keyboard  A hidden QLineEdit. A USB QR scanner in HID mode IS a keyboard -- it types
            the payload and presses Enter -- so typing a payload by hand is
            indistinguishable from a real scan, which is what keeps this testable with
            no hardware attached at all.

Keeping both means a dead camera degrades the station instead of closing the gate.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from qtpy.QtCore import Qt, QTimer, Slot
from qtpy.QtGui import QFontDatabase
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.service import Presentation, ScanPresentation, ScanService
from ..notify.limits import TokenBucket
from .camera import CameraPanel
from .worker import QueueStats

# Square. A 16:9 camera is centre-cropped to fill it -- see PreviewView. Square reads
# as a scanning target rather than as a video call, and it matches the shape of the
# thing being held up to it.
PREVIEW_W, PREVIEW_H = 380, 380

# Maps a domain presentation to the QSS "state" property driving the palette.
STATE_STYLE = {
    Presentation.IN: "in",
    Presentation.OUT: "out",
    Presentation.ALREADY: "already",
    Presentation.UNKNOWN_CODE: "unknown",
    Presentation.MISFIRE: "neutral",
    Presentation.NEEDS_OVERRIDE: "override",
    Presentation.NO_CLASSES: "neutral",
    Presentation.RATE_LIMITED: "neutral",
}

AVATAR_COLOURS = [
    "#4ADE80", "#58B6F5", "#F5C451", "#C08BF0", "#F58F58", "#5ED4C4",
]


def _restyle(widget: QWidget) -> None:
    """Qt does not re-evaluate property selectors on its own."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class KioskWindow(QWidget):
    def __init__(self, service: ScanService, *, windowed: bool = False) -> None:
        super().__init__()
        self.service = service
        self.setObjectName("Kiosk")
        self.setWindowTitle("TRACKIFY - Scan Station")

        # A stuck scanner or a held key cannot flood the queue.
        self._bucket = TokenBucket(
            service.config.scanning.input_rate_limit_per_sec,
            capacity=service.config.scanning.input_rate_limit_per_sec,
        )

        self._build()
        self._show_waiting()

        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._show_waiting)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

        if not windowed:
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self.showFullScreen()
        else:
            self.resize(1180, 760)

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.stage = QWidget()
        self.stage.setObjectName("Stage")
        self.stage.setProperty("state", "neutral")
        self.stage.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.stage, 1)

        stage_layout = QVBoxLayout(self.stage)
        stage_layout.setContentsMargins(64, 48, 64, 48)
        stage_layout.setSpacing(0)
        stage_layout.addStretch(1)

        # --- waiting block ---
        self.waiting = QWidget()
        self.waiting.setObjectName("Waiting")
        wait_row = QHBoxLayout(self.waiting)
        wait_row.setSpacing(56)
        wait_row.setAlignment(Qt.AlignCenter)

        # The preview is a CHILD of the waiting block, so _show_waiting/_render already
        # show and hide it with no extra code -- and the full-screen outcome colour is
        # never covered by a video panel during a result.
        self.camera = CameraPanel(self.service.config.camera)
        self.camera.set_preview_size(PREVIEW_W, PREVIEW_H)
        self.camera.code_detected.connect(self._submit)
        self.camera.status_changed.connect(self.on_camera_status)
        wait_row.addWidget(self.camera, 0, Qt.AlignVCenter)

        text_side = QWidget()
        wait_layout = QVBoxLayout(text_side)
        wait_layout.setSpacing(6)
        wait_layout.setAlignment(Qt.AlignCenter)
        wait_layout.setContentsMargins(0, 0, 0, 0)

        self.clock = QLabel("--:--")
        self.clock.setObjectName("Clock")
        self.clock.setAlignment(Qt.AlignCenter)
        self.clock_date = QLabel("")
        self.clock_date.setObjectName("ClockDate")
        self.clock_date.setAlignment(Qt.AlignCenter)
        self.waiting_title = QLabel("Scan your ID")
        self.waiting_title.setObjectName("WaitingTitle")
        self.waiting_title.setAlignment(Qt.AlignCenter)

        wait_layout.addWidget(self.clock)
        wait_layout.addWidget(self.clock_date)
        wait_layout.addSpacing(28)
        wait_layout.addWidget(self.waiting_title)
        wait_row.addWidget(text_side, 0, Qt.AlignVCenter)
        stage_layout.addWidget(self.waiting, 0, Qt.AlignCenter)

        # --- result block ---
        self.result = QWidget()
        self.result.setObjectName("Result")
        res_layout = QHBoxLayout(self.result)
        res_layout.setSpacing(48)
        res_layout.setAlignment(Qt.AlignCenter)

        self.avatar = QLabel("")
        self.avatar.setObjectName("Avatar")
        self.avatar.setAlignment(Qt.AlignCenter)
        res_layout.addWidget(self.avatar, 0, Qt.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self.name_label = QLabel("")
        self.name_label.setObjectName("StudentName")
        self.section_label = QLabel("")
        self.section_label.setObjectName("StudentSection")
        self.headline = QLabel("")
        self.headline.setObjectName("Headline")
        self.detail = QLabel("")
        self.detail.setObjectName("Detail")
        self.time_text = QLabel("")
        self.time_text.setObjectName("TimeText")

        text_col.addWidget(self.name_label)
        text_col.addWidget(self.section_label)
        text_col.addSpacing(18)
        text_col.addWidget(self.headline)
        text_col.addWidget(self.time_text)
        text_col.addSpacing(10)
        text_col.addWidget(self.detail)
        res_layout.addLayout(text_col, 1)

        self.result.hide()
        stage_layout.addWidget(self.result)
        stage_layout.addStretch(1)

        # --- hidden scanner input ---
        self.scan_input = QLineEdit()
        self.scan_input.setObjectName("ScanInput")
        self.scan_input.setFixedHeight(1)
        self.scan_input.returnPressed.connect(self._on_scan)
        stage_layout.addWidget(self.scan_input)

        # --- status bar ---
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(24, 0, 24, 0)
        bar_layout.setSpacing(14)

        self.status_session = QLabel("")
        self.status_session.setProperty("class", "status")
        self.status_provider = QLabel("")
        self.status_provider.setProperty("class", "status")
        self.status_camera = QLabel("Cam: off")
        self.status_camera.setObjectName("StatusCamera")
        self.status_unsent = QLabel("0 unsent")
        self.status_unsent.setObjectName("StatusUnsent")

        for widget, stretch in ((self.status_session, 1),):
            bar_layout.addWidget(widget, stretch)
        bar_layout.addWidget(self._sep())
        bar_layout.addWidget(self.status_camera)
        bar_layout.addWidget(self._sep())
        bar_layout.addWidget(self.status_provider)
        bar_layout.addWidget(self._sep())
        bar_layout.addWidget(self.status_unsent)
        root.addWidget(bar)

        self.status_session.setText(self.service.session_label())

    def start_camera(self) -> None:
        """Opened explicitly by app.py rather than in __init__.

        Constructing a window must not open a hardware device: it keeps the test suite
        free of the camera, and it means a camera that takes a second to warm up does
        so after the screen is already up rather than before it.
        """
        self.camera.start()

    def _sep(self) -> QLabel:
        sep = QLabel("|")
        sep.setObjectName("StatusSep")
        return sep

    # -- input --------------------------------------------------------------

    @Slot()
    def _on_scan(self) -> None:
        """The keyboard path: a HID scanner, or someone typing a payload."""
        payload = self.scan_input.text().strip()
        self.scan_input.clear()
        self._submit(payload)

    @Slot(str)
    def _submit(self, payload: str) -> None:
        """Where both input sources meet. Everything downstream is source-agnostic."""
        payload = (payload or "").strip()
        if not payload:
            return

        # The bucket exists for a stuck HID scanner or a held key. The camera cannot
        # burst past it -- ScanGate has already absorbed the repeats upstream -- so it
        # keeps its original job without misfiring on a live preview.
        if not self._bucket.try_acquire():
            self._render(ScanService.rate_limited())
            return

        self._render(self.service.handle_scan(payload))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        # Any other keystroke belongs to the scanner.
        self.scan_input.setFocus()
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        self.scan_input.setFocus()
        super().focusInEvent(event)

    # -- rendering ----------------------------------------------------------

    def _render(self, presentation: ScanPresentation) -> None:
        style_state = STATE_STYLE.get(presentation.state, "neutral")
        self.stage.setProperty("state", style_state)
        self.headline.setProperty("state", style_state)
        _restyle(self.stage)
        _restyle(self.headline)

        if presentation.student_name:
            self.avatar.setText(presentation.initials)
            colour = AVATAR_COLOURS[
                sum(ord(c) for c in presentation.student_name) % len(AVATAR_COLOURS)
            ]
            self.avatar.setStyleSheet(
                f"background: {colour}; border-radius: 90px; color: #0E1613;"
            )
            self.avatar.show()
        else:
            self.avatar.hide()

        self.name_label.setText(presentation.student_name)
        self.name_label.setVisible(bool(presentation.student_name))
        self.section_label.setText(presentation.section)
        self.section_label.setVisible(bool(presentation.section))
        self.headline.setText(presentation.headline)
        self.detail.setText(presentation.detail)
        self.time_text.setText(presentation.time_text)
        self.time_text.setVisible(bool(presentation.time_text))

        self.waiting.hide()
        self.result.show()
        self._reset_timer.start(presentation.hold_ms)

        # Suppress camera firing for exactly as long as this result is on screen, so a
        # queue of students at the lens cannot overwrite it before anyone reads it. A
        # red 'not recognised' therefore blocks longer than a green IN.
        # This is also the hook point for the Pi's GPIO buzzer: the outcome state and
        # its duration are both known right here.
        self.camera.hold(presentation.hold_ms)

    def _show_waiting(self) -> None:
        self.stage.setProperty("state", "neutral")
        _restyle(self.stage)
        self.result.hide()
        self.waiting.show()
        self.scan_input.setFocus()

    def _tick_clock(self) -> None:
        now = datetime.now()
        text = now.strftime("%I:%M")
        self.clock.setText(text[1:] if text.startswith("0") else text)
        self.clock_date.setText(now.strftime("%A, %d %B %Y").upper())

    # -- worker signals -----------------------------------------------------

    @Slot(object)
    def on_stats(self, stats: QueueStats) -> None:
        self.status_provider.setText(f"SMS: {stats.provider}")
        self.status_unsent.setText(
            f"{stats.unsent} unsent" if stats.unsent != 1 else "1 unsent"
        )
        self.status_unsent.setProperty("alert", "true" if stats.unsent else "false")
        _restyle(self.status_unsent)

    @Slot(str, str)
    def on_camera_status(self, state: str, message: str) -> None:
        """A dead camera is flagged in the status bar so it is noticed the same
        morning, rather than inferred afterwards from missing attendance data."""
        self.status_camera.setText(f"Cam: {state}")
        self.status_camera.setToolTip(message)
        self.status_camera.setProperty("alert", "true" if state == "error" else "false")
        _restyle(self.status_camera)

    def closeEvent(self, event) -> None:
        self.camera.shutdown()
        super().closeEvent(event)

    @Slot(str)
    def on_alarm(self, message: str) -> None:
        self.status_provider.setText("SMS: HALTED")
        self.status_unsent.setProperty("alert", "true")
        _restyle(self.status_unsent)
        print(f"[ALARM] {message}")
