"""The custody desk: what is held, who signs it out, and what came back.

Separate from the kiosk on purpose. The gate is a queue of students moving through in
seconds; this is a teacher standing at a cupboard. They are different jobs at different
moments and putting them on one screen would make both worse.

**Role note.** Until RBAC lands (build step 14), "adviser" here is a name typed into a
box, not an identity the system has verified. That is a real limitation and it is
stated on the screen rather than hidden -- an audit trail that records an unverified
name is still far better than a handover nobody wrote down, but it is not proof.
"""

from __future__ import annotations

from datetime import datetime

from qtpy.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import custody

COLUMNS = ("Tag", "Student", "Item", "Purpose", "Status", "Request today")


class ReleaseDialog(QDialog):
    """Sign an item out. The reason field appears only when it is actually needed."""

    def __init__(self, item_text: str, backed: bool, advisers, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Release item")
        self.setModal(True)

        root = QVBoxLayout(self)

        heading = QLabel(f"Release — {item_text}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            "A teacher's request covers this section today."
            if backed else
            "No teacher request covers this section today. Releasing anyway is "
            "allowed, but the reason is recorded and the release is flagged."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self.adviser = QComboBox()
        for user_id, name in advisers:
            self.adviser.addItem(name, user_id)
        form.addRow("Released to", self.adviser)

        self.reason = QLineEdit()
        self.reason.setPlaceholderText(
            "" if backed else "e.g. Art class moved from Thursday"
        )
        form.addRow("Reason" + ("" if backed else " *"), self.reason)
        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        self._backed = backed
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _try_accept(self) -> None:
        if not self._backed and not self.reason.text().strip():
            self.error.setText("A release with no teacher request needs a reason.")
            return
        if self.adviser.currentData() is None:
            self.error.setText("No adviser accounts exist yet.")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "released_to": self.adviser.currentData(),
            "reason": self.reason.text().strip() or None,
        }


class CustodyWindow(QWidget):
    """Everything not yet back, and the two actions that move it along."""

    def __init__(self, conn, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setObjectName("CustodyWindow")
        self.setWindowTitle("TRACKIFY - Custody desk")
        self.resize(980, 560)

        root = QVBoxLayout(self)

        heading = QLabel("Items held at the gate")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        self.caveat = QLabel(
            "Adviser identity is not yet verified by a login — the name recorded is "
            "the one selected here."
        )
        self.caveat.setObjectName("DialogHint")
        self.caveat.setWordWrap(True)
        root.addWidget(self.caveat)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.release_btn = QPushButton("Release to adviser")
        self.release_btn.clicked.connect(self._release)
        self.return_storage_btn = QPushButton("Return to storage")
        self.return_storage_btn.clicked.connect(lambda: self._give_back("storage"))
        self.return_student_btn = QPushButton("Return to student")
        self.return_student_btn.clicked.connect(lambda: self._give_back("student"))
        self.request_btn = QPushButton("Teacher request...")
        self.request_btn.clicked.connect(self._request)

        for button in (self.release_btn, self.return_storage_btn,
                       self.return_student_btn):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.request_btn)
        root.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("DialogHint")
        root.addWidget(self.status)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        self._rows = custody.outstanding(self.conn)
        today = datetime.now().date().isoformat()

        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            backed = self._request_for(row["student_id"], today) is not None
            cells = (
                row["storage_ref"] or "—",
                f"{row['first_name']} {row['last_name']}",
                row["item_description"],
                row["purpose"] or "—",
                row["status"],
                "yes" if backed else "no",
            )
            for column, text in enumerate(cells):
                self.table.setItem(index, column, QTableWidgetItem(str(text)))

        held = sum(1 for r in self._rows if r["status"] == "held")
        out = sum(1 for r in self._rows if r["status"] == "released")
        # Items still signed out are the ones worth chasing: out of storage, and
        # nobody has said where they went.
        self.status.setText(
            f"{held} held · {out} signed out"
            + ("  ← still out, not yet returned" if out else "")
        )

    def _request_for(self, student_id: int, day: str):
        return self.conn.execute(
            """SELECT h.id FROM hazard_requests h
               JOIN students s ON s.section_id = h.section_id
               WHERE s.id = ? AND h.date = ? LIMIT 1""",
            (student_id, day),
        ).fetchone()

    def _selected(self):
        index = self.table.currentRow()
        if index < 0 or index >= len(self._rows):
            self.status.setText("Select an item first.")
            return None
        return self._rows[index]

    def _advisers(self):
        return [
            (r["id"], r["full_name"])
            for r in self.conn.execute(
                "SELECT id, full_name FROM users WHERE role IN ('adviser', 'admin') "
                "AND active = 1 ORDER BY full_name"
            )
        ]

    # -- actions ------------------------------------------------------------

    def _release(self) -> None:
        row = self._selected()
        if row is None:
            return
        if row["status"] != "held":
            self.status.setText("That item is already signed out.")
            return

        today = datetime.now().date().isoformat()
        backed = self._request_for(row["student_id"], today) is not None
        dialog = ReleaseDialog(
            f"{row['item_description']} ({row['storage_ref'] or 'no tag'})",
            backed, self._advisers(), self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        try:
            custody.release(self.conn, row["id"], **dialog.values())
        except custody.CustodyError as exc:
            QMessageBox.warning(self, "Cannot release", str(exc))
            return
        self.refresh()

    def _give_back(self, to: str) -> None:
        row = self._selected()
        if row is None:
            return
        try:
            custody.give_back(self.conn, row["id"], to)
        except custody.CustodyError as exc:
            QMessageBox.warning(self, "Cannot return", str(exc))
            return
        self.refresh()

    def _request(self) -> None:
        dialog = HazardRequestDialog(self.conn, self)
        if dialog.exec() == QDialog.Accepted:
            self.refresh()


class HazardRequestDialog(QDialog):
    """A teacher declaring that a section needs hazardous tools for a subject.

    This is what turns a release from a judgement call at the cupboard into an
    expected event, so it deliberately takes ten seconds to fill in.
    """

    def __init__(self, conn, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Tools needed for a class")
        self.setModal(True)

        root = QVBoxLayout(self)
        heading = QLabel("Declare tools needed")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        form = QFormLayout()

        self.section = QComboBox()
        for row in conn.execute(
            "SELECT id, grade_level, name FROM sections ORDER BY grade_level, name"
        ):
            self.section.addItem(f"{row['grade_level']}-{row['name']}", row["id"])
        form.addRow("Section", self.section)

        self.date = QLineEdit(datetime.now().date().isoformat())
        form.addRow("Date", self.date)

        self.subject = QLineEdit()
        self.subject.setPlaceholderText("e.g. Art")
        form.addRow("Subject *", self.subject)

        self.item_type = QLineEdit()
        self.item_type.setPlaceholderText("e.g. cutters, scissors")
        form.addRow("Tools *", self.item_type)

        self.teacher = QComboBox()
        for row in conn.execute(
            "SELECT id, full_name FROM users WHERE active = 1 ORDER BY full_name"
        ):
            self.teacher.addItem(row["full_name"], row["id"])
        form.addRow("Requested by", self.teacher)

        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _try_accept(self) -> None:
        if not self.subject.text().strip() or not self.item_type.text().strip():
            self.error.setText("Subject and tools are both required.")
            return
        if self.section.currentData() is None:
            self.error.setText("No sections exist yet.")
            return

        custody.request_tools(
            self.conn,
            self.section.currentData(),
            self.date.text().strip(),
            self.subject.text().strip(),
            self.item_type.text().strip(),
            requested_by=self.teacher.currentData(),
        )
        self.accept()
