import pytest

from trackify.core.mobile import InvalidMobile, for_display, normalise


@pytest.mark.parametrize(
    "raw",
    ["09171234567", "+639171234567", "639171234567", "9171234567",
     "0917 123 4567", "0917-123-4567", " 09171234567 "],
)
def test_accepted_forms_all_normalise_the_same(raw):
    assert normalise(raw) == "639171234567"


def test_empty_is_none_not_an_error():
    """A student with no guardian number is attendance-only, not an import failure."""
    assert normalise("") is None
    assert normalise(None) is None
    assert normalise("   ") is None


@pytest.mark.parametrize(
    "raw",
    ["12345", "0812345678", "639171234", "6391712345678", "021234567", "abcdefghijk"],
)
def test_invalid_numbers_raise(raw):
    with pytest.raises(InvalidMobile):
        normalise(raw)


def test_display_format():
    assert for_display("639171234567") == "0917 123 4567"
    assert for_display(None) == ""


@pytest.mark.parametrize("raw", ["N/A", "none", "wala", "n/a", "-", "TBA"])
def test_placeholder_text_is_an_error_not_an_absent_number(raw):
    """A CSV cell saying 'N/A' must fail loudly. Silently treating it as 'no number'
    would exclude the student from notifications with no visible cause."""
    with pytest.raises(InvalidMobile):
        normalise(raw)
