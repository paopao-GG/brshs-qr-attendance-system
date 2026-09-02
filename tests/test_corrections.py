"""Attendance corrections.

The invariant under test throughout: docs/flow.md 4.2 says an original record is never
overwritten. If that fails, the Phase III comparison against manually recorded
attendance is measuring nothing -- "what the system recorded" and "what a human decided
afterwards" would be the same number.
"""
import pytest

from trackify.core import corrections
from trackify.core.attendance import record_scan
from trackify.core.corrections import CorrectionError, CorrectionType

from .conftest import at

DAY = "2026-09-01"


def _live(conn, student, day=DAY):
    return corrections.live_row(conn, student, day)


def _all_rows(conn, student, day=DAY):
    return conn.execute(
        "SELECT * FROM attendance_days WHERE student_id = ? AND date = ? ORDER BY id",
        (student, day),
    ).fetchall()


@pytest.fixture
def present(conn, student, config):
    """A student who scanned in, so there is a real record to correct."""
    record_scan(conn, student, at(7, 0), config)
    return student


# --- the invariant ----------------------------------------------------------

def test_the_original_row_survives_untouched(conn, present):
    original = _live(conn, present)
    assert original["status"] == "present"

    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="medical certificate on file", actor_name="T. San Jose")

    rows = _all_rows(conn, present)
    assert len(rows) == 2
    assert rows[0]["id"] == original["id"]
    assert rows[0]["status"] == "present"          # NOT rewritten
    assert rows[0]["superseded_by"] == rows[1]["id"]


def test_exactly_one_live_row_remains(conn, present):
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T. San Jose")

    live = conn.execute(
        """SELECT COUNT(*) FROM attendance_days
           WHERE student_id = ? AND date = ? AND superseded_by IS NULL""",
        (present, DAY),
    ).fetchone()[0]
    assert live == 1
    assert _live(conn, present)["status"] == "excused"


def test_a_correction_of_a_correction_chains(conn, present):
    """Someone excuses a student, then realises it was the wrong day."""
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T. San Jose")
    corrections.correct(conn, present, DAY, CorrectionType.DATA_ERROR,
                        status="present", reason="wrong date excused",
                        actor_name="R. Guardia")

    rows = _all_rows(conn, present)
    assert [r["status"] for r in rows] == ["present", "excused", "present"]
    assert rows[0]["superseded_by"] == rows[1]["id"]
    assert rows[1]["superseded_by"] == rows[2]["id"]
    assert rows[2]["superseded_by"] is None


def test_the_chain_never_leaves_a_dangling_reference(conn, present):
    corrections.correct(conn, present, DAY, CorrectionType.ONLINE,
                        reason="joined the class online", actor_name="T. San Jose")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- the four types ---------------------------------------------------------

def test_excused_leaves_the_denominator(conn, present, section):
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="funeral", actor_name="T. San Jose")

    _, rows = corrections.register(conn, section, 2026, 9)
    row = rows[0]
    assert row.excused == 1
    assert row.eligible == 0            # excluded, not counted either way
    assert row.rate is None


def test_online_counts_as_present(conn, present, section):
    corrections.correct(conn, present, DAY, CorrectionType.ONLINE,
                        reason="typhoon, joined remotely", actor_name="T. San Jose")

    _, rows = corrections.register(conn, section, 2026, 9)
    assert rows[0].present == 1
    assert rows[0].rate == 1.0


def test_data_error_can_mark_a_student_really_absent(conn, present, section):
    """The case that prompted this: a record that says present but is wrong."""
    corrections.correct(conn, present, DAY, CorrectionType.DATA_ERROR,
                        status="absent", reason="scanned another student's ID",
                        actor_name="R. Guardia")

    assert _live(conn, present)["status"] == "absent"
    _, rows = corrections.register(conn, section, 2026, 9)
    assert rows[0].absent == 1
    assert rows[0].rate == 0.0


def test_data_error_must_say_which_status(conn, present):
    with pytest.raises(CorrectionError, match="must say what the status is"):
        corrections.correct(conn, present, DAY, CorrectionType.DATA_ERROR,
                            reason="wrong", actor_name="R. Guardia")


def test_data_error_refuses_an_invented_status(conn, present):
    with pytest.raises(CorrectionError, match="not a valid status"):
        corrections.correct(conn, present, DAY, CorrectionType.DATA_ERROR,
                            status="maybe", reason="x", actor_name="y")


# --- class suspension -------------------------------------------------------

def test_suspension_excuses_the_whole_section(conn, make_student, section):
    a, b = make_student(), make_student(first="Ana", last="Reyes")

    ids = corrections.suspend_section(conn, section, DAY,
                                      reason="Typhoon signal no. 2",
                                      actor_name="School Head")

    assert len(ids) == 2
    for student in (a, b):
        row = _live(conn, student)
        assert row["status"] == "excused"
        assert "class_suspension" in row["flags"]


def test_suspension_does_not_touch_another_section(conn, make_student, section):
    mine = make_student()
    other_section = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Mabini', 9)"
    ).lastrowid
    theirs = conn.execute(
        """INSERT INTO students (lrn, first_name, last_name, section_id,
               guardian_mobile, consent_on_file, created_at)
           VALUES ('999', 'Not', 'Mine', ?, '639171234567', 1, '2026-01-01')""",
        (other_section,),
    ).lastrowid

    corrections.suspend_section(conn, section, DAY, reason="suspension",
                                actor_name="School Head")

    assert _live(conn, mine) is not None
    assert _live(conn, theirs) is None


def test_suspending_an_empty_section_is_an_error_not_a_silent_noop(conn):
    empty = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Empty', 7)"
    ).lastrowid
    with pytest.raises(CorrectionError, match="no active students"):
        corrections.suspend_section(conn, empty, DAY, reason="x", actor_name="y")


# --- what a correction must carry -------------------------------------------

def test_a_reason_is_mandatory(conn, present):
    """A correction with no reason is indistinguishable from tampering."""
    with pytest.raises(CorrectionError, match="requires a reason"):
        corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                            reason="   ", actor_name="T. San Jose")


def test_a_name_is_mandatory(conn, present):
    with pytest.raises(CorrectionError, match="requires the name"):
        corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                            reason="sick", actor_name="")


def test_the_typed_name_never_lands_in_corrected_by(conn, present):
    """corrected_by is a foreign key to a verified account. A typed name is a claim,
    and storing it there would make an unverified claim look authenticated."""
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T. San Jose")

    row = _live(conn, present)
    assert row["corrected_by"] is None
    assert row["corrected_by_name"] == "T. San Jose"


def test_a_correction_for_a_day_with_no_record_creates_one(conn, student):
    """A student who never scanned and whose day was never closed -- which is exactly
    when someone turns up with an excuse slip."""
    corrections.correct(conn, student, DAY, CorrectionType.EXCUSED,
                        reason="hospital", actor_name="T. San Jose")

    row = _live(conn, student)
    assert row["status"] == "excused"
    assert row["entry_scan_id"] is None


# --- the edit log -----------------------------------------------------------

def test_every_correction_is_audited_with_who_and_why(conn, present):
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="medical certificate", actor_name="T. San Jose")

    entry = corrections.edit_log(conn)[0]
    assert entry["action"] == "attendance.corrected"
    assert entry["actor_name"] == "T. San Jose"
    assert entry["reason"] == "medical certificate"
    assert "present" in entry["old_value"]
    assert "excused" in entry["new_value"]


def test_the_edit_log_can_be_filtered_by_section(conn, present, section, make_student):
    other_section = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Mabini', 9)"
    ).lastrowid
    outsider = conn.execute(
        """INSERT INTO students (lrn, first_name, last_name, section_id,
               guardian_mobile, consent_on_file, created_at)
           VALUES ('998', 'Out', 'Sider', ?, '639171234567', 1, '2026-01-01')""",
        (other_section,),
    ).lastrowid

    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="mine", actor_name="A")
    corrections.correct(conn, outsider, DAY, CorrectionType.EXCUSED,
                        reason="theirs", actor_name="B")

    reasons = [e["reason"] for e in corrections.edit_log(conn, section_id=section)]
    assert reasons == ["mine"]


# --- the register -----------------------------------------------------------

def test_the_register_shows_letters_and_marks_corrections(conn, present, section):
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T. San Jose")

    days, rows = corrections.register(conn, section, 2026, 9)
    assert len(days) == 30
    cell = rows[0].cells[DAY]
    assert cell.letter == "E"
    assert cell.corrected is True


def test_a_scanned_day_is_not_marked_as_corrected(conn, present, section):
    _, rows = corrections.register(conn, section, 2026, 9)
    assert rows[0].cells[DAY].corrected is False


def test_a_month_with_no_records_has_an_undefined_rate_not_zero(conn, student, section):
    """0% reads as catastrophic attendance. No data is not the same thing."""
    _, rows = corrections.register(conn, section, 2026, 9)
    assert rows[0].rate is None


def test_the_register_ignores_superseded_rows(conn, present, section):
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T")
    _, rows = corrections.register(conn, section, 2026, 9)

    # One cell for the day, not two, despite two rows existing in the table.
    assert rows[0].cells[DAY].letter == "E"
    assert rows[0].present == 0


# --- all students, section_id=None -------------------------------------------

def test_register_with_no_section_covers_every_section(conn, present, section,
                                                        make_student):
    """section_id=None is "All students" in the Records page: every live section's
    roster, not an error and not an empty one."""
    other = conn.execute(
        "INSERT INTO sections (name, grade_level) VALUES ('Bonifacio', 8)"
    ).lastrowid
    conn.execute(
        """INSERT INTO students
           (lrn, first_name, last_name, section_id, guardian_name,
            guardian_mobile, consent_on_file, created_at)
           VALUES ('136584129999', 'Ana', 'Reyes', ?, 'Maria', '639171234567', 1, ?)""",
        (other, "2026-01-01T00:00:00"),
    )

    _, rows = corrections.register(conn, None, 2026, 9)

    names = {r.name for r in rows}
    assert "Dela Cruz, Juan" in names           # from the `present` fixture's section
    assert "Reyes, Ana" in names                # from the second section


def test_register_with_no_section_labels_each_row_by_its_section(conn, present, section):
    """r.section is blank in the single-section call -- it only exists to disambiguate
    an All-students grid, where "Dela Cruz, Juan" alone would not say which one."""
    _, single = corrections.register(conn, section, 2026, 9)
    assert single[0].section == ""

    _, every = corrections.register(conn, None, 2026, 9)
    assert every[0].section == "7-Rizal"


def test_a_plain_string_type_is_accepted(conn, present):
    """CorrectionType subclasses str, and anything round-tripped through a Qt variant
    -- a combo box's userData, for one -- comes back as a bare str. An identity check
    against the enum then silently fails and every correction gets the wrong type."""
    corrections.correct(conn, present, DAY, "excused_absence",
                        reason="sick", actor_name="T. San Jose")

    row = _live(conn, present)
    assert row["status"] == "excused"
    assert row["correction_type"] == "excused_absence"


def test_a_plain_string_data_error_still_validates(conn, present):
    with pytest.raises(CorrectionError, match="must say what the status is"):
        corrections.correct(conn, present, DAY, "data_error",
                            reason="wrong", actor_name="R")


def test_an_invented_type_is_refused(conn, present):
    with pytest.raises(ValueError):
        corrections.correct(conn, present, DAY, "made_up",
                            reason="x", actor_name="y")


def test_the_audit_row_names_the_student_not_an_id(conn, present):
    """An audit entry has to be readable on its own years later; 'student 13' means
    nothing to anyone reading a printed log."""
    corrections.correct(conn, present, DAY, CorrectionType.EXCUSED,
                        reason="sick", actor_name="T. San Jose")

    entry = corrections.edit_log(conn)[0]
    assert "Dela Cruz, Juan" in entry["old_value"]
    assert "student " not in entry["old_value"]


# --- absence runs, shared by the SF2 five-day rule and the risk model --------

@pytest.mark.parametrize("statuses,longest,trailing", [
    (["absent", "absent", "absent"], 3, 3),
    (["absent", "absent", "present"], 2, 0),
    (["present", "present"], 0, 0),
    ([], 0, 0),
    (["absent", "", "absent"], 1, 1),          # no record is not evidence of absence
])
def test_absence_runs(statuses, longest, trailing):
    assert corrections.longest_absence_run(statuses) == longest
    assert corrections.trailing_absence_run(statuses) == trailing


def test_a_suspended_day_is_transparent_to_a_run():
    """A per-section suspension writes 'excused' on a date that is still a column. It
    used to reset the run to zero, so a child who missed a fortnight around one
    suspended Wednesday scored 2 instead of 4 -- in the risk model AND on the form."""
    week = ["absent", "absent", "excused", "absent", "absent"]

    assert corrections.longest_absence_run(week) == 4
    assert corrections.trailing_absence_run(week) == 4


def test_a_day_the_student_attended_still_breaks_a_run():
    """Transparency is for days nobody could attend, not for days they did."""
    assert corrections.longest_absence_run(
        ["absent", "absent", "online", "absent"]) == 2


def test_the_form_and_the_model_agree_on_the_same_week():
    """One rule, two callers. They disagreed before and one of them fed a paper."""
    from trackify.analytics import risk  # noqa: F401 - import path check
    week = ["absent", "excused", "absent", "absent", "absent"]

    assert corrections.longest_absence_run(week) == 4
    assert corrections.trailing_absence_run(week) == 4
