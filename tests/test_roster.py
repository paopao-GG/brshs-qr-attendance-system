"""Roster import rules.

Every case here is built from synthetic rows. The real student-info.xlsx holds 124 real
children's LRNs, parents' names and mobile numbers -- a test suite that opens it would
put that data in every CI log and every developer's terminal scrollback.

The distinction under test throughout: a REJECTION means the record cannot exist, a NOTE
means it exists but something looks wrong. Collapsing the two either loses students to a
typo or seeds a record nobody knows to check.
"""
import pytest

from trackify.core import roster
from trackify.core.roster import Candidate, Rejected

GRADE, SECTION = 11, "Initiative"


def parse(lrn, name, guardian="Cruz, Maria", mobile="9171234567"):
    return roster.parse_row((lrn, name, guardian, mobile, None), GRADE, SECTION)


# --- the one hard requirement ------------------------------------------------

def test_a_row_without_an_lrn_is_rejected():
    result = parse("", "Dela Torre, Deus Jeflor B.")
    assert isinstance(result, Rejected)
    assert "no LRN" in result.reasons


def test_an_lrn_is_the_only_hard_requirement():
    """Guardian details used to reject a row. They no longer do: the roster UI exists so
    staff can fill them in, and refusing the student here would mean the only way to fix
    an empty contact column is Excel -- the thing the UI replaces."""
    result = parse("111995150037", "Arado, Sean Eusef M.", guardian="", mobile="")

    assert isinstance(result, Candidate)
    assert result.guardian_name == ""
    assert result.guardian_mobile is None


def test_a_missing_parent_name_is_noted():
    result = parse("111995150037", "Arado, Sean Eusef M.", guardian="")
    assert any("no parent name" in note for note in result.notes)


def test_a_missing_mobile_says_the_guardian_cannot_be_texted():
    """The consequence, not just the absence -- that is what staff need to act on."""
    result = parse("111995150037", "Osi, Noel B.", mobile="")
    assert any("cannot be texted" in note for note in result.notes)


def test_a_row_with_nothing_at_all_is_rejected_for_the_lrn():
    result = parse("", "Bitara, Zolliel Van S.", guardian="", mobile="")
    assert isinstance(result, Rejected)
    assert set(result.reasons) == {"no LRN"}


def test_this_rule_matches_the_qr_generator():
    """Both tools must agree on who is a student. When they disagreed, the generator
    printed 103 cards against a database holding 73, and 30 of them scanned to
    'Student not found'."""
    with_lrn = parse("432511150038", "Arado, Sean Eusef M.", guardian="", mobile="")
    without = parse("", "Camba, Darius Lamuel C.", guardian="", mobile="")

    assert isinstance(with_lrn, Candidate), "the generator would give this one a card"
    assert isinstance(without, Rejected), "the generator cannot make a code without an LRN"


# --- the LRN is stored as written --------------------------------------------

@pytest.mark.parametrize("lrn", ["49006150157", "111995150037", "1119955150048"])
def test_an_lrn_of_any_length_is_kept_verbatim(lrn):
    """11, 12 and 13 digits all import. No padding, no truncation, no guessing --
    a wrong LRN in a DepEd-shaped record is worse than a flagged one."""
    result = parse(lrn, "Almuena, Yuri Alyssa M.")
    assert isinstance(result, Candidate)
    assert result.lrn == lrn


def test_an_odd_length_lrn_is_noted_not_rejected():
    result = parse("1119955150048", "Almuena, Yuri Alyssa M.")
    assert isinstance(result, Candidate)
    assert any("13 digits" in note for note in result.notes)


def test_a_twelve_digit_lrn_carries_no_note():
    assert parse("111995150037", "Almuena, Jan Adriel M.").notes == ()


# --- the mobile --------------------------------------------------------------

def test_an_unparseable_mobile_imports_with_a_null_number():
    """The parent supplied something, it just needs correcting. Losing the student over
    it would cost them their attendance record as well as their notifications."""
    result = parse("111937150028", "Ricacho, Rhiana Chloe C.", mobile="99430625693")
    assert isinstance(result, Candidate)
    assert result.guardian_mobile is None
    assert any("not a PH mobile number" in note for note in result.notes)


def test_a_ten_digit_mobile_regains_its_country_code():
    """Excel strips the leading zero from every number it stores numerically."""
    assert parse("111995150037", "A, B", mobile="9478179371").guardian_mobile == "639478179371"


@pytest.mark.parametrize("written,stored", [
    ("0969 357 3566", "639693573566"),      # spaced text, as typed by the office
    ("639274402332", "639274402332"),       # already international
    ("09171234567", "639171234567"),
])
def test_the_written_forms_the_office_uses_all_normalise(written, stored):
    assert parse("111995150037", "A, B", mobile=written).guardian_mobile == stored


# --- spreadsheet cells -------------------------------------------------------

def test_excel_floats_become_clean_digits():
    """openpyxl returns 9478179371.0 for anything Excel stored as a number. str() on
    that keeps the '.0' and turns a valid number into a rejected one."""
    assert roster.cell(9478179371.0) == "9478179371"
    assert roster.cell(111995150037.0) == "111995150037"


def test_a_genuine_decimal_is_not_silently_truncated():
    assert roster.cell(1.5) == "1.5"


def test_blank_cells_and_whitespace_collapse_to_empty():
    assert roster.cell(None) == ""
    assert roster.cell("   ") == ""


# --- banners and headers -----------------------------------------------------

@pytest.mark.parametrize("first,second", [
    ("MALE", None), ("FEMALE:", None), (None, "FEMALE"),
    ("LRN:", "NAME OF STUDENT:"),
])
def test_banner_and_header_rows_are_skipped(first, second):
    """These appear mid-sheet, not only at the top, so they cannot be skipped by row
    number -- the banner sits at row 19 in one sheet and row 24 in the others."""
    assert roster.parse_row((first, second, None, None), GRADE, SECTION) is None


def test_a_wholly_empty_row_is_skipped_not_rejected():
    assert roster.parse_row((None, None, None, None), GRADE, SECTION) is None


def test_a_short_row_does_not_raise():
    """Trailing empty cells make openpyxl return rows shorter than five columns."""
    assert roster.parse_row(("111995150037",), GRADE, SECTION).reasons


def test_an_lrn_with_no_name_is_reported_not_silently_dropped():
    result = roster.parse_row(("111995150037", "", "", ""), GRADE, SECTION)
    assert isinstance(result, Rejected)
    assert "no student name" in result.reasons
    assert "111995150037" in result.name


# --- sections and names ------------------------------------------------------

def test_the_sheet_title_becomes_a_section():
    assert roster.parse_section("11-Initiative") == (11, "Initiative")


def test_a_sheet_title_without_a_grade_still_imports():
    assert roster.parse_section("Initiative") == (0, "Initiative")


def test_the_section_label_rebuilds_the_sheet_title():
    """The rest of the application renders a section as f'{grade_level}-{name}'."""
    assert parse("111995150037", "A, B").section_label == "11-Initiative"


def test_a_name_splits_on_the_first_comma_only():
    result = parse("432524150044", "Ellorenco, Marlou Alan Jr. B.")
    assert (result.last, result.first) == ("Ellorenco", "Marlou Alan Jr. B.")


def test_the_middle_initial_stays_with_the_given_name():
    """students has no middle-name column; dropping it would discard recorded data."""
    assert parse("111995150037", "Almuena, Jan Adriel M.").first == "Jan Adriel M."


def test_a_name_with_no_comma_is_noted_not_lost():
    result = parse("111995150037", "Madonna")
    assert isinstance(result, Candidate)
    assert result.last == "Madonna"
    assert any("no comma" in note for note in result.notes)


def test_a_surname_differing_only_in_its_diacritics_is_noted():
    """Real case: the student is typed with n-caron and the parent with n-tilde. Nearly
    identical on screen, different everywhere a computer looks."""
    result = parse("111821150112", "Seňora, Dave D.", guardian="Señora, Dorimar D.")
    assert isinstance(result, Candidate)
    assert any("different characters" in note for note in result.notes)
    assert result.last == "Seňora", "flagged, never rewritten"


def test_an_identical_surname_is_not_noted():
    assert parse("111821150112", "Cruz, Dave", guardian="Cruz, Maria").notes == ()


def test_a_genuinely_different_surname_is_not_noted():
    """Stepparents, remarriage and single mothers make this common and unremarkable."""
    assert parse("111821150112", "Ortile, Jhake R.", guardian="Relona, Jhoanna").notes == ()


def test_a_parent_written_given_name_first_is_not_compared():
    """'Melrose O. Balana' would otherwise compare a given name against a surname."""
    assert parse("432503150030", "Balana, Zuriel Jhon O.",
                 guardian="Melrose O. Balana").notes == ()


def test_a_parent_named_identically_to_the_student_is_noted():
    """A real data-entry slip in the sheet: the parent column repeats the child."""
    result = parse("418602150041", "Loria, Sam Nicole R.", guardian="Loria, Sam Nicole R.")
    assert any("identical" in note for note in result.notes)


def test_a_leading_zero_lrn_is_flagged_as_unscannable():
    """A payload is built by encode(int(lrn)), so a leading zero is lost and the card
    could never resolve back. Caught at import, not as a mystery at the gate."""
    result = parse("0119951500", "Zero, Leading A.")
    assert isinstance(result, Candidate)
    assert result.lrn == "0119951500", "still stored exactly as the sheet has it"
    assert any("never scan" in note for note in result.notes)


def test_a_non_numeric_lrn_says_no_code_can_be_made():
    result = parse("11995150O37", "Oh, Not Zero B.")   # letter O for a zero
    assert any("no QR code" in note for note in result.notes)
