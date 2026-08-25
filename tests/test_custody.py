"""Chain of custody for hazardous school tools.

These records concern potentially dangerous objects belonging to minors. If an item
goes missing, the audit chain is the school's entire account of what happened to it,
so most of these tests are about the chain being complete rather than about features.
"""
import pytest

from trackify.core import custody
from trackify.core.custody import CustodyError, Status


@pytest.fixture
def held(conn, student):
    return custody.collect(
        conn, student, "utility cutter, yellow handle",
        purpose="for Art, 4th period", storage_ref="TAG-014", collected_by=None,
    )


def _status(conn, custody_id):
    return conn.execute(
        "SELECT status FROM custody_items WHERE id = ?", (custody_id,)
    ).fetchone()[0]


# --- collect ----------------------------------------------------------------

def test_collecting_records_the_tag_and_the_purpose(conn, held):
    row = conn.execute("SELECT * FROM custody_items WHERE id = ?", (held,)).fetchone()
    assert row["status"] == "held"
    assert row["storage_ref"] == "TAG-014"
    assert row["purpose"] == "for Art, 4th period"


def test_collecting_needs_a_description(conn, student):
    with pytest.raises(ValueError, match="item_description"):
        custody.collect(conn, student, "   ")


# --- release ----------------------------------------------------------------

def test_release_backed_by_a_teacher_request_needs_no_reason(conn, held, section, adviser):
    from datetime import datetime
    custody.request_tools(conn, section, datetime.now().date().isoformat(),
                          "Art", "cutters", requested_by=adviser)

    result = custody.release(conn, held, released_to=adviser)

    assert result.backed_by_request
    assert _status(conn, held) == "released"
    assert conn.execute(
        "SELECT released_unbacked FROM custody_items WHERE id = ?", (held,)
    ).fetchone()[0] == 0


def test_release_without_a_request_demands_a_reason(conn, held, adviser):
    """Blocking it outright would push the handover into an unrecorded one in a
    corridor, which is worse. It costs a reason and a flag instead."""
    with pytest.raises(CustodyError, match="reason is required"):
        custody.release(conn, held, released_to=adviser)

    result = custody.release(conn, held, released_to=adviser,
                             reason="Art class moved from Thursday")
    assert not result.backed_by_request
    assert conn.execute(
        "SELECT released_unbacked FROM custody_items WHERE id = ?", (held,)
    ).fetchone()[0] == 1


def test_an_item_cannot_be_released_twice(conn, held, adviser):
    custody.release(conn, held, released_to=adviser, reason="x")
    with pytest.raises(CustodyError, match="not held"):
        custody.release(conn, held, released_to=adviser, reason="x")


# --- return -----------------------------------------------------------------

def test_returning_records_where_it_went(conn, held, adviser):
    custody.release(conn, held, released_to=adviser, reason="x")
    custody.give_back(conn, held, "storage")

    row = conn.execute("SELECT * FROM custody_items WHERE id = ?", (held,)).fetchone()
    assert row["status"] == "returned"
    assert row["returned_to"] == "storage"


def test_storage_and_student_are_different_outcomes(conn, held):
    """An item back in storage is still the school's responsibility; one returned to
    a student is not."""
    custody.give_back(conn, held, "student")
    assert conn.execute(
        "SELECT returned_to FROM custody_items WHERE id = ?", (held,)
    ).fetchone()[0] == "student"


def test_an_invented_destination_is_refused(conn, held):
    with pytest.raises(ValueError, match="storage.*student"):
        custody.give_back(conn, held, "the bin")


def test_a_returned_item_cannot_be_returned_again(conn, held):
    custody.give_back(conn, held, "student")
    with pytest.raises(CustodyError, match="cannot be returned"):
        custody.give_back(conn, held, "storage")


# --- disposal ---------------------------------------------------------------

def test_disposal_always_needs_a_reason(conn, held):
    """The one transition after which the item no longer exists to be accounted for."""
    with pytest.raises(ValueError, match="requires a reason"):
        custody.dispose(conn, held, "")

    custody.dispose(conn, held, "surrendered by the parent")
    assert _status(conn, held) == "disposed"


# --- the chain --------------------------------------------------------------

def test_the_full_chain_has_no_gaps(conn, held, adviser):
    """collect -> release -> return, and every step accounted for."""
    custody.release(conn, held, released_to=adviser, reason="Art moved")
    custody.give_back(conn, held, "storage")

    actions = [r["action"] for r in custody.chain(conn, held)]
    assert actions == ["custody.collected", "custody.released", "custody.returned"]

    released = custody.chain(conn, held)[1]
    assert released["reason"] == "Art moved"
    assert (released["old_value"], released["new_value"]) == ("held", "released")


def test_outstanding_lists_what_is_not_back_yet(conn, held, student, adviser):
    other = custody.collect(conn, student, "scissors", storage_ref="TAG-015")
    custody.release(conn, held, released_to=adviser, reason="x")
    custody.give_back(conn, other, "student")

    ids = [r["id"] for r in custody.outstanding(conn)]
    assert ids == [held]              # the returned one has left the list


def test_held_for_section_flags_a_matching_request(conn, held, section, adviser):
    from datetime import datetime
    rows = custody.held_for_section(conn, section)
    assert [r["has_request"] for r in rows] == [0]

    custody.request_tools(conn, section, datetime.now().date().isoformat(),
                          "Art", "cutters", requested_by=adviser)
    rows = custody.held_for_section(conn, section)
    assert [r["has_request"] for r in rows] == [1]


def test_an_unknown_item_is_a_clear_error(conn):
    with pytest.raises(CustodyError, match="no custody item"):
        custody.release(conn, 9999, released_to=None, reason="x")
