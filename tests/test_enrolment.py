"""Applying a roster to the database.

Built from synthetic candidates, never the real workbook -- that file holds 124 real
children's records.

The failure this module exists to prevent is duplication: the same child in the system
twice, one row carrying their attendance history and the other carrying their working
card. test_a_corrected_lrn_does_not_duplicate_the_student is the one that matters.
"""
import pytest

from trackify.core import enrolment, roster
from trackify.core.enrolment import LRN_CHANGED, NEW, UNCHANGED, UPDATED, EnrolmentError


def candidate(lrn="111995150037", last="Almuena", first="Jan Adriel M.",
              guardian="Almuena, Edith M.", mobile="639478179371",
              grade=11, section="Initiative"):
    return roster.Candidate(
        lrn=lrn, first=first, last=last, section_name=section, grade_level=grade,
        guardian_name=guardian, guardian_mobile=mobile,
    )


@pytest.fixture
def enrolled(conn):
    """One student already in the system, imported the same way the UI would."""
    def _enrol(*candidates, actor="T. San Jose"):
        plan = enrolment.plan_import(conn, candidates or [candidate()])
        enrolment.apply_import(conn, plan, actor_name=actor)
        return plan
    return _enrol


def students(conn):
    return conn.execute(
        "SELECT * FROM students ORDER BY id"
    ).fetchall()


# --- the base cases ---------------------------------------------------------

def test_a_new_lrn_creates_a_student(conn):
    plan = enrolment.plan_import(conn, [candidate()])
    assert plan.counts[NEW] == 1

    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    row = students(conn)[0]
    assert row["lrn"] == "111995150037"
    assert row["first_name"] == "Jan Adriel M."
    assert row["guardian_mobile"] == "639478179371"


def test_the_section_is_created_from_the_sheet_name(conn, enrolled):
    enrolled()
    section = conn.execute("SELECT * FROM sections").fetchone()
    assert (section["grade_level"], section["name"]) == (11, "Initiative")


def test_reimporting_the_same_file_changes_nothing(conn, enrolled):
    enrolled()
    plan = enrolment.plan_import(conn, [candidate()])

    assert plan.counts[UNCHANGED] == 1
    assert plan.writes == 0


def test_a_matching_lrn_updates_in_place(conn, enrolled):
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(mobile="639171234567")])
    assert plan.counts[UPDATED] == 1

    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert len(students(conn)) == 1, "an update must not create a second row"
    assert students(conn)[0]["guardian_mobile"] == "639171234567"


# --- the duplicate guard ----------------------------------------------------

def test_a_corrected_lrn_does_not_duplicate_the_student(conn, enrolled):
    """The adviser fixes a mistyped LRN. Matching on LRN alone would see a stranger and
    insert them, leaving the school holding the same child twice."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(lrn="111995150099")])

    assert plan.counts[LRN_CHANGED] == 1
    assert plan.counts[NEW] == 0

    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    rows = students(conn)
    assert len(rows) == 1
    assert rows[0]["lrn"] == "111995150099"


def test_a_corrected_lrn_is_reported_as_card_breaking(conn, enrolled):
    """Left to work it out, nobody reprints the card and the student is stuck at the
    gate holding a code that no longer resolves."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(lrn="111995150099")])

    reprints = plan.card_reprints
    assert len(reprints) == 1
    assert reprints[0].old_lrn == "111995150037"


def test_the_card_warning_reaches_the_audit_log(conn, enrolled):
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(lrn="111995150099")])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    entry = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'student.updated'").fetchone()
    assert "reprint" in entry["reason"].lower()


def test_a_same_named_student_in_another_section_is_not_matched(conn, enrolled):
    """Two children can share a name. Matching across sections would merge them."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(lrn="999999999999",
                                                 section="Ingenuity")])
    assert plan.counts[NEW] == 1


# --- what an import may not touch -------------------------------------------

def test_import_never_grants_consent(conn, enrolled):
    """queue.py refuses to enqueue without consent, and that guard travels with the
    database. A column in an emailed spreadsheet cannot be allowed to flip it."""
    enrolled()
    assert students(conn)[0]["consent_on_file"] == 0


def test_import_never_revokes_consent_either(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]
    enrolment.update_student(conn, student_id, consent_on_file=1,
                             actor_name="T. San Jose", reason="form signed")

    plan = enrolment.plan_import(conn, [candidate(mobile="639171234567")])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["consent_on_file"] == 1


def test_import_never_reactivates_a_deactivated_student(conn, enrolled):
    """October's decision must survive November's file, which still lists them."""
    enrolled()
    student_id = students(conn)[0]["id"]
    enrolment.set_active(conn, student_id, False,
                         actor_name="T. San Jose", reason="transferred out")

    plan = enrolment.plan_import(conn, [candidate(mobile="639171234567")])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["active"] == 0


def test_a_student_missing_from_the_file_is_untouched(conn, enrolled):
    """An adviser importing one section's list must not wipe the other two."""
    enrolled(candidate(), candidate(lrn="222222222222", last="Reyes", first="Ana"))

    plan = enrolment.plan_import(conn, [candidate()])

    assert [row["last_name"] for row in plan.missing] == ["Reyes"]
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")
    assert len(students(conn)) == 2
    assert all(row["active"] == 1 for row in students(conn))


# --- rejections and reading -------------------------------------------------

def test_a_rejected_row_never_reaches_the_plan_as_a_change(conn):
    rejected = [roster.Rejected(name="Camba, Darius", section_label="11-Innovative",
                                reasons=("no LRN",))]
    plan = enrolment.plan_import(conn, [], rejected)

    assert plan.changes == []
    assert len(plan.rejected) == 1


def test_nothing_is_written_when_the_plan_is_not_applied(conn):
    """The preview has to be genuinely read-only or confirming it means nothing."""
    enrolment.plan_import(conn, [candidate()])
    assert students(conn) == []


# --- auditing ---------------------------------------------------------------

def test_every_import_is_audited_with_the_typed_name(conn, enrolled):
    enrolled(actor="R. Guardia")
    entry = conn.execute("SELECT * FROM audit_log").fetchone()

    assert entry["action"] == "student.imported"
    assert entry["actor_name"] == "R. Guardia"
    assert entry["actor_id"] is None, "a typed name is not an authenticated account"


def test_an_import_requires_a_name(conn):
    plan = enrolment.plan_import(conn, [candidate()])
    with pytest.raises(EnrolmentError, match="requires the name"):
        enrolment.apply_import(conn, plan, actor_name="   ")


# --- single-student edits ---------------------------------------------------

def test_a_mobile_is_normalised_on_the_way_in(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]

    enrolment.update_student(conn, student_id, guardian_mobile="0947 817 9371",
                             actor_name="T. San Jose", reason="parent changed number")

    assert students(conn)[0]["guardian_mobile"] == "639478179371"


def test_a_bad_mobile_is_refused_with_the_reason(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]
    with pytest.raises(EnrolmentError, match="Not a Philippine mobile"):
        enrolment.update_student(conn, student_id, guardian_mobile="99430625693",
                                 actor_name="T", reason="fixing")


def test_an_edit_may_set_consent_unlike_an_import(conn, enrolled):
    """A person ticking a box having seen the signed form is exactly the authority a
    spreadsheet column lacks."""
    enrolled()
    student_id = students(conn)[0]["id"]

    enrolment.update_student(conn, student_id, consent_on_file=1,
                             actor_name="T. San Jose", reason="consent form on file")

    assert students(conn)[0]["consent_on_file"] == 1


def test_an_edit_requires_a_reason_and_a_name(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]
    with pytest.raises(EnrolmentError, match="requires a reason"):
        enrolment.update_student(conn, student_id, first_name="Juan",
                                 actor_name="T", reason="  ")
    with pytest.raises(EnrolmentError, match="requires the name"):
        enrolment.update_student(conn, student_id, first_name="Juan",
                                 actor_name="", reason="typo")


def test_an_edit_cannot_blank_a_name(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]
    with pytest.raises(EnrolmentError, match="cannot be empty"):
        enrolment.update_student(conn, student_id, last_name="   ",
                                 actor_name="T", reason="x")


def test_an_edit_cannot_steal_another_students_lrn(conn, enrolled):
    """lrn is UNIQUE, so this would fail anyway -- but with an IntegrityError nobody
    standing at the kiosk can act on."""
    enrolled(candidate(), candidate(lrn="222222222222", last="Reyes", first="Ana"))
    first, second = students(conn)

    with pytest.raises(EnrolmentError, match="already belongs"):
        enrolment.update_student(conn, second["id"], lrn=first["lrn"],
                                 actor_name="T", reason="fixing")


def test_editing_the_lrn_warns_about_the_card_in_the_audit_row(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]

    enrolment.update_student(conn, student_id, lrn="111995150099",
                             actor_name="T. San Jose", reason="DepEd corrected it")

    entry = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'student.edited'").fetchone()
    assert "reprint" in entry["reason"].lower()


def test_an_unknown_column_is_refused(conn, enrolled):
    """active is deliberately not editable here -- set_active() audits it properly."""
    enrolled()
    student_id = students(conn)[0]["id"]
    with pytest.raises(EnrolmentError, match="Not editable"):
        enrolment.update_student(conn, student_id, active=0,
                                 actor_name="T", reason="x")


# --- deactivation -----------------------------------------------------------

def test_deactivating_is_audited(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]

    enrolment.set_active(conn, student_id, False,
                         actor_name="T. San Jose", reason="transferred to another school")

    assert students(conn)[0]["active"] == 0
    entry = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'student.deactivated'").fetchone()
    assert entry["reason"].startswith("transferred")


def test_deactivating_stops_the_card_at_the_gate(conn, config, enrolled):
    """The whole point: ScanService.student_row() filters active = 1."""
    import dataclasses
    from datetime import datetime
    from trackify.core.qrcodes import encode
    from trackify.core.service import Presentation, ScanService

    enrolled()
    student = students(conn)[0]
    cfg = dataclasses.replace(
        config, secrets=dataclasses.replace(config.secrets, qr_secret="test-secret"))
    service = ScanService(conn, cfg)
    card = encode(int(student["lrn"]), "test-secret")

    assert service.handle_scan(
        card, at=datetime(2026, 9, 1, 7, 0)).state is Presentation.IN

    enrolment.set_active(conn, student["id"], False, actor_name="T", reason="left")

    assert service.handle_scan(
        card, at=datetime(2026, 9, 1, 7, 30)).state is Presentation.UNKNOWN_CODE


def test_readmitting_reverses_it(conn, enrolled):
    enrolled()
    student_id = students(conn)[0]["id"]
    enrolment.set_active(conn, student_id, False, actor_name="T", reason="left")
    enrolment.set_active(conn, student_id, True, actor_name="T", reason="came back")

    assert students(conn)[0]["active"] == 1


# --- the roster listing -----------------------------------------------------

def test_the_listing_can_be_searched_by_name_or_lrn(conn, enrolled):
    enrolled(candidate(), candidate(lrn="222222222222", last="Reyes", first="Ana"))

    assert len(enrolment.roster_rows(conn, search="Reyes")) == 1
    assert len(enrolment.roster_rows(conn, search="111995")) == 1
    assert len(enrolment.roster_rows(conn)) == 2


def test_the_listing_shows_deactivated_students_too(conn, enrolled):
    """They have to be visible to be readmitted."""
    enrolled()
    student_id = students(conn)[0]["id"]
    enrolment.set_active(conn, student_id, False, actor_name="T", reason="left")

    assert len(enrolment.roster_rows(conn)) == 1
    assert len(enrolment.roster_rows(conn, include_inactive=False)) == 0


# --- an import fills blanks, it does not create them -------------------------

def test_a_blank_cell_does_not_erase_a_number_someone_typed_in(conn, enrolled):
    """The office sheet is chronically incomplete -- that is the premise of the roster
    screen. If importing it nulled every guardian number staff had entered, the next
    import would quietly undo an afternoon's work."""
    enrolled()
    student_id = students(conn)[0]["id"]
    enrolment.update_student(conn, student_id, guardian_mobile="09175551234",
                             actor_name="T. San Jose", reason="parent phoned in")

    plan = enrolment.plan_import(conn, [candidate(guardian="", mobile=None)])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["guardian_mobile"] == "639175551234"
    assert students(conn)[0]["guardian_name"] == "Almuena, Edith M."


def test_a_blank_cell_leaves_the_row_unchanged_rather_than_updated(conn, enrolled):
    """It must not even be reported as a change, or every import shows phantom edits."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(guardian="", mobile=None)])

    assert plan.counts[UNCHANGED] == 1
    assert plan.writes == 0


def test_a_filled_cell_still_overwrites_an_old_number(conn, enrolled):
    """Fill-only must not become never-update: a parent who changes number has to be
    correctable from the sheet."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(mobile="639175551234")])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["guardian_mobile"] == "639175551234"


def test_an_import_still_fills_a_blank(conn, enrolled):
    enrolled(candidate(guardian="", mobile=None))
    assert students(conn)[0]["guardian_mobile"] is None

    plan = enrolment.plan_import(conn, [candidate()])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["guardian_mobile"] == "639478179371"


def test_a_name_is_never_treated_as_fill_only(conn, enrolled):
    """Only guardian columns get this protection -- a corrected surname must apply."""
    enrolled()
    plan = enrolment.plan_import(conn, [candidate(first="Jan Adriel")])
    enrolment.apply_import(conn, plan, actor_name="T. San Jose")

    assert students(conn)[0]["first_name"] == "Jan Adriel"
