import pytest

from trackify.notify import gsm7


def test_plain_ascii_is_gsm7():
    assert gsm7.is_gsm7("TRACKIFY: Juan (7-A) arrived 7:12 AM on 2026-09-01.")


@pytest.mark.parametrize("name", ["Pena", "Pe\u00f1a", "Mu\u00f1oz", "MU\u00d1OZ", "Jos\u00e9"])
def test_filipino_surnames_are_safe(name):
    """n-tilde IS in GSM-7. The docs corrected an earlier claim that it wasn't."""
    assert gsm7.is_gsm7(f"TRACKIFY: {name} arrived 7:12 AM.")


@pytest.mark.parametrize(
    "char", ["\u2018", "\u2019", "\u201c", "\u201d", "\u2013", "\u2014", "\u20b1", "\u2026"]
)
def test_word_processor_punctuation_is_caught(char):
    assert not gsm7.is_gsm7(f"TRACKIFY: test {char} message")


def test_peso_sign_rejected_with_actionable_message():
    with pytest.raises(gsm7.NotGSM7, match="peso sign"):
        gsm7.validate("Balance is \u20b1500")


def test_smart_quote_error_names_word():
    with pytest.raises(gsm7.NotGSM7, match="Word"):
        gsm7.validate("Juan\u2019s arrival")


def test_emoji_rejected():
    assert not gsm7.is_gsm7("Arrived \U0001f393")


def test_extension_chars_count_double():
    assert gsm7.septets("[") == 2
    assert gsm7.septets("abc") == 3
    assert gsm7.septets("a[b") == 4


def test_160_gsm7_chars_is_one_segment():
    assert gsm7.segments("a" * 160) == 1
    assert gsm7.segments("a" * 161) == 2


def test_extension_chars_can_push_past_one_segment():
    """80 brackets is 160 septets -- exactly one segment; 81 tips it over."""
    assert gsm7.segments("[" * 80) == 1
    assert gsm7.segments("[" * 81) == 2


def test_validate_rejects_overlong_body():
    with pytest.raises(ValueError, match="would bill as"):
        gsm7.validate("a" * 200)


def test_truncate_fits_one_segment():
    long_body = "TRACKIFY: " + "Juan (7-A) in 7:12AM. " * 20
    short = gsm7.truncate(long_body)
    assert gsm7.septets(short) <= 160
    assert short.endswith(".")
    gsm7.validate(short)  # must now pass


def test_truncate_leaves_short_text_alone():
    body = "TRACKIFY: Juan arrived."
    assert gsm7.truncate(body) == body
