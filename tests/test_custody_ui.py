"""The custody desk window."""
import pytest

pytest.importorskip("qtpy")

from trackify.core import custody


@pytest.fixture
def desk(qtbot, conn, student, adviser):
    from trackify.ui.custody import CustodyWindow
    custody.collect(conn, student, "utility cutter, yellow handle",
                    purpose="for Art, 4th period", storage_ref="TAG-014")
    window = CustodyWindow(conn)
    qtbot.addWidget(window)
    window.show()
    return window


def test_held_items_are_listed_with_their_tag(desk):
    assert desk.table.rowCount() == 1
    assert desk.table.item(0, 0).text() == "TAG-014"
    assert desk.table.item(0, 4).text() == "held"


def test_a_missing_teacher_request_is_visible_in_the_list(desk, conn, section, adviser):
    from datetime import datetime
    assert desk.table.item(0, 5).text() == "no"

    custody.request_tools(conn, section, datetime.now().date().isoformat(),
                          "Art", "cutters", requested_by=adviser)
    desk.refresh()
    assert desk.table.item(0, 5).text() == "yes"


def test_releasing_without_a_request_needs_a_reason(qtbot, conn, adviser):
    from trackify.ui.custody import ReleaseDialog
    dialog = ReleaseDialog("cutter (TAG-014)", backed=False,
                           advisers=[(adviser, "Tricia San Jose")])
    qtbot.addWidget(dialog)

    dialog.reason.setText("")
    dialog._try_accept()
    assert "needs a reason" in dialog.error.text()

    dialog.reason.setText("Art class moved")
    dialog._try_accept()
    assert dialog.result() != 0


def test_a_backed_release_needs_no_reason(qtbot, conn, adviser):
    from trackify.ui.custody import ReleaseDialog
    dialog = ReleaseDialog("cutter", backed=True,
                           advisers=[(adviser, "Tricia San Jose")])
    qtbot.addWidget(dialog)
    dialog._try_accept()
    assert dialog.error.text() == ""


def test_returning_closes_the_chain_and_drops_it_from_the_list(desk, conn):
    desk.table.setCurrentCell(0, 0)
    desk._give_back("student")

    assert desk.table.rowCount() == 0
    row = conn.execute("SELECT status, returned_to FROM custody_items").fetchone()
    assert (row["status"], row["returned_to"]) == ("returned", "student")


def test_items_still_signed_out_are_called_out(desk, conn, adviser):
    custody.release(conn, desk._rows[0]["id"], released_to=adviser, reason="x")
    desk.refresh()
    assert "still out" in desk.status.text()


def test_acting_with_nothing_selected_says_so(desk):
    desk.table.setCurrentCell(-1, -1)
    desk._release()
    assert "Select an item" in desk.status.text()


def test_the_unverified_adviser_limitation_is_on_the_screen(desk):
    """An audit trail that records an unverified name is better than an unrecorded
    handover, but it is not proof, and the screen should not pretend otherwise."""
    assert "not yet verified" in desk.caveat.text()


def test_a_teacher_request_can_be_filed(qtbot, conn, section, adviser):
    from trackify.ui.custody import HazardRequestDialog
    dialog = HazardRequestDialog(conn)
    qtbot.addWidget(dialog)

    dialog.subject.setText("Art")
    dialog.item_type.setText("cutters")
    dialog._try_accept()

    row = conn.execute("SELECT * FROM hazard_requests").fetchone()
    assert (row["subject"], row["item_type"]) == ("Art", "cutters")


def test_an_incomplete_request_is_refused(qtbot, conn, section):
    from trackify.ui.custody import HazardRequestDialog
    dialog = HazardRequestDialog(conn)
    qtbot.addWidget(dialog)

    dialog.subject.setText("Art")
    dialog.item_type.setText("")
    dialog._try_accept()

    assert "required" in dialog.error.text()
    assert conn.execute("SELECT COUNT(*) FROM hazard_requests").fetchone()[0] == 0
