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

from qtpy.QtCore import QSize, Qt, QTimer, Slot
from qtpy.QtGui import QFontDatabase, QPainter, QPainterPath, QPixmap
from qtpy.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.config import PROJECT_ROOT
from ..core import custody
from ..core.security import AttemptGate
from ..core import screening as screening_taxonomy
from ..core.screening import Outcome as ScreeningOutcome
from ..core.service import Presentation, ScanPresentation, ScanService
from ..notify.limits import TokenBucket
from .camera import CameraPanel
from . import icons
from .records import PasswordDialog, RecordsPage
from .screening import CustodyDialog, IncidentDialog
from .worker import QueueStats

# Square. A 16:9 camera is centre-cropped to fill it -- see PreviewView. Square reads
# as a scanning target rather than as a video call, and it matches the shape of the
# thing being held up to it.
PREVIEW_W, PREVIEW_H = 380, 380

# Matches min/max-width on QLabel#Avatar in style.qss. A photo scaled to anything
# else either overflows the circle or leaves a ring of background inside it.
AVATAR_PX = 180

# The small avatar on the inspection page. Matches min/max-width on
# QLabel#InspectAvatar in style.qss.
AVATAR_STRIP_PX = 52

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


def _load_photo(path: str | None) -> QPixmap | None:
    """Load a student photo, cropped square and scaled to the avatar circle.

    Returns None for anything unusable -- no path, missing file, or a file Qt cannot
    decode -- so the caller can fall back to initials. Photos come from a school
    roster, which means some of them WILL be broken.
    """
    if not path:
        return None
    file = Path(path)
    if not file.is_absolute():
        file = PROJECT_ROOT / file
    if not file.is_file():
        return None

    pixmap = QPixmap(str(file))
    if pixmap.isNull():
        return None

    # Centre-crop to a square first: scaling a portrait straight to 180x180 would
    # squash the face, and a face is the whole point of showing it.
    side = min(pixmap.width(), pixmap.height())
    pixmap = pixmap.copy(
        (pixmap.width() - side) // 2, (pixmap.height() - side) // 2, side, side
    )
    pixmap = pixmap.scaled(
        AVATAR_PX, AVATAR_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation
    )
    return _circular(pixmap)


def _circular(pixmap: QPixmap) -> QPixmap:
    """Clip a square pixmap to a circle.

    border-radius in QSS rounds a widget's BACKGROUND, not the pixmap drawn on top of
    it -- so the initials avatar comes out round and a photo in the same label comes
    out square. The mask has to be painted.
    """
    rounded = QPixmap(pixmap.size())
    rounded.fill(Qt.transparent)

    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, pixmap.width(), pixmap.height())
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return rounded


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

        # Latched by on_alarm so the next stats tick cannot quietly clear it. Only a
        # restart lifts it, which is right for a spend cap: it means someone looked.
        self._sms_halted = False

        self._build()
        self._show_waiting()

        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._show_waiting)

        # Date whose end-of-day job has already run in this process. Set before the
        # first _tick_clock() call, which happens two lines below.
        self._closed_for: str | None = None
        self._dismissal: tuple[str, object] = ("", None)

        # The scan currently awaiting a screening outcome, or None. Never holds a
        # student id: a screening binds to a scan (flow.md Rule 2).
        self._awaiting_scan: int | None = None
        self._awaiting_student: int | None = None
        self._result_state = "neutral"
        self._avatar_initials = ""
        self._records_gate = AttemptGate()

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

        # Only on the waiting screen, never over a result: docs/flow.md 8 says the
        # station screen shows the current student and nothing else. Staff use this
        # after the last student has come through.
        wait_layout.addSpacing(24)
        self.btn_records = self._screening_button(
            "Attendance records", "ScreeningMinor", self._open_records,
        )
        wait_layout.addWidget(self.btn_records, 0, Qt.AlignCenter)
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
        self.adviser_label = QLabel("")
        self.adviser_label.setObjectName("StudentAdviser")
        self.headline = QLabel("")
        self.headline.setObjectName("Headline")
        self.detail = QLabel("")
        self.detail.setObjectName("Detail")
        self.time_text = QLabel("")
        self.time_text.setObjectName("TimeText")

        text_col.addWidget(self.name_label)
        text_col.addWidget(self.section_label)
        text_col.addWidget(self.adviser_label)
        text_col.addSpacing(18)
        text_col.addWidget(self.headline)
        text_col.addWidget(self.time_text)
        text_col.addSpacing(10)
        text_col.addWidget(self.detail)

        # --- screening prompt (docs/prohibited-items.md) ---
        # Only the "did it beep" question lives here. The overwhelming majority of
        # students have nothing in their bag, and that path has to stay one click
        # with no page change. Classifying a find gets its own page instead.
        self.screening_row = QWidget()
        self.screening_row.setObjectName("ScreeningRow")
        scr = QVBoxLayout(self.screening_row)
        scr.setContentsMargins(0, 18, 0, 0)
        scr.setSpacing(8)

        self.screening_prompt = QLabel("")
        self.screening_prompt.setObjectName("ScreeningPrompt")
        self.screening_prompt.setWordWrap(True)
        scr.addWidget(self.screening_prompt)
        scr.addWidget(self._build_stage_one())

        text_col.addWidget(self.screening_row)
        self.screening_row.hide()

        res_layout.addLayout(text_col, 1)

        self.result.hide()
        stage_layout.addWidget(self.result)

        # --- inspection page ---
        # A third sibling of waiting and result, shown and hidden the same way. Not a
        # QStackedWidget at this level: the camera panel is a child of `waiting` and
        # depends on that show/hide to stop drawing over a result.
        self.inspection = self._build_inspection()
        self.inspection.hide()
        stage_layout.addWidget(self.inspection)

        # --- records page ---
        # A page rather than a separate window, on purpose. A separate window takes
        # focus off scan_input, so the gate would silently stop accepting scans while
        # records were open with nothing on screen to say so.
        self.records = RecordsPage(
            self.service.conn,
            school_name=self.service.config.school.name,
            config=self.service.config,
        )
        self.records.closed.connect(self._close_records)
        self.records.hide()
        stage_layout.addWidget(self.records)

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
        self.status_provider.setObjectName("StatusProvider")
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

    def _screening_button(self, text: str, name: str, slot, *,
                          icon: str | None = None) -> QPushButton:
        """A screening button that can never steal the scanner's keystrokes.

        NoFocus is not cosmetic. A focused QPushButton consumes Enter and Space, and
        a HID scanner ends every payload with Enter -- one click on a focusable button
        and scanning silently stops working until someone clicks elsewhere.
        """
        button = QPushButton(text)
        button.setObjectName(name)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        if icon is not None:
            tint = "#0E1613" if name == "ScreeningPrimary" else "#9AACA6"
            button.setIcon(icons.icon(icon, 22, tint))
            button.setIconSize(QSize(22, 22))
        button.clicked.connect(slot)
        return button

    def _build_stage_one(self) -> QWidget:
        """Did the detector beep?"""
        stage = QWidget()
        row = QHBoxLayout(stage)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self.btn_no_metal = self._screening_button(
            "NO METAL", "ScreeningPrimary",
            lambda: self._resolve_screening(ScreeningOutcome.CLEAR),
            icon="no_metal",
        )
        self.btn_metal = self._screening_button(
            "METAL DETECTED", "ScreeningPrimary", self._show_inspection,
            icon="metal_detected",
        )
        # Set apart on purpose: the honest way out when the detector is flat or a
        # student cannot be screened at all. It is an escape hatch, not a third
        # normal choice, and it must never look like one.
        self.btn_not_screened = self._screening_button(
            "Not screened", "ScreeningMinor",
            lambda: self._resolve_screening(ScreeningOutcome.NOT_SCREENED),
            icon="not_screened",
        )

        row.addWidget(self.btn_no_metal)
        row.addWidget(self.btn_metal)
        row.addSpacing(24)
        row.addWidget(self.btn_not_screened)
        row.addStretch(1)
        return stage

    def _card(self, code: str, label: str, covers: str, icon_name: str,
              danger: bool, slot) -> QToolButton:
        """One item tile: icon above label.

        QToolButton rather than QPushButton because ToolButtonTextUnderIcon is the
        only way Qt stacks an icon over its text without hand-laying-out a widget.
        """
        button = QToolButton()
        button.setObjectName("InspectCardDanger" if danger else "InspectCard")
        button.setText(label)
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setIcon(icons.icon(icon_name, 44, "#F0918F" if danger else "#E6EDEA"))
        button.setIconSize(QSize(44, 44))
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(covers)
        button.clicked.connect(slot)
        return button

    def _build_inspection(self) -> QWidget:
        """The bag inspection page.

        The `tool` category is deliberately absent from the prohibited row.
        prohibited-items.md 6 says a tool with an ordinary classroom use routes to
        custody, so "School tool" IS the tool category -- showing both would only make
        the operator choose between two buttons that mean the same thing.
        """
        page = QWidget()
        page.setObjectName("InspectionPage")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(8, 0, 8, 0)
        outer.setSpacing(0)

        # --- student strip -------------------------------------------------
        # The whole reason this used to live inside the result block: the operator
        # must see WHO they are recording against. Putting a knife on the wrong
        # student's record is the worst mistake available on this screen, and a
        # separate page is exactly how that happens.
        strip = QHBoxLayout()
        strip.setSpacing(14)
        self.inspect_avatar = QLabel("")
        self.inspect_avatar.setObjectName("InspectAvatar")
        self.inspect_avatar.setAlignment(Qt.AlignCenter)
        self.inspect_student = QLabel("")
        self.inspect_student.setObjectName("InspectStudent")
        self.inspect_section = QLabel("")
        self.inspect_section.setObjectName("InspectSection")

        strip.addWidget(self.inspect_avatar)
        strip.addWidget(self.inspect_student)
        strip.addWidget(self.inspect_section)
        strip.addStretch(1)

        self.btn_back = self._screening_button(
            "Back", "ScreeningMinor", self._back_to_result, icon="back",
        )
        strip.addWidget(self.btn_back)
        outer.addLayout(strip)
        outer.addSpacing(22)

        heading = QLabel("What was found in the bag?")
        heading.setObjectName("InspectHeading")
        outer.addWidget(heading)

        # The decision rule belongs HERE, not in the incident dialog: the operator
        # reaches that dialog only after choosing a category, one step too late for
        # the rule to help them choose it.
        rule = QLabel(screening_taxonomy.DECISION_RULE)
        rule.setObjectName("InspectRule")
        rule.setWordWrap(True)
        outer.addWidget(rule)
        outer.addSpacing(18)

        self.inspect_prompt = QLabel("")
        self.inspect_prompt.setObjectName("ScreeningPrompt")
        self.inspect_prompt.setWordWrap(True)
        outer.addWidget(self.inspect_prompt)

        # --- not a concern -------------------------------------------------
        outer.addWidget(self._group_label("NOT A CONCERN"))
        safe = QHBoxLayout()
        safe.setSpacing(14)
        self.btn_common = self._card(
            "common", "Common items", "phone, laptop, coins, tumbler",
            "common_items", False,
            lambda: self._resolve_screening(ScreeningOutcome.COMMON_ITEMS),
        )
        self.btn_school_tool = self._card(
            "tool", "School tool", "collect, tag, release to the adviser",
            "school_tool", False,
            lambda: self._resolve_screening(ScreeningOutcome.SCHOOL_HAZARD),
        )
        safe.addWidget(self.btn_common)
        safe.addWidget(self.btn_school_tool)
        safe.addStretch(1)
        outer.addLayout(safe)
        outer.addSpacing(18)

        # --- prohibited ----------------------------------------------------
        outer.addWidget(self._group_label("PROHIBITED"))
        cats = QHBoxLayout()
        cats.setSpacing(14)
        self.category_buttons = {}
        for cat in screening_taxonomy.CATEGORIES:
            if cat.code == "tool":              # School tool, above
                continue
            button = self._card(
                cat.code, cat.label.split()[0], f"{cat.label} - {cat.covers}",
                cat.code, True,
                lambda _=False, code=cat.code: self._prohibited(code),
            )
            self.category_buttons[cat.code] = button
            cats.addWidget(button)
        cats.addStretch(1)
        outer.addLayout(cats)
        outer.addSpacing(22)

        self.btn_unfinished = self._screening_button(
            "Inspection not finished", "ScreeningMinor",
            lambda: self._resolve_screening(ScreeningOutcome.PENDING_VERIFICATION),
            icon="unfinished",
        )
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_unfinished)
        bottom.addStretch(1)
        outer.addLayout(bottom)
        return page

    def _group_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("InspectGroup")
        return label

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

        # The gate always wins. A student at the lens outranks a register on screen,
        # and leaving attendance records open at a gate is exactly what flow.md 8
        # forbids. Nothing is lost by closing: corrections save one at a time, so
        # there is never a half-finished form to discard.
        if self.records_open:
            self._close_records()

        # The gate blocks until the current student has been answered for. A refused
        # scan writes NOTHING -- no scan_events row, no attendance, no notification --
        # so the student simply scans again once the screen is free, and the debounce
        # window does not catch them either because there is nothing to debounce
        # against. The cost is throughput, never a lost or a wrong record.
        if self._awaiting_scan is not None:
            self._set_prompt(
                f"Finish the screening for {self.name_label.text()} first - "
                "this scan was NOT recorded.",
                alert=True,
            )
            return

        self._render(self.service.handle_scan(payload))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        # Every keystroke belongs to the scanner. The screening buttons are all
        # NoFocus, so none of them can intercept the Enter that ends a payload.
        self.scan_input.setFocus()
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:
        self.scan_input.setFocus()
        super().focusInEvent(event)

    # -- rendering ----------------------------------------------------------

    def _paint_avatar(self, presentation: ScanPresentation) -> None:
        """The student photo, or initials when there is no usable one.

        The photo is the guard's second identity control -- an HMAC proves the CODE is
        genuine, only a face proves the PERSON holding it is. Every failure here falls
        back to initials rather than showing an empty circle: a missing file, an
        unreadable one, or a roster that simply has no photos yet must all still leave
        a screen the guard can act on.
        """
        pixmap = _load_photo(presentation.photo_path)
        if pixmap is not None:
            self.avatar.setText("")
            self.avatar.setPixmap(pixmap)
            # Transparent behind it: the pixmap is already circular, and a square
            # background would show as corners around it.
            self.avatar.setStyleSheet("background: transparent;")
            return

        self.avatar.setPixmap(QPixmap())        # drop any previous student's photo
        self.avatar.setText(presentation.initials)
        colour = AVATAR_COLOURS[
            sum(ord(c) for c in presentation.student_name) % len(AVATAR_COLOURS)
        ]
        self.avatar.setStyleSheet(
            f"background: {colour}; border-radius: 90px; color: #0E1613;"
        )

    def _render(self, presentation: ScanPresentation) -> None:
        style_state = STATE_STYLE.get(presentation.state, "neutral")
        # Remembered so Back from the inspection page restores this ground rather
        # than guessing at it.
        self._result_state = style_state
        self.stage.setProperty("state", style_state)
        self.headline.setProperty("state", style_state)
        _restyle(self.stage)
        _restyle(self.headline)

        self._avatar_initials = presentation.initials
        if presentation.student_name:
            self._paint_avatar(presentation)
            self.avatar.show()
        else:
            self.avatar.hide()

        self.name_label.setText(presentation.student_name)
        self.name_label.setVisible(bool(presentation.student_name))
        self.section_label.setText(presentation.section)
        self.section_label.setVisible(bool(presentation.section))
        self.adviser_label.setText(presentation.adviser)
        self.adviser_label.setVisible(bool(presentation.adviser))
        self.headline.setText(presentation.headline)
        self.detail.setText(presentation.detail)
        self.time_text.setText(presentation.time_text)
        self.time_text.setVisible(bool(presentation.time_text))

        self._offer_screening(presentation)

        self.waiting.hide()
        self.result.show()

        hold = self._hold_for(presentation)
        if hold is None:
            self._reset_timer.stop()
        else:
            self._reset_timer.start(hold)

        # Suppress camera firing for exactly as long as this result is on screen, so a
        # queue of students at the lens cannot overwrite it before anyone reads it. A
        # red 'not recognised' therefore blocks longer than a green IN.
        # This is also the hook point for the Pi's GPIO buzzer: the outcome state and
        # its duration are both known right here.
        self.camera.hold(presentation.hold_ms)

    # -- attendance records ---------------------------------------------------

    def _open_records(self) -> None:
        """Password on EVERY open, not once per session.

        The kiosk stands at a gate. "Unlocked earlier today" is not a reason to show
        one student's history to whoever is standing in front of it now.
        """
        dialog = PasswordDialog(self.service.conn, self._records_gate, self)
        if dialog.exec() != QDialog.Accepted:
            self.scan_input.setFocus()
            return

        self.records.refresh()
        self.waiting.hide()
        self.result.hide()
        self.records.show()
        self.stage.setProperty("state", "records")
        _restyle(self.stage)
        self.scan_input.setFocus()

    def _close_records(self) -> None:
        self.records.hide()
        self._show_waiting()

    @property
    def records_open(self) -> bool:
        return self.records.isVisible()

    # -- screening (docs/prohibited-items.md) --------------------------------

    def _hold_for(self, presentation: ScanPresentation) -> int | None:
        """How long this result stays up, or None to stay until someone answers.

        A pending screening NEVER times out. The guard may still be holding the bag
        open when the timer would have fired, and a screen that closes itself records
        an outcome nobody chose -- which is how a study about safety ends up with
        fabricated data in it. Nothing is written until a person clicks.
        """
        if self._awaiting_scan is not None:
            return None
        return presentation.hold_ms

    def _offer_screening(self, presentation: ScanPresentation) -> None:
        """Show the outcome keys, if this scan is one that can be screened.

        Only an arrival is screened -- a student leaving is not swept on the way out.
        Attendance is already committed by the time this runs, which is the point:
        flow.md 3 step 6 says the screening outcome must never affect whether
        attendance was recorded, so nothing here can undo it.
        """
        config = self.service.config.screening
        self._awaiting_scan = None
        self._awaiting_student = None
        self.screening_row.hide()

        if not config.enabled or presentation.state is not Presentation.IN:
            return
        if presentation.scan_id is None:
            return

        self._awaiting_scan = presentation.scan_id
        self._awaiting_student = presentation.student_id
        self._set_prompt(f"Tray: {config.declared_items_hint}")
        self.screening_row.show()

    def _back_to_result(self) -> None:
        """Leave the inspection page. Nothing was recorded there, so nothing to undo."""
        self.inspection.hide()
        self.result.show()
        self.stage.setProperty("state", self._result_state)
        _restyle(self.stage)
        self._set_prompt(f"Tray: {self.service.config.screening.declared_items_hint}")

    def _show_inspection(self) -> None:
        """Open the bag inspection page, carrying the student across with it."""
        # Scale the result screen's avatar down rather than reusing it at full size:
        # a 180px pixmap in a 52px label is CLIPPED, not fitted, so the photo would
        # show as a square crop of the top-left corner.
        photo = self.avatar.pixmap()
        if photo is not None and not photo.isNull():
            self.inspect_avatar.setText("")
            self.inspect_avatar.setPixmap(photo.scaled(
                AVATAR_STRIP_PX, AVATAR_STRIP_PX,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ))
            self.inspect_avatar.setStyleSheet("background: transparent;")
        else:
            self.inspect_avatar.setPixmap(QPixmap())
            self.inspect_avatar.setText(self._avatar_initials)
            colour = AVATAR_COLOURS[
                sum(ord(c) for c in self.name_label.text() or " ")
                % len(AVATAR_COLOURS)
            ]
            self.inspect_avatar.setStyleSheet(
                f"background: {colour}; border-radius: {AVATAR_STRIP_PX // 2}px; "
                "color: #0E1613;"
            )

        self.inspect_student.setText(self.name_label.text())
        self.inspect_section.setText(self.section_label.text())

        self.result.hide()
        self.inspection.show()
        # A ground that says "different mode", not one that implies an outcome: no
        # decision has been made yet.
        self.stage.setProperty("state", "inspect")
        _restyle(self.stage)
        self._set_prompt("")

    def _set_prompt(self, text: str, *, alert: bool = False) -> None:
        """Both prompts, always.

        The refusal message used to go only to the label on the RESULT page, which is
        hidden while the inspection page is up -- so a scan refused mid-inspection
        showed nothing at all and the operator would think the scanner had died.
        """
        for label in (self.screening_prompt, self.inspect_prompt):
            label.setText(text)
            label.setProperty("alert", "true" if alert else "false")
            _restyle(label)

    def _prohibited(self, category: str) -> None:
        """A prohibited-item button. The category seeds the dialog, it does not lock
        it -- a guard often looks properly only once the bag is open."""
        self._resolve_screening(ScreeningOutcome.PROHIBITED, category=category)

    def _resolve_screening(self, outcome: ScreeningOutcome, *, category=None) -> None:
        """Record the guard's answer and move on.

        The screening row is written FIRST and unconditionally. If the guard then
        cancels the detail dialog, the outcome stays recorded as
        pending_verification -- an unfinished inspection, which is true -- rather than
        vanishing as though the student had never been screened.
        """
        scan_id, self._awaiting_scan = self._awaiting_scan, None
        if scan_id is None:
            return

        student_id = self._awaiting_student
        self._awaiting_student = None

        try:
            screening_id = self.service.record_screening(
                scan_id, outcome,
                metal_detected=outcome is not ScreeningOutcome.CLEAR,
            )
            if outcome is ScreeningOutcome.PROHIBITED:
                self._collect_incident(scan_id, screening_id, student_id, category)
            elif outcome is ScreeningOutcome.SCHOOL_HAZARD:
                self._collect_custody(scan_id, screening_id, student_id)
        except Exception as exc:      # noqa: BLE001 - the gate stays open regardless
            self.status_session.setText(f"Screening not recorded: {exc}")
            return
        self._show_waiting()

    def _collect_incident(
        self, scan_id: int, screening_id: int, student_id, category=None
    ) -> None:
        dialog = IncidentDialog(self.name_label.text(), category=category, parent=self)
        if dialog.exec() != QDialog.Accepted:
            # Cancelled: the inspection is genuinely unfinished, so say so.
            self.service.record_screening(
                scan_id, ScreeningOutcome.PENDING_VERIFICATION, metal_detected=True
            )
            return
        self.service.record_incident(screening_id, student_id, **dialog.values())

    def _collect_custody(self, scan_id: int, screening_id: int, student_id) -> None:
        dialog = CustodyDialog(self.name_label.text(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            self.service.record_screening(
                scan_id, ScreeningOutcome.PENDING_VERIFICATION, metal_detected=True
            )
            return
        custody.collect(
            self.service.conn, student_id,
            screening_event_id=screening_id, **dialog.values()
        )

    def _show_waiting(self) -> None:
        self.stage.setProperty("state", "neutral")
        _restyle(self.stage)
        self.inspection.hide()
        self.records.hide()
        self.result.hide()
        self.waiting.show()
        self.scan_input.setFocus()

    def _tick_clock(self) -> None:
        now = datetime.now()
        text = now.strftime("%I:%M")
        self.clock.setText(text[1:] if text.startswith("0") else text)
        self.clock_date.setText(now.strftime("%A, %d %B %Y").upper())
        self._maybe_close_day(now)

    # -- end of day ---------------------------------------------------------

    def _maybe_close_day(self, now: datetime) -> None:
        """Run the end-of-day job once, the first tick after dismissal.

        Absences are DERIVED from the absence of a scan, so something has to decide
        the day is over. Hanging that off the clock tick means there is no second
        timer to keep in sync and no scheduler to install on the Pi.

        The latch is what makes this cheap: the job runs at most once per day per
        process, so the check that happens every second is a date comparison. The job
        itself is idempotent anyway -- the latch is about not doing pointless work,
        not about correctness.

        Its known limitation: a day the kiosk was never started past dismissal is
        never closed. Opening the kiosk later that day still closes it, because this
        fires on the first tick after launch too, but a day it never ran at all needs
        someone to open the kiosk before the roster reflects it.
        """
        day = now.date().isoformat()
        if self._closed_for == day:
            return

        try:
            # Cached per day: this runs on every clock tick, and reading the school-day
            # row once a second for eight hours to learn a value that cannot change is
            # a database query the gate does not need.
            if self._dismissal[0] != day:
                self._dismissal = (day, self.service.school_day(day).dismissal_time)
            if now.time() < self._dismissal[1]:
                return

            # A day nobody scanned is a day the gate did not run, so there is nothing
            # to close. Absence is derived from a student having no scan AMONG a day of
            # scans; with no scans at all there is no evidence the school was even
            # open, and closing marks the entire roster absent -- which is what
            # close_open_days' own docstring calls the worst thing this job could do.
            #
            # Opening the kiosk one evening to look at the records is enough to trigger
            # it, and the fabricated 0% day then acts as a leverage point on the
            # attendance trend. Only the AUTOMATIC job is guarded: a deliberate close
            # from the records UI or a script still behaves exactly as before, because
            # then someone has actually asked for it.
            scanned = self.service.conn.execute(
                "SELECT COUNT(*) FROM scan_events WHERE date = ?", (day,)
            ).fetchone()[0]
            if not scanned:
                self._closed_for = day    # latch: don't re-check every tick all evening
                return

            self._closed_for = day    # latch BEFORE the work, so a failure cannot loop
            result = self.service.close_day(day, at=now)
        except Exception as exc:      # noqa: BLE001 - never take the gate down for this
            # Includes the connection being closed during shutdown, when a queued
            # clock tick can still arrive after the database has gone.
            self.status_session.setText(f"End-of-day job failed: {exc}")
            return

        if result.skipped or not result.absent:
            return
        self.status_session.setText(
            f"Day closed: {result.absent} absent, {result.exit_missing} without an out-scan"
        )

    # -- worker signals -----------------------------------------------------

    @Slot(object)
    def on_stats(self, stats: QueueStats) -> None:
        # HALTED survives the tick. The breaker fires once and the next stats update
        # arrives four seconds later; overwriting it made the alarm effectively
        # invisible, which is the opposite of what a spend cap is for.
        if not self._sms_halted:
            self._set_provider_status(stats)
        self.status_unsent.setText(
            f"{stats.unsent} unsent" if stats.unsent != 1 else "1 unsent"
        )
        self.status_unsent.setProperty("alert", "true" if stats.unsent else "false")
        _restyle(self.status_unsent)

    def _set_provider_status(self, stats: QueueStats) -> None:
        """A module that is not there is said so in the bar, not left to be inferred.

        Same treatment as a dead camera: text, tooltip, amber. The alternative is a bar
        reading "SMS: gsm" while the queue quietly waits for hardware nobody has
        noticed is unplugged.
        """
        if stats.provider_available:
            self.status_provider.setText(f"SMS: {stats.provider}")
            self.status_provider.setToolTip("")
            self.status_provider.setProperty("alert", "false")
        else:
            self.status_provider.setText(f"SMS: {stats.provider} unavailable")
            self.status_provider.setToolTip(
                stats.provider_detail
                or "The notification module is not answering. Queued messages are "
                   "waiting and will go out once it is back."
            )
            self.status_provider.setProperty("alert", "true")
        _restyle(self.status_provider)

    @Slot(str, str)
    def on_camera_status(self, state: str, message: str) -> None:
        """A dead camera is flagged in the status bar so it is noticed the same
        morning, rather than inferred afterwards from missing attendance data."""
        self.status_camera.setText(f"Cam: {state}")
        self.status_camera.setToolTip(message)
        self.status_camera.setProperty("alert", "true" if state == "error" else "false")
        _restyle(self.status_camera)

    def closeEvent(self, event) -> None:
        # Stop the clock before the camera: the tick reads the database, and on
        # shutdown that connection is about to be closed underneath it.
        self._clock_timer.stop()
        self.camera.shutdown()
        super().closeEvent(event)

    @Slot(str)
    def on_alarm(self, message: str) -> None:
        self._sms_halted = True
        self.status_provider.setText("SMS: HALTED")
        self.status_provider.setToolTip(message)
        self.status_provider.setProperty("alert", "true")
        _restyle(self.status_provider)
        self.status_unsent.setProperty("alert", "true")
        _restyle(self.status_unsent)
        print(f"[ALARM] {message}")
