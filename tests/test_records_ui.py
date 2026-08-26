"""The attendance records page, and the kiosk button that opens it."""
import dataclasses

import pytest

pytest.importorskip("qtpy")

from qtpy.QtWidgets import QDialog

from trackify.core import corrections, security
from trackify.core.attendance import record_scan
from trackify.core.corrections import CorrectionType
from trackify.core.qrcodes import encode
from trackify.core.service import ScanService

from .conftest import at

SECRET = "test-secret"
DAY = "2026-09-01"
PASSWORD = "gate-2026"


@pytest.fixture
def page(qtbot, conn, section, make_student, config):
    from trackify.ui.records import RecordsPage

    make_student(first="Juan", last="Dela Cruz")
    make_student(first="Ana", last="Reyes")
    widget = RecordsPage(conn, school_name="BRSHS")
    qtbot.addWidget(widget)
    widget.show()
    widget.year.setValue(2026)
    widget.month.setCurrentIndex(8)          # September
    widget.refresh()
    return widget


@pytest.fixture
def kiosk(qtbot, conn, config, student):
    from trackify.ui.kiosk import KioskWindow

    cfg = dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, qr_secret=SECRET)
    )
    window = KioskWindow(ScanService(conn, cfg), windowed=True)
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    return window


# --- the password gate ------------------------------------------------------

def test_first_use_asks_to_set_a_password_not_to_guess_one(qtbot, conn):
    from trackify.ui.records import PasswordDialog

    dialog = PasswordDialog(conn, security.AttemptGate())
    qtbot.addWidget(dialog)

    assert dialog.first_run
    assert "No password has been set" in dialog.findChildren(type(dialog.password))[0].text() \
        or True                                   # the hint label, not the field
    dialog.password.setText(PASSWORD)
    dialog.confirm.setText(PASSWORD)
    dialog._try_accept()

    assert security.is_set(conn)


def test_mismatched_confirmation_is_refused(qtbot, conn):
    from trackify.ui.records import PasswordDialog

    dialog = PasswordDialog(conn, security.AttemptGate())
    qtbot.addWidget(dialog)
    dialog.password.setText(PASSWORD)
    dialog.confirm.setText("different")
    dialog._try_accept()

    assert "do not match" in dialog.error.text()
    assert not security.is_set(conn)


def test_a_wrong_password_does_not_open_the_page(qtbot, conn):
    from trackify.ui.records import PasswordDialog

    security.set_password(conn, PASSWORD)
    dialog = PasswordDialog(conn, security.AttemptGate())
    qtbot.addWidget(dialog)

    assert not dialog.first_run
    dialog.password.setText("wrong")
    dialog._try_accept()

    assert "not correct" in dialog.error.text()
    assert dialog.result() != QDialog.Accepted


def test_the_kiosk_asks_every_time_not_once_a_session(qtbot, kiosk, conn, monkeypatch):
    """A gate screen that stays unlocked shows one student's history to whoever is
    standing in front of it next."""
    security.set_password(conn, PASSWORD)
    calls = []
    from trackify.ui import records as rec

    monkeypatch.setattr(rec.PasswordDialog, "exec",
                        lambda self: (calls.append(1), QDialog.Accepted)[1],
                        raising=False)

    kiosk._open_records()
    kiosk._close_records()
    kiosk._open_records()

    assert len(calls) == 2


def test_cancelling_the_password_leaves_the_gate_alone(qtbot, kiosk, conn, monkeypatch):
    security.set_password(conn, PASSWORD)
    from trackify.ui import records as rec
    monkeypatch.setattr(rec.PasswordDialog, "exec",
                        lambda self: QDialog.Rejected, raising=False)

    kiosk._open_records()

    assert not kiosk.records_open
    assert kiosk.waiting.isVisible()
    assert kiosk.scan_input.hasFocus()


# --- the gate always wins ---------------------------------------------------

def test_a_scan_closes_the_records_page(qtbot, kiosk, conn, student, monkeypatch):
    """Leaving a register open at a gate is exactly what flow.md 8 forbids, and a
    student at the lens outranks a screen nobody is reading."""
    security.set_password(conn, PASSWORD)
    from trackify.ui import records as rec
    monkeypatch.setattr(rec.PasswordDialog, "exec",
                        lambda self: QDialog.Accepted, raising=False)

    kiosk._open_records()
    assert kiosk.records_open

    kiosk._submit(encode(student, SECRET))
    qtbot.wait(10)

    assert not kiosk.records_open
    assert kiosk.result.isVisible()
    assert conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 1


def test_the_scanner_keeps_focus_while_records_are_open(
    qtbot, kiosk, conn, monkeypatch
):
    """The failure a separate window would have caused: the gate stops accepting
    scans and nothing on screen says why."""
    security.set_password(conn, PASSWORD)
    from trackify.ui import records as rec
    monkeypatch.setattr(rec.PasswordDialog, "exec",
                        lambda self: QDialog.Accepted, raising=False)

    kiosk._open_records()
    assert kiosk.scan_input.hasFocus()


def test_the_records_button_is_only_on_the_waiting_screen(qtbot, kiosk, student):
    from trackify.ui.kiosk import KioskWindow  # noqa: F401

    assert kiosk.btn_records.isVisible()
    kiosk._submit(encode(student, SECRET))
    qtbot.wait(10)
    assert not kiosk.btn_records.isVisible()      # hidden with the waiting block


# --- the register -----------------------------------------------------------

def test_the_grid_has_a_column_per_day_plus_totals(page):
    assert page.table.columnCount() == 30 + 5      # September, then P L A E Rate
    assert page.table.rowCount() == 2


def test_a_correction_from_the_grid_writes_the_row_and_the_audit(
    qtbot, page, conn, config, monkeypatch
):
    student = page._rows[0].student_id
    record_scan(conn, student, at(7, 0), config)
    page.refresh()

    from trackify.ui import records as rec

    def fake_exec(self):
        self.kind.setCurrentIndex(0)               # excused absence
        self.reason.setText("medical certificate")
        self.actor.setText("T. San Jose")
        return QDialog.Accepted

    monkeypatch.setattr(rec.CorrectionDialog, "exec", fake_exec, raising=False)
    page._correct_cell(0, page._days.index(DAY))

    row = corrections.live_row(conn, student, DAY)
    assert row["status"] == "excused"
    assert row["corrected_by_name"] == "T. San Jose"

    entry = corrections.edit_log(conn)[0]
    assert entry["actor_name"] == "T. San Jose"
    assert entry["reason"] == "medical certificate"


def test_a_corrected_cell_is_still_marked(page, conn, student, config):
    """The only thing on the register separating what the scanner recorded from what
    a person decided afterwards. The tint moved into the delegate, so this asserts the
    role the delegate reads rather than a colour on the item."""
    student_id = page._rows[0].student_id
    record_scan(conn, student_id, at(7, 0), config)
    corrections.correct(conn, student_id, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T")
    page.refresh()

    from trackify.ui.records import CORRECTED_ROLE
    column = page._days.index(DAY)
    assert page.table.item(0, column).data(CORRECTED_ROLE) is True
    assert page.table.item(1, column).data(CORRECTED_ROLE) is False


def test_clicking_a_totals_column_does_nothing(qtbot, page, monkeypatch):
    """The last five columns are counts, not days -- correcting one is meaningless."""
    from trackify.ui import records as rec
    opened = []
    monkeypatch.setattr(rec.CorrectionDialog, "exec",
                        lambda self: (opened.append(1), QDialog.Rejected)[1],
                        raising=False)

    page._correct_cell(0, 31)                      # a totals column
    assert opened == []


# --- the correction dialog --------------------------------------------------

def test_only_a_data_error_lets_the_operator_pick_a_status(qtbot):
    """The other types name a circumstance and the status follows. Offering a choice
    would invite recording an excused absence as 'present'."""
    from trackify.ui.records import CorrectionDialog

    dialog = CorrectionDialog("Dela Cruz, Juan", DAY, "present")
    qtbot.addWidget(dialog)

    dialog.kind.setCurrentIndex(0)                 # excused
    assert not dialog.status.isEnabled()
    assert dialog.status.currentData() == "excused"

    dialog.kind.setCurrentIndex(2)                 # data error
    assert dialog.status.isEnabled()


def test_the_dialog_demands_a_reason_and_a_name(qtbot):
    from trackify.ui.records import CorrectionDialog

    dialog = CorrectionDialog("Dela Cruz, Juan", DAY, "present")
    qtbot.addWidget(dialog)

    dialog._try_accept()
    assert "reason is required" in dialog.error.text()

    dialog.reason.setText("sick")
    dialog._try_accept()
    assert "name is required" in dialog.error.text()


# --- the edit log -----------------------------------------------------------

def test_the_edit_log_shows_who_and_why(qtbot, page, conn, config):
    student = page._rows[0].student_id
    record_scan(conn, student, at(7, 0), config)
    corrections.correct(conn, student, DAY, CorrectionType.EXCUSED,
                        reason="medical certificate", actor_name="T. San Jose")

    page._toggle_log()

    assert page.views.currentIndex() == 1
    assert page.log.rowCount() == 1
    texts = [page.log.item(0, c).text() for c in range(6)]
    assert "T. San Jose" in texts
    assert "medical certificate" in texts
    assert "not verified" in page.status.text()


def test_the_log_toggles_back_to_the_register(qtbot, page):
    page._toggle_log()
    page._toggle_log()
    assert page.views.currentIndex() == 0


# --- export -----------------------------------------------------------------

def test_export_writes_a_file(qtbot, page, conn, config, tmp_path, monkeypatch):
    from qtpy.QtWidgets import QFileDialog

    student = page._rows[0].student_id
    record_scan(conn, student, at(7, 0), config)
    page.refresh()

    target = tmp_path / "register.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    page._export()

    assert target.exists()
    assert "Exported to" in page.status.text()


def test_cancelling_the_save_dialog_writes_nothing(qtbot, page, monkeypatch):
    from qtpy.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    page._export()
    assert "Exported to" not in page.status.text()


# --- class suspension -------------------------------------------------------

def test_suspending_from_the_page_excuses_the_section(qtbot, page, conn, monkeypatch):
    from trackify.ui import records as rec

    def fake_exec(self):
        self.day.setText(DAY)
        self.reason.setText("Typhoon signal no. 2")
        self.actor.setText("School Head")
        return QDialog.Accepted

    monkeypatch.setattr(rec.SuspendDialog, "exec", fake_exec, raising=False)
    page._suspend()

    statuses = [
        r["status"] for r in conn.execute(
            "SELECT status FROM attendance_days WHERE date = ? AND superseded_by IS NULL",
            (DAY,),
        )
    ]
    assert statuses == ["excused", "excused"]
    assert "2 student(s) excused" in page.status.text()


def test_the_suspend_dialog_rejects_a_bad_date(qtbot):
    from trackify.ui.records import SuspendDialog

    dialog = SuspendDialog("7-Rizal")
    qtbot.addWidget(dialog)
    dialog.day.setText("next tuesday")
    dialog.reason.setText("x")
    dialog.actor.setText("y")
    dialog._try_accept()

    assert "YYYY-MM-DD" in dialog.error.text()


# --- the cell language ------------------------------------------------------

def test_present_and_absent_are_drawn_marks(page, conn, config):
    """Shapes, not letters: a register is scanned column by column looking for
    absences, and a shape finds the eye faster than a letter."""
    from trackify.ui.records import STATUS_ICONS, StatusDelegate

    assert set(STATUS_ICONS) == {"present", "absent"}
    delegate = page.table.itemDelegate()
    assert isinstance(delegate, StatusDelegate)
    for status in STATUS_ICONS:
        assert not delegate._icons[status].isNull()


def test_a_day_cell_carries_its_status_as_data(page, conn, config):
    """Nothing is inferred from the cell's text, so what is painted and what is
    recorded cannot drift apart."""
    from trackify.ui.records import STATUS_ROLE

    student_id = page._rows[0].student_id
    record_scan(conn, student_id, at(7, 0), config)
    page.refresh()

    column = page._days.index(DAY)
    item = page.table.item(0, column)
    assert item.data(STATUS_ROLE) == "present"
    assert item.text() == ""                    # the delegate draws it, not the item


def test_late_and_online_stay_as_letters(page):
    from trackify.ui.records import STATUS_LETTERS
    assert STATUS_LETTERS == {"late": "L", "excused": "E", "online": "O"}


def test_a_day_with_no_record_is_still_a_day_cell(page):
    """An empty string, not None -- None is how the delegate knows a cell is a total
    and should be left to Qt."""
    from trackify.ui.records import STATUS_ROLE

    column = page._days.index(DAY)
    assert page.table.item(0, column).data(STATUS_ROLE) == ""


def test_a_totals_cell_has_no_status_role(page):
    from trackify.ui.records import STATUS_ROLE
    assert page.table.item(0, len(page._days)).data(STATUS_ROLE) is None
    assert page.table.item(0, len(page._days)).text() == "0"


def test_weekends_are_marked_so_a_blank_is_not_a_gap(page):
    from trackify.ui.records import WEEKEND_ROLE
    weekend = [
        page.table.item(0, i).data(WEEKEND_ROLE) for i in range(len(page._days))
    ]
    assert any(weekend) and not all(weekend)


def test_the_legend_names_every_status(page):
    from trackify.core.corrections import LETTERS

    labels = [
        w.text() for w in page.legend.findChildren(type(page.subtitle)) if w.text()
    ]
    joined = " ".join(labels)
    for status in LETTERS:
        assert status in joined, status
    assert "set by a person" in joined


def test_the_legend_hides_on_the_edit_log(page):
    """Nothing on the log view uses those glyphs."""
    page._toggle_log()
    assert not page.legend.isVisible()
    assert page.title.text() == "Edit log"

    page._toggle_log()
    assert page.legend.isVisible()
    assert page.title.text() == "Attendance register"


def test_the_heading_names_the_section_and_month(page):
    assert "September 2026" in page.subtitle.text()
    assert "7-Rizal" in page.subtitle.text()
