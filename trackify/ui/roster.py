"""The student roster screen: list, import from a spreadsheet, edit, deactivate.

Adding or correcting a student used to mean editing data/student-list.xlsx and asking the
developer to re-seed. That is fine while building and wrong for a system handed to a
school -- a transferee in November should not need the developer.

Lives as a view inside RecordsPage's stack rather than as its own page, so it inherits
the password gate, the close handling and, most importantly, the rule that any scan
closes the staff area and returns to the gate.

Everything that writes goes through trackify.core.enrolment, which is where the rules
about consent, duplicates and auditing live. This file is presentation only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QCheckBox,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import enrolment, roster
from ..core.config import PROJECT_ROOT
from ..core.enrolment import LRN_CHANGED, NEW, UNCHANGED, UPDATED, EnrolmentError
from ..core.mobile import for_display
from . import icons

COLUMNS = ("LRN", "Name", "Sex", "Section", "Guardian", "Contact", "Status")

# Rows the operator is meant to act on, marked rather than merely left blank. A blank
# cell reads as "nothing to do"; the point of importing a student with no guardian is
# that somebody then chases the number.
INCOMPLETE = "no contact"
INACTIVE = "inactive"


def _item(text: str, *, dim: bool = False) -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
    if dim:
        cell.setForeground(Qt.gray)
    return cell


class StudentDialog(QDialog):
    """Edit one student. Name and reason are mandatory, as they are for a correction."""

    def __init__(self, student: sqlite3.Row, sections: list[tuple[str, int]],
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit student")
        self.setModal(True)
        self._original_lrn = student["lrn"]

        root = QVBoxLayout(self)
        heading = QLabel(f"{student['last_name']}, {student['first_name']}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        form = QFormLayout()

        self.first = QLineEdit(student["first_name"])
        self.last = QLineEdit(student["last_name"])
        form.addRow("First name", self.first)
        form.addRow("Last name", self.last)

        self.lrn = QLineEdit(student["lrn"])
        form.addRow("LRN", self.lrn)

        self.lrn_warning = QLabel(
            "Changing the LRN stops their printed card working - the code is signed "
            "over it. Reprint their card with the QR generator."
        )
        self.lrn_warning.setObjectName("DialogWarning")
        self.lrn_warning.setWordWrap(True)
        self.lrn_warning.hide()
        self.lrn.textChanged.connect(self._lrn_changed)
        form.addRow("", self.lrn_warning)

        self.section = QComboBox()
        for label, section_id in sections:
            self.section.addItem(label, section_id)
        index = self.section.findData(student["section_id"])
        if index >= 0:
            self.section.setCurrentIndex(index)
        form.addRow("Section", self.section)

        # Blank first, so "not recorded" is the value you get by leaving it alone
        # rather than something the dialog quietly picks for you.
        self.sex = QComboBox()
        for label, value in (("not recorded", ""), ("Male", "M"), ("Female", "F")):
            self.sex.addItem(label, value)
        index = self.sex.findData(student["sex"] or "")
        if index >= 0:
            self.sex.setCurrentIndex(index)
        form.addRow("Sex", self.sex)

        sex_hint = QLabel(
            "DepEd SF2 is a male block and a female block. A student left as "
            "\"not recorded\" is listed under neither and the export says so."
        )
        sex_hint.setObjectName("DialogHint")
        sex_hint.setWordWrap(True)
        form.addRow("", sex_hint)

        self.guardian = QLineEdit(student["guardian_name"] or "")
        form.addRow("Guardian", self.guardian)

        self.mobile = QLineEdit(for_display(student["guardian_mobile"]))
        self.mobile.setPlaceholderText("0917 123 4567")
        form.addRow("Guardian mobile", self.mobile)

        self.consent = QCheckBox("Consent form is on file")
        self.consent.setChecked(bool(student["consent_on_file"]))
        form.addRow("", self.consent)

        consent_hint = QLabel(
            "Until this is ticked no message is ever sent about this student. Tick it "
            "only if you have seen the signed form."
        )
        consent_hint.setObjectName("DialogHint")
        consent_hint.setWordWrap(True)
        form.addRow("", consent_hint)

        self.reason = QLineEdit()
        self.reason.setPlaceholderText("why this is being changed")
        form.addRow("Reason", self.reason)

        self.actor = QLineEdit()
        self.actor.setPlaceholderText("your name")
        form.addRow("Your name", self.actor)

        root.addLayout(form)

        unverified = QLabel(
            "Your name is recorded as typed. It is not verified by a login."
        )
        unverified.setObjectName("DialogHint")
        unverified.setWordWrap(True)
        root.addWidget(unverified)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

    def _lrn_changed(self, text: str) -> None:
        self.lrn_warning.setVisible(text.strip() != self._original_lrn)

    def _try_accept(self) -> None:
        if not self.actor.text().strip():
            self.error.setText("Your name is required.")
            return
        if not self.reason.text().strip():
            self.error.setText("A reason is required.")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "first_name": self.first.text(),
            "last_name": self.last.text(),
            "lrn": self.lrn.text(),
            "section_id": self.section.currentData(),
            "sex": self.sex.currentData() or None,
            "guardian_name": self.guardian.text().strip() or None,
            "guardian_mobile": self.mobile.text().strip(),
            "consent_on_file": 1 if self.consent.isChecked() else 0,
        }

    @property
    def actor_name(self) -> str:
        return self.actor.text().strip()

    @property
    def reason_text(self) -> str:
        return self.reason.text().strip()


class DeactivateDialog(QDialog):
    """Deactivate rather than delete -- a student who has scanned cannot be removed."""

    def __init__(self, student_name: str, active: bool, parent=None) -> None:
        super().__init__(parent)
        self.readmit = not active
        self.setWindowTitle("Readmit student" if self.readmit else "Deactivate student")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel(student_name)
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            "Their card will work again at the gate."
            if self.readmit else
            "Their card stops working at the gate immediately. Their attendance history "
            "is kept -- this is not a deletion, and a student who has ever scanned "
            "cannot be deleted."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self.reason = QLineEdit()
        self.reason.setPlaceholderText(
            "returned from leave" if self.readmit else "transferred to another school")
        form.addRow("Reason", self.reason)
        self.actor = QLineEdit()
        self.actor.setPlaceholderText("your name")
        form.addRow("Your name", self.actor)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        root.addWidget(self.error)

    def _try_accept(self) -> None:
        if not self.actor.text().strip() or not self.reason.text().strip():
            self.error.setText("A reason and your name are both required.")
            return
        self.accept()

    @property
    def actor_name(self) -> str:
        return self.actor.text().strip()

    @property
    def reason_text(self) -> str:
        return self.reason.text().strip()


class ImportPreviewDialog(QDialog):
    """What the import will do, before it does any of it.

    103 rows arriving from a file the adviser emailed is not something to apply on
    trust, and the LRN-changed count in particular has a physical consequence -- cards
    that must be reprinted -- which nobody will act on if it is not said here.
    """

    def __init__(self, path: Path, plan: enrolment.ImportPlan, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import students")
        self.setModal(True)
        self.plan = plan

        root = QVBoxLayout(self)
        heading = QLabel(f"Importing {path.name}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        counts = plan.counts
        rows = [
            ("New", counts[NEW], "students who will be created"),
            ("Updated", counts[UPDATED],
             "a name, section, sex or guardian detail changed"),
            # Its own line because it is invisible in the one above. Re-importing the
            # office sheet after students.sex existed makes almost every row an
            # "Updated" whose only change is sex, and a screen that did not say so
            # would not mention the thing the import was run for.
            ("Sex recorded", len(plan.sex_recorded),
             "gaining M or F - needed for the DepEd SF2"),
            ("LRN changed", counts[LRN_CHANGED],
             "their printed card will stop working - reprint it"),
            ("Unchanged", counts[UNCHANGED], ""),
            ("Not in this file", len(plan.missing),
             "left alone - nothing will happen to them"),
            ("Skipped", len(plan.rejected), "no LRN, so no student and no card"),
        ]
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(("", "", ""))
        table.horizontalHeader().hide()
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setShowGrid(False)
        for index, (label, count, note) in enumerate(rows):
            table.setItem(index, 0, _item(label))
            table.setItem(index, 1, _item(str(count)))
            table.setItem(index, 2, _item(note, dim=True))
        table.resizeColumnsToContents()
        table.setMinimumHeight(len(rows) * 30 + 8)
        root.addWidget(table)

        if plan.card_reprints:
            names = ", ".join(c.candidate.full_name for c in plan.card_reprints[:6])
            more = "" if len(plan.card_reprints) <= 6 else f" and {len(plan.card_reprints) - 6} more"
            warning = QLabel(
                f"Reprint the cards for: {names}{more}. {enrolment.CARD_WARNING}"
            )
            warning.setObjectName("DialogWarning")
            warning.setWordWrap(True)
            root.addWidget(warning)

        consent_note = QLabel(
            "Imported students cannot be texted. Consent is never set by a file - tick "
            "it per student once you have seen the signed form."
        )
        consent_note.setObjectName("DialogHint")
        consent_note.setWordWrap(True)
        root.addWidget(consent_note)

        form = QFormLayout()
        self.actor = QLineEdit()
        self.actor.setPlaceholderText("your name")
        form.addRow("Your name", self.actor)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        self.import_button.setEnabled(plan.writes > 0)
        if not plan.writes:
            self.import_button.setText("Nothing to import")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        root.addWidget(self.error)

    def _try_accept(self) -> None:
        if not self.actor.text().strip():
            self.error.setText("Your name is required.")
            return
        self.accept()

    @property
    def actor_name(self) -> str:
        return self.actor.text().strip()


class RosterPage(QWidget):
    """The roster table plus its actions. Emits `changed` when the database moved."""

    changed = Signal()

    def __init__(self, conn: sqlite3.Connection, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setObjectName("RosterPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or LRN")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search, 1)

        self.section = QComboBox()
        self.section.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.section)

        self.btn_import = self._button("Import XLSX", None, self._import,
                                       kind="ToolbarPrimary")
        self.btn_edit = self._button("Edit", None, self._edit)
        self.btn_active = self._button("Deactivate", None, self._toggle_active)
        for button in (self.btn_import, self.btn_edit, self.btn_active):
            bar.addWidget(button)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit())
        root.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("DialogHint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._rows: list[sqlite3.Row] = []
        self._load_sections()
        self.refresh()

    def _button(self, text, icon_name, slot, *, kind="ToolbarButton") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(kind)
        button.setFocusPolicy(Qt.NoFocus)      # never steal the scanner's Enter
        button.setCursor(Qt.PointingHandCursor)
        if icon_name:
            button.setIcon(icons.icon(icon_name, 18, "#9AACA6"))
        button.clicked.connect(slot)
        return button

    # -- data ----------------------------------------------------------------

    def _load_sections(self) -> None:
        self.section.blockSignals(True)
        self.section.clear()
        self.section.addItem("All sections", None)
        for row in self.conn.execute(
            "SELECT id, grade_level, name FROM sections ORDER BY grade_level, name"
        ):
            self.section.addItem(f"{row['grade_level']}-{row['name']}", row["id"])
        self.section.blockSignals(False)

    def _sections(self) -> list[tuple[str, int]]:
        return [(f"{r['grade_level']}-{r['name']}", r["id"]) for r in self.conn.execute(
            "SELECT id, grade_level, name FROM sections ORDER BY grade_level, name")]

    def refresh(self) -> None:
        self._rows = enrolment.roster_rows(
            self.conn, section_id=self.section.currentData(),
            search=self.search.text(),
        )
        self.table.setRowCount(len(self._rows))
        incomplete = 0

        for index, row in enumerate(self._rows):
            inactive = not row["active"]
            has_contact = bool(row["guardian_mobile"])
            if not has_contact:
                incomplete += 1

            marks = []
            if inactive:
                marks.append(INACTIVE)
            if not has_contact:
                marks.append(INCOMPLETE)

            cells = (
                row["lrn"],
                f"{row['last_name']}, {row['first_name']}",
                row["sex"] or "-",
                f"{row['grade_level']}-{row['section_name']}",
                row["guardian_name"] or "-",
                for_display(row["guardian_mobile"]) or "-",
                " · ".join(marks),
            )
            for column, text in enumerate(cells):
                self.table.setItem(index, column, _item(text, dim=inactive))

        self.table.resizeColumnsToContents()
        total = len(self._rows)
        note = f" · {incomplete} with no guardian number" if incomplete else ""
        self.status.setText(f"{total} student{'' if total == 1 else 's'}{note}")
        self._selection_changed()

    def _selected(self) -> sqlite3.Row | None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        index = rows.pop()
        return self._rows[index] if index < len(self._rows) else None

    def _selection_changed(self) -> None:
        student = self._selected()
        self.btn_edit.setEnabled(student is not None)
        self.btn_active.setEnabled(student is not None)
        if student is not None:
            self.btn_active.setText("Readmit" if not student["active"] else "Deactivate")

    # -- actions -------------------------------------------------------------

    def _import(self) -> None:
        # Opens where the roster actually lives rather than the working directory,
        # which is wherever the kiosk happened to be launched from.
        start = PROJECT_ROOT / "data"
        path, _ = QFileDialog.getOpenFileName(
            self, "Import students", str(start if start.is_dir() else ""),
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        path = Path(path)

        try:
            candidates, rejected = roster.parse_workbook(path)
        except Exception as exc:            # noqa: BLE001 - a locked or odd file is common
            QMessageBox.warning(self, "Could not read that file", str(exc))
            return

        if not candidates and not rejected:
            QMessageBox.information(self, "Nothing to import",
                                    f"{path.name} has no student rows.")
            return

        plan = enrolment.plan_import(self.conn, candidates, rejected)
        dialog = ImportPreviewDialog(path, plan, self)
        if dialog.exec() != QDialog.Accepted:
            self.status.setText("Import cancelled. Nothing was changed.")
            return

        try:
            written = enrolment.apply_import(self.conn, plan,
                                             actor_name=dialog.actor_name)
        except EnrolmentError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        self._load_sections()
        self.refresh()
        self.changed.emit()

        summary = (f"Imported {path.name}: {written[NEW]} new, "
                   f"{written[UPDATED] + written[LRN_CHANGED]} updated.")
        if plan.card_reprints:
            summary += (f" {len(plan.card_reprints)} card(s) must be reprinted - "
                        "their LRN changed.")
        self.status.setText(summary)

    def _edit(self) -> None:
        student = self._selected()
        if student is None:
            return

        dialog = StudentDialog(student, self._sections(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            changed = enrolment.update_student(
                self.conn, student["id"], actor_name=dialog.actor_name,
                reason=dialog.reason_text, **dialog.values(),
            )
        except EnrolmentError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return

        self.refresh()
        self.changed.emit()
        if not changed:
            self.status.setText("Nothing changed.")
        elif "lrn" in changed:
            self.status.setText(
                f"Saved. {student['last_name']}'s LRN changed - reprint their card.")
        else:
            self.status.setText(f"Saved: {', '.join(sorted(changed))}.")

    def _toggle_active(self) -> None:
        student = self._selected()
        if student is None:
            return
        name = f"{student['last_name']}, {student['first_name']}"

        dialog = DeactivateDialog(name, bool(student["active"]), self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            enrolment.set_active(
                self.conn, student["id"], not student["active"],
                actor_name=dialog.actor_name, reason=dialog.reason_text,
            )
        except EnrolmentError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return

        self.refresh()
        self.changed.emit()
        self.status.setText(
            f"{name} is now {'active' if not student['active'] else 'inactive'}.")
