"""The attendance records screen: register, corrections, edit log, export.

Lives inside the kiosk window as a page rather than as a separate top-level window.
That is deliberate: a separate window takes focus away from the kiosk's hidden scan
input, so the gate would silently stop accepting scans while records were open, with
nothing on screen to explain why. As a page, `scan_input` keeps focus and any scan
closes this and returns to the gate.

docs/flow.md 8 -- the station screen never shows history -- is why the password is
asked on every open, not once per session.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date

from qtpy.QtCore import QPointF, Qt, Signal
from qtpy.QtGui import QColor, QFont
from qtpy.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import corrections, security
from ..core.corrections import CorrectionError, CorrectionType
from ..export import xlsx
from . import icons
from .roster import RosterPage

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

TYPE_LABELS = (
    ("Excused absence", CorrectionType.EXCUSED),
    ("Online participation", CorrectionType.ONLINE),
    ("Data error", CorrectionType.DATA_ERROR),
)

# Lighter than it was. The glyph now carries the status, so this only has to answer
# one question -- "did a person set this, or did the scanner?" -- and it should not
# compete with the mark it sits behind.
CORRECTED_TINT = QColor("#2A2718")
# Weekends tinted so an empty cell reads as "no class" rather than as a gap in the
# data. Without it a blank Saturday looks identical to a missing record.
WEEKEND_TINT = QColor("#161A1E")
SELECTION_TINT = QColor("#22303C")

# Item roles. The delegate reads these rather than parsing the cell's text, so the
# painted appearance and the underlying status cannot drift apart.
#
# A day cell ALWAYS carries STATUS_ROLE, empty string included. A totals cell never
# does, and that absence is how the delegate knows to leave it to Qt to draw.
STATUS_ROLE = Qt.UserRole + 1
CORRECTED_ROLE = Qt.UserRole + 2
WEEKEND_ROLE = Qt.UserRole + 3

# Drawn marks for the two statuses that carry the most meaning at a glance; letters
# for the rest. A register is scanned column by column looking for absences, and a
# shape finds the eye faster than a letter does.
STATUS_ICONS = {"present": "present", "absent": "absent"}
STATUS_LETTERS = {"late": "L", "excused": "E", "online": "O"}
STATUS_COLOURS = {
    "present": "#5BD98A",
    "absent": "#F0736F",
    "late": "#F5C451",
    "excused": "#7FB6E8",
    "online": "#7FB6E8",
}

LEGEND = (
    ("present", "present"), ("late", "late"), ("absent", "absent"),
    ("excused", "excused"), ("online", "online"),
)


class StatusDelegate(QStyledItemDelegate):
    """Paints a register cell: tint, then a centred mark.

    A delegate rather than QTableWidgetItem.setIcon, because setIcon puts the icon on
    the LEFT beside the text with no reliable way to centre an icon-only cell -- the
    usual fix is per-cell padding that breaks the moment the row height changes.

    It also puts all three visual signals (status, corrected, weekend) in one place
    instead of spreading them across item properties, which is what keeps them
    consistent with each other.
    """

    ICON_PX = 15

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Rendered once. Doing this per paint would redraw an SVG for every cell of
        # every repaint -- 31 days by 40 students is 1240 renders a scroll.
        self._icons = {
            status: icons.pixmap(name, self.ICON_PX, STATUS_COLOURS[status])
            for status, name in STATUS_ICONS.items()
        }

    def paint(self, painter, option, index) -> None:
        status = index.data(STATUS_ROLE)
        if status is None:                      # not a day cell -- totals, etc.
            super().paint(painter, option, index)
            return

        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, SELECTION_TINT)
        elif index.data(CORRECTED_ROLE):
            painter.fillRect(option.rect, CORRECTED_TINT)
        elif index.data(WEEKEND_ROLE):
            painter.fillRect(option.rect, WEEKEND_TINT)

        pixmap = self._icons.get(status)
        if pixmap is not None:
            ratio = pixmap.devicePixelRatio() or 1.0
            width, height = pixmap.width() / ratio, pixmap.height() / ratio
            painter.drawPixmap(
                QPointF(option.rect.center().x() - width / 2 + 0.5,
                        option.rect.center().y() - height / 2 + 0.5),
                pixmap,
            )
        elif status in STATUS_LETTERS:
            font = QFont(option.font)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(STATUS_COLOURS[status]))
            painter.drawText(option.rect, Qt.AlignCenter, STATUS_LETTERS[status])
        painter.restore()


# --- password ---------------------------------------------------------------

class PasswordDialog(QDialog):
    """Asks for the records password, or asks to set one on first use."""

    def __init__(self, conn: sqlite3.Connection, gate: security.AttemptGate,
                 parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.gate = gate
        self.first_run = not security.is_set(conn)
        self.setWindowTitle("Attendance records")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel(
            "Set a records password" if self.first_run else "Attendance records"
        )
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            "No password has been set yet. Choose one now -- it will be needed every "
            "time these records are opened."
            if self.first_run else
            "Enter the records password."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("New password" if self.first_run else "Password", self.password)

        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.Password)
        if self.first_run:
            form.addRow("Repeat", self.confirm)
        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.password.setFocus()

    def _try_accept(self) -> None:
        try:
            if self.first_run:
                if self.password.text() != self.confirm.text():
                    self.error.setText("Those do not match.")
                    return
                security.set_password(self.conn, self.password.text())
            else:
                self.gate.check(self.conn, self.password.text())
        except security.PasswordError as exc:
            self.error.setText(str(exc))
            self.password.clear()
            return
        self.accept()


class ChangePasswordDialog(QDialog):
    def __init__(self, conn: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Change records password")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel("Change records password")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        form = QFormLayout()
        self.current = QLineEdit(); self.current.setEchoMode(QLineEdit.Password)
        self.new = QLineEdit(); self.new.setEchoMode(QLineEdit.Password)
        self.confirm = QLineEdit(); self.confirm.setEchoMode(QLineEdit.Password)
        form.addRow("Current", self.current)
        form.addRow("New", self.new)
        form.addRow("Repeat", self.confirm)
        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.current.setFocus()

    def _try_accept(self) -> None:
        if self.new.text() != self.confirm.text():
            self.error.setText("Those do not match.")
            return
        try:
            security.set_password(self.conn, self.new.text(),
                                  current=self.current.text())
        except security.PasswordError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


# --- correcting one cell ----------------------------------------------------

class CorrectionDialog(QDialog):
    """Type, resulting status, reason, and who is doing it -- all mandatory."""

    def __init__(self, student_name: str, day: str, current: str | None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Correct attendance")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel(f"{student_name} - {day}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            f"Currently recorded as: {current or 'no record'}. The original is kept; "
            "this adds a correction on top of it."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()

        self.kind = QComboBox()
        for label, value in TYPE_LABELS:
            # The plain string, not the enum member: Qt stores userData as a variant
            # and hands a str-subclass enum back as a bare str, so storing the member
            # only creates the illusion of one. _kind() converts back explicitly.
            self.kind.addItem(label, value.value)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("Correction", self.kind)

        self.status = QComboBox()
        for status in corrections.DATA_ERROR_STATUSES:
            self.status.addItem(status, status)
        form.addRow("Status", self.status)

        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. medical certificate on file")
        form.addRow("Reason *", self.reason)

        self.actor = QLineEdit()
        self.actor.setPlaceholderText("your name")
        form.addRow("Recorded by *", self.actor)
        root.addLayout(form)

        # The claim this screen can and cannot support, said out loud rather than
        # implied. Same honesty as the custody desk's unverified adviser.
        caveat = QLabel(
            "The name is recorded as typed and is not verified by a login."
        )
        caveat.setObjectName("DialogHint")
        caveat.setWordWrap(True)
        root.addWidget(caveat)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._kind_changed()
        self.reason.setFocus()

    def _kind(self) -> CorrectionType:
        return CorrectionType(self.kind.currentData())

    def _kind_changed(self) -> None:
        """Only a data error lets the operator pick a status.

        The others name a specific circumstance and the status follows from it --
        offering a choice would invite recording an excused absence as 'present'.
        """
        kind = self._kind()
        is_error = kind is CorrectionType.DATA_ERROR
        self.status.setEnabled(is_error)
        if not is_error:
            fixed = corrections.TYPE_STATUS[kind]
            self.status.setCurrentIndex(self.status.findData(fixed))

    def _try_accept(self) -> None:
        if not self.reason.text().strip():
            self.error.setText("A reason is required.")
            return
        if not self.actor.text().strip():
            self.error.setText("Your name is required.")
            return
        self.accept()

    def values(self) -> dict:
        kind = self._kind()
        return {
            "kind": kind,
            "status": (self.status.currentData()
                       if kind is CorrectionType.DATA_ERROR else None),
            "reason": self.reason.text().strip(),
            "actor_name": self.actor.text().strip(),
        }


class SuspendDialog(QDialog):
    """Class suspension: one date, the whole section."""

    def __init__(self, section_label: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Class suspension")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel(f"Suspend classes - {section_label}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            "Every student in this section is excused for the date, and the day "
            "leaves their attendance rate rather than counting against it."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self.day = QLineEdit(Date.today().isoformat())
        form.addRow("Date", self.day)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. Typhoon signal no. 2")
        form.addRow("Reason *", self.reason)
        self.actor = QLineEdit()
        self.actor.setPlaceholderText("your name")
        form.addRow("Recorded by *", self.actor)
        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _try_accept(self) -> None:
        try:
            Date.fromisoformat(self.day.text().strip())
        except ValueError:
            self.error.setText("Date must be YYYY-MM-DD.")
            return
        if not self.reason.text().strip() or not self.actor.text().strip():
            self.error.setText("Reason and your name are both required.")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "day": self.day.text().strip(),
            "reason": self.reason.text().strip(),
            "actor_name": self.actor.text().strip(),
        }


# --- the page ---------------------------------------------------------------

class RecordsPage(QWidget):
    """Register and edit log, as two stacked views."""

    closed = Signal()

    def __init__(self, conn: sqlite3.Connection, *, school_name: str = "",
                 config=None, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.school_name = school_name
        # Only the analytics export needs it (risk band cutoffs and the saturation
        # constants), so it stays optional rather than forcing every existing caller
        # and test to build one.
        self.config = config
        self.setObjectName("RecordsPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 0, 4, 0)
        root.setSpacing(10)

        # --- heading -------------------------------------------------------
        title_row = QHBoxLayout()
        self.title = QLabel("Attendance register")
        self.title.setObjectName("PageTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("PageSubtitle")
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.subtitle)
        root.addLayout(title_row)

        # --- controls ------------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.section = QComboBox()
        self.section.currentIndexChanged.connect(self.refresh)
        self.section_label = QLabel("Section")
        bar.addWidget(self.section_label)
        bar.addWidget(self.section)

        self.month = QComboBox()
        for index, name in enumerate(MONTHS, start=1):
            self.month.addItem(name, index)
        self.month.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.month)

        self.year = QSpinBox()
        self.year.setRange(2020, 2100)
        self.year.valueChanged.connect(self.refresh)
        bar.addWidget(self.year)
        bar.addStretch(1)

        # Three weights, because "Export" and "Close" are not the same kind of action
        # and styling them identically makes the operator read every one of them.
        self.btn_log = self._button("Edit log", "unfinished", self._toggle_log)
        self.btn_roster = self._button("Student roster", None, self._toggle_roster)
        self.btn_suspend = self._button("Class suspension", None, self._suspend)
        self.btn_password = self._button("Change password", None, self._change_password)
        self.btn_export = self._button("Export XLSX", None, self._export,
                                       kind="ToolbarPrimary")
        self.btn_analytics = self._button("Export analytics", None, self._export_analytics)
        self.btn_summaries = self._button("Send weekly summaries", None,
                                          self._send_summaries)
        self.btn_close = self._button("Close", "back", self.closed.emit,
                                      kind="ToolbarQuiet")
        for button in (self.btn_log, self.btn_roster, self.btn_suspend,
                       self.btn_password, self.btn_summaries, self.btn_analytics,
                       self.btn_export, self.btn_close):
            bar.addWidget(button)
        root.addLayout(bar)
        # The attendance controls are meaningless over a roster; the roster carries its
        # own search and section filter. Held together so _show_view can hide them.
        self._attendance_controls = (self.month, self.year, self.btn_suspend,
                                     self.btn_export, self.btn_analytics,
                                     self.btn_summaries, self.btn_log,
                                     self.section, self.section_label)

        # --- legend --------------------------------------------------------
        # The glyphs explain themselves here rather than in a manual nobody opens.
        self.legend = QWidget()
        self.legend.setObjectName("Legend")
        legend_row = QHBoxLayout(self.legend)
        legend_row.setContentsMargins(0, 0, 0, 0)
        legend_row.setSpacing(18)
        for status, label in LEGEND:
            legend_row.addWidget(self._legend_item(status, label))
        shaded = QLabel("shaded = set by a person, not a scan")
        shaded.setObjectName("LegendNote")
        legend_row.addSpacing(10)
        legend_row.addWidget(shaded)
        legend_row.addStretch(1)
        root.addWidget(self.legend)

        # --- views ---------------------------------------------------------
        self.views = QStackedWidget()
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectItems)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setItemDelegate(StatusDelegate(self.table))
        self.table.verticalHeader().setDefaultSectionSize(30)
        # Qt derives a minimum section width from the header text plus its padding,
        # and silently overrides setColumnWidth with it. Without this the day columns
        # inflate and the totals block slides off the right-hand edge.
        self.table.horizontalHeader().setMinimumSectionSize(24)
        self.table.cellDoubleClicked.connect(self._correct_cell)
        self.table.cellClicked.connect(self._correct_cell)
        self.views.addWidget(self.table)

        self.log = QTableWidget(0, 6)
        self.log.setHorizontalHeaderLabels(
            ("When", "Who (typed)", "Student and date", "From", "To", "Reason")
        )
        self.log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log.setAlternatingRowColors(True)
        self.log.setShowGrid(False)
        self.log.verticalHeader().setVisible(False)
        self.log.verticalHeader().setDefaultSectionSize(28)
        self.views.addWidget(self.log)

        # Third view rather than a separate kiosk page: this way the roster inherits the
        # password gate, the close handling, and the rule that any scan closes the staff
        # area and returns to the gate. A separate page would re-implement all three.
        self.roster = RosterPage(conn)
        self.roster.changed.connect(self._roster_changed)
        self.views.addWidget(self.roster)
        root.addWidget(self.views, 1)

        self.status = QLabel("")
        self.status.setObjectName("DialogHint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._load_sections()
        today = Date.today()
        self.year.setValue(today.year)
        self.month.setCurrentIndex(today.month - 1)
        self.refresh()

    def _legend_item(self, status: str, label: str) -> QWidget:
        item = QWidget()
        row = QHBoxLayout(item)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        mark = QLabel()
        mark.setObjectName("LegendMark")
        if status in STATUS_ICONS:
            mark.setPixmap(icons.pixmap(STATUS_ICONS[status], 13,
                                        STATUS_COLOURS[status]))
        else:
            mark.setText(STATUS_LETTERS[status])
            mark.setStyleSheet(
                f"color: {STATUS_COLOURS[status]}; font-weight: 600;"
            )
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedWidth(16)

        text = QLabel(label)
        text.setObjectName("LegendText")
        row.addWidget(mark)
        row.addWidget(text)
        return item

    def _button(self, text, icon_name, slot, *, kind="ToolbarButton") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(kind)
        button.setFocusPolicy(Qt.NoFocus)      # never steal the scanner's Enter
        button.setCursor(Qt.PointingHandCursor)
        if icon_name:
            button.setIcon(icons.icon(icon_name, 18, "#9AACA6"))
        button.clicked.connect(slot)
        return button

    # -- data -----------------------------------------------------------------

    def _load_sections(self) -> None:
        self.section.blockSignals(True)
        self.section.clear()
        for row in self.conn.execute(
            "SELECT id, grade_level, name FROM sections ORDER BY grade_level, name"
        ):
            self.section.addItem(f"{row['grade_level']}-{row['name']}", row["id"])
        self.section.blockSignals(False)

    @property
    def section_id(self):
        return self.section.currentData()

    def refresh(self) -> None:
        if self.section_id is None:
            self.status.setText("No sections exist yet.")
            return

        year, month = self.year.value(), self.month.currentData()
        days, rows = corrections.register(self.conn, self.section_id, year, month)
        self._days, self._rows = days, rows

        self.table.clear()
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(days) + 5)
        self.table.setHorizontalHeaderLabels(
            [str(Date.fromisoformat(d).day) for d in days] + ["P", "L", "A", "E", "Rate"]
        )
        self.table.setVerticalHeaderLabels([r.name for r in rows])

        for row_index, row in enumerate(rows):
            for column, day in enumerate(days):
                cell = row.cells[day]
                item = QTableWidgetItem("")
                # Everything the delegate needs, as data. Nothing is inferred from the
                # cell's text, so what is painted and what is recorded cannot drift.
                item.setData(STATUS_ROLE, cell.status or "")
                item.setData(CORRECTED_ROLE, cell.corrected)
                item.setData(WEEKEND_ROLE, Date.fromisoformat(day).weekday() >= 5)
                if cell.corrected:
                    item.setToolTip("Corrected by a person after scanning")
                elif cell.status:
                    item.setToolTip(cell.status)
                self.table.setItem(row_index, column, item)

            totals = [row.present, row.late, row.absent, row.excused,
                      "-" if row.rate is None else f"{row.rate:.0%}"]
            for offset, value in enumerate(totals):
                # No STATUS_ROLE: that absence is what tells the delegate to leave
                # this cell to Qt rather than trying to paint a status in it.
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                if offset == 4:
                    font = item.font(); font.setBold(True); item.setFont(font)
                self.table.setItem(row_index, len(days) + offset, item)

        # Narrow enough that a 31-day month plus the totals fits a kiosk display
        # without the summary sliding off the right-hand edge.
        for column in range(len(days)):
            self.table.setColumnWidth(column, 27)
        for offset in range(5):
            self.table.setColumnWidth(len(days) + offset, 46)
        self.table.verticalHeader().setMinimumWidth(150)

        self.subtitle.setText(
            f"{self.section.currentText()}  -  {MONTHS[month - 1]} {year}"
        )
        corrected = sum(1 for r in rows for c in r.cells.values() if c.corrected)
        self.status.setText(
            f"{len(rows)} student(s)  -  {corrected} corrected cell(s)  -  "
            "click a day to correct it"
        )

    def _refresh_log(self) -> None:
        entries = corrections.edit_log(self.conn, section_id=self.section_id)
        self.log.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            # old_value reads "Name YYYY-MM-DD: status". Splitting it keeps the
            # student and date in one column and the old status in another, instead
            # of printing the status twice on every row.
            subject, _, from_status = (entry["old_value"] or "").rpartition(": ")
            cells = (
                (entry["occurred_at"] or "")[:19].replace("T", " "),
                entry["actor_name"] or "(not recorded)",
                subject or entry["old_value"] or entry["action"],
                from_status,
                entry["new_value"] or "",
                entry["reason"] or "",
            )
            for column, text in enumerate(cells):
                self.log.setItem(index, column, QTableWidgetItem(str(text)))
        self.log.resizeColumnsToContents()

    # -- actions --------------------------------------------------------------

    REGISTER, LOG, ROSTER = 0, 1, 2

    def _show_view(self, index: int) -> None:
        """Switch views, setting the WHOLE toolbar state every time.

        Each toggle used to undo only its own changes, which meant going roster -> log
        left the month and year pickers hidden and two different buttons both reading
        "Back to register". Setting every affected widget on every switch costs nothing
        and removes a whole class of that.
        """
        self.views.setCurrentIndex(index)
        self.btn_log.setText("Back to register" if index == self.LOG else "Edit log")
        self.btn_roster.setText(
            "Back to register" if index == self.ROSTER else "Student roster")

        # The month, year and section pickers belong to the register. Left visible over
        # a roster they look like filters and do nothing.
        for widget in self._attendance_controls:
            widget.setVisible(index != self.ROSTER)
        # Only the register uses the status glyphs.
        self.legend.setVisible(index == self.REGISTER)

        if index == self.REGISTER:
            self.title.setText("Attendance register")
            self.refresh()
        elif index == self.LOG:
            self.title.setText("Edit log")
            self._refresh_log()
            self.status.setText(
                "Every correction ever made to this section. Names are as typed and "
                "are not verified by a login."
            )
        else:
            self.title.setText("Student roster")
            self.subtitle.setText("")
            self.roster.refresh()
            self.status.setText("")

    def _toggle_log(self) -> None:
        self._show_view(self.REGISTER if self.views.currentIndex() == self.LOG
                        else self.LOG)

    def _toggle_roster(self) -> None:
        self._show_view(self.REGISTER if self.views.currentIndex() == self.ROSTER
                        else self.ROSTER)

    def _roster_changed(self) -> None:
        """A student was added, edited or deactivated -- the register's section list and
        its rows may both be stale now."""
        self._load_sections()

    def _correct_cell(self, row_index: int, column: int) -> None:
        if self.views.currentIndex() != self.REGISTER or column >= len(self._days):
            return
        row = self._rows[row_index]
        day = self._days[column]
        cell = row.cells[day]

        dialog = CorrectionDialog(row.name, day, cell.status, self)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.values()
        try:
            corrections.correct(
                self.conn, row.student_id, day, values["kind"],
                status=values["status"], reason=values["reason"],
                actor_name=values["actor_name"],
            )
        except CorrectionError as exc:
            QMessageBox.warning(self, "Cannot correct", str(exc))
            return
        self.refresh()

    def _suspend(self) -> None:
        if self.section_id is None:
            return
        dialog = SuspendDialog(self.section.currentText(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            ids = corrections.suspend_section(
                self.conn, self.section_id, values["day"],
                reason=values["reason"], actor_name=values["actor_name"],
            )
        except CorrectionError as exc:
            QMessageBox.warning(self, "Cannot suspend", str(exc))
            return
        self.refresh()
        self.status.setText(f"{len(ids)} student(s) excused for {values['day']}.")

    def _export(self) -> None:
        if self.section_id is None:
            return
        year, month = self.year.value(), self.month.currentData()
        suggested = xlsx.default_filename(self.section.currentText(), year, month)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export register", suggested, "Excel workbook (*.xlsx)"
        )
        if not path:
            return
        try:
            written = xlsx.export_register(
                self.conn, self.section_id, year, month, path,
                school_name=self.school_name,
            )
        except Exception as exc:            # noqa: BLE001 - a locked file is common
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.status.setText(f"Exported to {written}")

    def _export_analytics(self) -> None:
        """The trend, risk, AHP and screening workbook.

        Deliberately a separate file from the register: the register is the SF2-shaped
        sheet staff already check, and burying it inside a report is how it stops being
        checked. Whole-database scope, not the selected month -- a trend over one month
        of a 20-day study would be most of the data thrown away.
        """
        from ..core.config import load_config
        from ..export import analytics

        config = self.config or load_config()
        scope = self.section.currentText() or "all"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export analytics", analytics.default_filename(scope),
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        try:
            written = analytics.export_analytics(
                self.conn, config, path,
                section_id=self.section_id, school_name=self.school_name,
            )
        except Exception as exc:            # noqa: BLE001 - a locked file is common
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.status.setText(
            f"Analytics exported to {written}. Check the Summary sheet first -- it "
            "lists every caveat in force."
        )

    def _send_summaries(self) -> None:
        """Queue this week's attendance summary for every consenting guardian.

        Counted first and confirmed before anything is written. Seventy-odd texts
        leaving at once is a real event with a real cost, and the person pressing the
        button should see the number before it happens rather than afterwards.

        Whole roster, not the selected section: a summary is per guardian, and a parent
        with children in two sections should not get two half-reports.
        """
        from ..core.config import load_config
        from ..notify import periodic

        config = self.config or load_config()
        if not config.notifications.weekly_summary:
            QMessageBox.information(
                self, "Weekly summaries are off",
                "Set weekly_summary = true under [notifications] in config.toml to "
                "enable them.",
            )
            return

        preview = periodic.weekly_summaries(self.conn, config, dry_run=True)
        if not preview.eligible:
            QMessageBox.information(
                self, "Nothing to send",
                f"No attendance was recorded for {preview.period} "
                f"({preview.start} to {preview.end}), so there is nothing to summarise.",
            )
            return

        answer = QMessageBox.question(
            self, "Send weekly summaries",
            f"Queue an attendance summary for the week of {preview.period} "
            f"({preview.start} to {preview.end})?\n\n"
            f"{preview.eligible} student(s) have attendance recorded. One message goes "
            "to each consenting guardian; siblings are combined into a single text.\n\n"
            "Messages are queued, not sent here -- the scan station sends them.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            run = periodic.weekly_summaries(self.conn, config)
            self.conn.commit()
        except Exception as exc:            # noqa: BLE001 - never lose the screen
            QMessageBox.warning(self, "Could not queue summaries", str(exc))
            return

        refused = "; ".join(f"{n} {reason}" for reason, n in sorted(run.skipped.items()))
        self.status.setText(
            f"{run.queued} summary message(s) queued for {run.period}."
            + (f" Not queued: {refused}." if refused else "")
            + (" Re-pressing this queues nothing more for the same week."
               if run.queued else "")
        )

    def _change_password(self) -> None:
        if ChangePasswordDialog(self.conn, self).exec() == QDialog.Accepted:
            self.status.setText("Records password changed.")
