import pytest

from trackify.core.qrcodes import InvalidQRCode, decode, encode, is_wellformed

SECRET = "test-secret-do-not-use-in-production"
OTHER = "a-different-secret"


def test_roundtrip():
    for student_id in (1, 42, 999999):
        assert decode(encode(student_id, SECRET), SECRET) == student_id


def test_forged_sequential_id_is_rejected():
    """The attack the signature exists to stop: take a real code, change the id."""
    real = encode(100, SECRET)
    forged = real.replace("-100-", "-101-")
    with pytest.raises(InvalidQRCode):
        decode(forged, SECRET)


def test_invented_payload_is_rejected():
    with pytest.raises(InvalidQRCode):
        decode("TRK-101-deadbeef", SECRET)


def test_code_from_another_deployment_is_rejected():
    with pytest.raises(InvalidQRCode):
        decode(encode(7, OTHER), SECRET)


@pytest.mark.parametrize(
    "payload",
    ["", "   ", "12345", "TRK-abc-12345678", "TRK-1-SHORT", "TRK-1-XYZ12345",
     "trk-1-00000000", "TRK-1-1234567890", None],
)
def test_malformed_payloads_rejected(payload):
    with pytest.raises(InvalidQRCode):
        decode(payload, SECRET)


def test_surrounding_whitespace_tolerated():
    """HID scanners can emit stray whitespace around the payload."""
    code = encode(5, SECRET)
    assert decode(f"  {code}\r\n", SECRET) == 5


def test_wellformed_separates_misfire_from_bad_code():
    assert is_wellformed(encode(3, SECRET))
    assert is_wellformed("TRK-3-00000000")   # shaped right, signature wrong
    assert not is_wellformed("garbage")      # scanner misfire


def test_missing_secret_is_a_clear_error():
    with pytest.raises(ValueError, match="TRACKIFY_QR_SECRET"):
        encode(1, "")


def test_rejects_nonpositive_id():
    with pytest.raises(ValueError):
        encode(0, SECRET)
