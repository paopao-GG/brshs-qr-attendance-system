"""The student roster page: listing, editing, deactivating, and the import preview.

The rules themselves are tested in test_enrolment.py. These check that the screen shows
what the operator needs to act on and cannot write anything without a name and a reason.
"""
import dataclasses

import pytest

pytest.importorskip("qtpy")

from openpyxl import Workbook
from qtpy.QtWidgets import QDialog

from trackify.core import enrolment
from trackify.core.service import ScanService

from .conftest import payload_for

SECRET = "test-secret"


@pytest.fixture
def page(qtbot, conn, section, make_student):
    from trackify.ui.roster import RosterPage

    make_student(first="Juan", last="Dela Cruz")
    make_student(first="Ana", last="Reyes", guardian_mobile=None)
    widget = RosterPage(conn)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def select(page, row: int) -> None:
    page.table.selectRow(row)


def names(page) -> list[str]:
    return [page.table.item(r, 1).text() for r in range(page.table.rowCount())]


# --- the listing ------------------------------------------------------------

def test_the_page_lists_every_student(page):
    assert sorted(names(page)) == ["Dela Cruz, Juan", "Reyes, Ana"]


def test_a_student_with_no_guardian_number_is_marked(page):
    """A blank cell reads as 'nothing to do'. The whole point of importing a student
    with no contact is that somebody then chases the number."""
    row = names(page).index("Reyes, Ana")
    assert "no contact" in page.table.item(row, 5).text()
    assert "with no guardian number" in page.status.text()


def test_the_search_filters_by_name(page):
    page.search.setText("Reyes")
    assert names(page) == ["Reyes, Ana"]


def test_the_search_filters_by_lrn(page, conn):
    lrn = conn.execute("SELECT lrn FROM students WHERE last_name = 'Reyes'").fetchone()[0]
    page.search.setText(lrn)
    assert names(page) == ["Reyes, Ana"]


def test_the_buttons_are_disabled_with_nothing_selected(page):
    page.table.clearSelection()
    assert not page.btn_edit.isEnabled()
    assert not page.btn_active.isEnabled()


def test_selecting_a_student_enables_the_actions(page):
    select(page, 0)
    assert page.btn_edit.isEnabled()
    assert page.btn_active.isEnabled()


def test_the_button_reads_readmit_for_an_inactive_student(page, conn):
    student = conn.execute("SELECT id FROM students LIMIT 1").fetchone()[0]
    enrolment.set_active(conn, student, False, actor_name="T", reason="left")
    page.refresh()
    select(page, 0)

    assert page.btn_active.text() == "Readmit"


def test_an_inactive_student_is_still_listed(page, conn):
    """They have to be visible to be readmitted."""
    student = conn.execute("SELECT id FROM students LIMIT 1").fetchone()[0]
    enrolment.set_active(conn, student, False, actor_name="T", reason="left")
    page.refresh()

    assert len(names(page)) == 2
    assert any("inactive" in page.table.item(r, 5).text()
               for r in range(page.table.rowCount()))


# --- the edit dialog --------------------------------------------------------

def dialog_for(page, conn, last="Dela Cruz"):
    from trackify.ui.roster import StudentDialog
    student = conn.execute(
        """SELECT s.*, sec.name AS section_name, sec.grade_level FROM students s
           JOIN sections sec ON sec.id = s.section_id WHERE s.last_name = ?""",
        (last,),
    ).fetchone()
    return StudentDialog(student, page._sections()), student


def test_the_dialog_shows_the_mobile_in_readable_form(qtbot, page, conn):
    """639171234567 is not a number anyone can check against a form."""
    dialog, _ = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    assert dialog.mobile.text() == "0917 123 4567"


def test_a_typed_local_mobile_is_stored_normalised(qtbot, page, conn):
    dialog, student = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    dialog.mobile.setText("0947 817 9371")

    enrolment.update_student(conn, student["id"], actor_name="T. San Jose",
                             reason="parent changed number", **dialog.values())

    stored = conn.execute("SELECT guardian_mobile FROM students WHERE id = ?",
                          (student["id"],)).fetchone()[0]
    assert stored == "639478179371"


def test_the_dialog_refuses_to_save_without_a_name(qtbot, page, conn):
    dialog, _ = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    dialog.reason.setText("typo")
    dialog._try_accept()

    assert dialog.result() != QDialog.Accepted
    assert "name is required" in dialog.error.text()


def test_the_dialog_refuses_to_save_without_a_reason(qtbot, page, conn):
    dialog, _ = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    dialog.actor.setText("T. San Jose")
    dialog._try_accept()

    assert dialog.result() != QDialog.Accepted
    assert "reason is required" in dialog.error.text()


def test_changing_the_lrn_warns_about_the_printed_card(qtbot, page, conn):
    """Nobody reprints a card they were not told about, and the student is then stuck
    at the gate holding a code that no longer resolves."""
    dialog, _ = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    dialog.show()          # a child's isVisible() is False until the parent is shown
    assert not dialog.lrn_warning.isVisible()

    dialog.lrn.setText("999999999999")
    assert dialog.lrn_warning.isVisible()
    assert "reprint" in dialog.lrn_warning.text().lower()


def test_restoring_the_original_lrn_hides_the_warning(qtbot, page, conn):
    dialog, student = dialog_for(page, conn)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.lrn.setText("999999999999")
    dialog.lrn.setText(student["lrn"])

    assert not dialog.lrn_warning.isVisible()


def test_the_consent_box_reflects_the_stored_value(qtbot, page, conn):
    student = conn.execute("SELECT id FROM students LIMIT 1").fetchone()[0]
    conn.execute("UPDATE students SET consent_on_file = 0 WHERE id = ?", (student,))
    dialog, _ = dialog_for(page, conn)
    qtbot.addWidget(dialog)

    assert not dialog.consent.isChecked()


# --- deactivation -----------------------------------------------------------

def test_the_deactivate_dialog_needs_a_reason_and_a_name(qtbot):
    from trackify.ui.roster import DeactivateDialog
    dialog = DeactivateDialog("Dela Cruz, Juan", True)
    qtbot.addWidget(dialog)
    dialog._try_accept()

    assert dialog.result() != QDialog.Accepted
    assert "required" in dialog.error.text()


def test_the_deactivate_dialog_says_it_is_not_a_deletion(qtbot):
    from trackify.ui.roster import DeactivateDialog
    dialog = DeactivateDialog("Dela Cruz, Juan", True)
    qtbot.addWidget(dialog)

    text = " ".join(w.text() for w in dialog.findChildren(type(dialog.error)))
    assert "not a deletion" in text


def test_deactivating_stops_the_card_at_the_gate(qtbot, page, conn, config):
    from datetime import datetime
    from trackify.core.service import Presentation

    student = conn.execute("SELECT id FROM students LIMIT 1").fetchone()[0]
    cfg = dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, qr_secret=SECRET))
    service = ScanService(conn, cfg)

    assert service.handle_scan(
        payload_for(student), at=datetime(2026, 9, 1, 7, 0)).state is Presentation.IN

    enrolment.set_active(conn, student, False, actor_name="T", reason="left")

    assert service.handle_scan(
        payload_for(student), at=datetime(2026, 9, 1, 7, 30)
    ).state is Presentation.UNKNOWN_CODE


# --- the import preview -----------------------------------------------------

@pytest.fixture
def sheet(tmp_path):
    def _make(rows, title="7-Rizal"):
        book = Workbook()
        ws = book.active
        ws.title = title
        ws.append(("MALE", None, None, None, None))
        ws.append(("LRN:", "NAME OF STUDENT:", "NAME OF PARENT:", "NO. OF PARENT:", None))
        for row in rows:
            ws.append(row)
        path = tmp_path / "roster.xlsx"
        book.save(path)
        return path
    return _make


def preview(page, conn, path):
    from trackify.core import roster
    from trackify.ui.roster import ImportPreviewDialog
    candidates, rejected = roster.parse_workbook(path)
    plan = enrolment.plan_import(conn, candidates, rejected)
    return ImportPreviewDialog(path, plan), plan


def test_the_preview_counts_new_students(qtbot, page, conn, sheet):
    path = sheet([(555555555555, "Nuevo, Nina P.", "Nuevo, Rosa", 9171234567)])
    dialog, plan = preview(page, conn, path)
    qtbot.addWidget(dialog)

    assert plan.counts[enrolment.NEW] == 1


def test_the_preview_writes_nothing_on_its_own(qtbot, page, conn, sheet):
    """Confirming a preview means nothing if the preview already applied itself."""
    before = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    path = sheet([(555555555555, "Nuevo, Nina P.", "Nuevo, Rosa", 9171234567)])
    dialog, _ = preview(page, conn, path)
    qtbot.addWidget(dialog)

    assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == before


def test_the_preview_refuses_to_import_without_a_name(qtbot, page, conn, sheet):
    path = sheet([(555555555555, "Nuevo, Nina P.", "Nuevo, Rosa", 9171234567)])
    dialog, _ = preview(page, conn, path)
    qtbot.addWidget(dialog)
    dialog._try_accept()

    assert dialog.result() != QDialog.Accepted
    assert "name is required" in dialog.error.text()


def test_the_preview_names_the_cards_needing_a_reprint(qtbot, page, conn, sheet):
    """The one consequence of an import that happens off-screen, in a box of cards."""
    student = conn.execute(
        "SELECT * FROM students WHERE last_name = 'Dela Cruz'").fetchone()
    path = sheet([(999999999999, "Dela Cruz, Juan", "Maria", 9171234567)])

    dialog, plan = preview(page, conn, path)
    qtbot.addWidget(dialog)

    assert len(plan.card_reprints) == 1
    text = " ".join(w.text() for w in dialog.findChildren(type(dialog.error)))
    assert "Dela Cruz, Juan" in text
    assert "reprint" in text.lower()


def test_the_import_button_is_dead_when_nothing_would_change(qtbot, page, conn, sheet):
    path = sheet([])
    from trackify.core import roster
    from trackify.ui.roster import ImportPreviewDialog
    candidates, rejected = roster.parse_workbook(path)
    plan = enrolment.plan_import(conn, candidates, rejected)
    dialog = ImportPreviewDialog(path, plan)
    qtbot.addWidget(dialog)

    assert not dialog.import_button.isEnabled()
    assert dialog.import_button.text() == "Nothing to import"


def test_the_preview_says_imported_students_cannot_be_texted(qtbot, page, conn, sheet):
    path = sheet([(555555555555, "Nuevo, Nina P.", "Nuevo, Rosa", 9171234567)])
    dialog, _ = preview(page, conn, path)
    qtbot.addWidget(dialog)

    text = " ".join(w.text() for w in dialog.findChildren(type(dialog.error)))
    assert "cannot be texted" in text


# --- the page inside the records screen -------------------------------------

def test_the_roster_is_reachable_from_the_records_page(qtbot, conn, section,
                                                       make_student):
    from trackify.ui.records import RecordsPage
    make_student()
    records = RecordsPage(conn)
    qtbot.addWidget(records)
    records.show()

    records.btn_roster.click()

    assert records.views.currentIndex() == records.ROSTER
    assert records.title.text() == "Student roster"


def test_the_attendance_controls_hide_on_the_roster(qtbot, conn, section, make_student):
    """Month and year pickers over a roster look like filters and do nothing."""
    from trackify.ui.records import RecordsPage
    make_student()
    records = RecordsPage(conn)
    qtbot.addWidget(records)
    records.show()

    records.btn_roster.click()
    assert not records.month.isVisible()
    assert not records.legend.isVisible()

    records.btn_roster.click()
    assert records.month.isVisible()
    assert records.views.currentIndex() == records.REGISTER


def test_switching_to_the_log_from_the_roster_still_works(qtbot, conn, section,
                                                          make_student):
    from trackify.ui.records import RecordsPage
    make_student()
    records = RecordsPage(conn)
    qtbot.addWidget(records)
    records.show()

    records.btn_roster.click()
    records.btn_log.click()

    assert records.views.currentIndex() == records.LOG
    assert records.btn_roster.text() == "Student roster",         "two buttons both reading 'Back to register'"
    assert records.month.isVisible(), "the register controls came back"
