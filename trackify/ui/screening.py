"""Dialogs the guard fills in after an inspection.

Both are deliberately small. They are filled in at a gate, standing up, with a queue
of students waiting, and every field that is not strictly needed is a field that gets
filled in badly or not at all.

The one thing neither dialog will let you skip is the free-text description. The
category is for counting; the description is for knowing what actually happened when
someone reads the record a year later.
"""

from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..core import screening
from . import icons


class IncidentDialog(QDialog):
    """Category, description, severity, notes -- for a confirmed prohibited item."""

    def __init__(self, student_name: str, category: str | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Prohibited item")
        self.setObjectName("IncidentDialog")
        self.setModal(True)

        root = QVBoxLayout(self)

        # The icon of the card that was pressed, so the operator can see at a glance
        # that this dialog is about the thing they just clicked.
        head_row = QHBoxLayout()
        head_row.setSpacing(10)
        self.heading_icon = QLabel("")
        self.heading_icon.setObjectName("DialogIcon")
        if category is not None:
            self.heading_icon.setPixmap(icons.pixmap(category, 28, "#F0918F"))
        head_row.addWidget(self.heading_icon)

        heading = QLabel(f"Prohibited item — {student_name}")
        heading.setObjectName("DialogHeading")
        head_row.addWidget(heading)
        head_row.addStretch(1)
        root.addLayout(head_row)

        # The taxonomy is only as good as the guard's ability to apply it in a few
        # seconds, so the rule is on the screen rather than in a manual nobody reads.
        rule = QLabel(screening.DECISION_RULE)
        rule.setObjectName("DialogHint")
        rule.setWordWrap(True)
        root.addWidget(rule)

        form = QFormLayout()

        self.category = QComboBox()
        for cat in screening.CATEGORIES:
            self.category.addItem(f"{cat.label} — {cat.covers}", cat.code)
        self.category.currentIndexChanged.connect(self._category_changed)
        form.addRow("Category", self.category)

        self.description = QLineEdit()
        self.description.setPlaceholderText("e.g. folding knife, ~8 cm blade, side pocket")
        form.addRow("Description *", self.description)

        self.severity = QSpinBox()
        self.severity.setRange(screening.SEVERITY_MIN, screening.SEVERITY_MAX)
        form.addRow("Severity", self.severity)

        self.severity_reason = QLineEdit()
        self.severity_reason.setPlaceholderText(
            "required only if severity differs from the category default"
        )
        form.addRow("Reason", self.severity_reason)

        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(64)
        form.addRow("Notes", self.notes)

        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Seeded from the button the guard pressed, but deliberately NOT locked: the
        # bag is usually only opened properly after that first guess.
        if category is not None:
            index = self.category.findData(category)
            if index >= 0:
                self.category.setCurrentIndex(index)

        self._category_changed()
        self.description.setFocus()

    def _category_changed(self) -> None:
        """Pre-fill the severity so the common case needs no thought and no reason."""
        self.severity.setValue(screening.default_severity(self.category.currentData()))

    def _try_accept(self) -> None:
        """Validate with the same function the service uses, so the dialog cannot
        accept something the domain would then reject."""
        try:
            screening.validate_incident(
                self.category.currentData(),
                self.description.text(),
                self.severity.value(),
                self.severity_reason.text(),
            )
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "category": self.category.currentData(),
            "item_description": self.description.text().strip(),
            "severity": self.severity.value(),
            "severity_reason": self.severity_reason.text().strip() or None,
            "notes": self.notes.toPlainText().strip() or None,
        }


class CustodyDialog(QDialog):
    """A school tool collected at the gate.

    The purpose field is the whole point of this dialog: it is captured while the
    student is standing there and can say what the cutter is for, rather than being
    reconstructed later by someone guessing from a class schedule.
    """

    def __init__(self, student_name: str, next_tag: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("School tool collected")
        self.setObjectName("CustodyDialog")
        self.setModal(True)

        root = QVBoxLayout(self)

        heading = QLabel(f"School tool — {student_name}")
        heading.setObjectName("DialogHeading")
        root.addWidget(heading)

        hint = QLabel(
            "Write the tag number on the item before putting it in the box. "
            "Without it, 'held' does not tell anyone where the item is."
        )
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()

        self.description = QLineEdit()
        self.description.setPlaceholderText("e.g. utility cutter, yellow handle")
        form.addRow("Item *", self.description)

        self.purpose = QLineEdit()
        self.purpose.setPlaceholderText("e.g. for Art, 4th period")
        form.addRow("Purpose *", self.purpose)

        self.storage_ref = QLineEdit(next_tag)
        self.storage_ref.setPlaceholderText("bag tag or bin number")
        form.addRow("Tag *", self.storage_ref)

        root.addLayout(form)

        self.error = QLabel("")
        self.error.setObjectName("DialogError")
        self.error.setWordWrap(True)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.description.setFocus()

    def _try_accept(self) -> None:
        missing = [
            name for name, field in (
                ("item", self.description), ("purpose", self.purpose),
                ("tag", self.storage_ref),
            )
            if not field.text().strip()
        ]
        if missing:
            self.error.setText(f"Required: {', '.join(missing)}")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "item_description": self.description.text().strip(),
            "purpose": self.purpose.text().strip(),
            "storage_ref": self.storage_ref.text().strip(),
        }
