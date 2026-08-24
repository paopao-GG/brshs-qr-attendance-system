"""QR decoding, against real generated codes rather than fixtures.

The codes here are built with the same qrcodes.encode() the printed sheets use, so a
change that breaks the payload format breaks this too.
"""

import numpy as np
import pytest

pytest.importorskip("zxingcpp")
qrcode = pytest.importorskip("qrcode")

from PIL import Image

from trackify.core.qrcodes import encode
from trackify.scan.decoder import available, decode_qr

SECRET = "test-secret"


def frame(payload, *, size=220, at=(500, 250), bg=210):
    """A payload rendered into a 1280x720 grayscale frame, like a camera would see."""
    code = qrcode.make(payload).convert("L").resize((size, size), Image.NEAREST)
    canvas = Image.new("L", (1280, 720), bg)
    canvas.paste(code, at)
    return np.array(canvas)


def test_decoder_is_available():
    assert available()


def test_reads_a_real_payload():
    payload = encode(1, SECRET)
    assert decode_qr(frame(payload)) == payload


def test_reads_every_rotation():
    """A student will not hold the card the right way up."""
    payload = encode(7, SECRET)
    base = frame(payload)
    for turns in (1, 2, 3):
        assert decode_qr(np.rot90(base, turns).copy()) == payload


def test_blank_frame_returns_none():
    assert decode_qr(np.full((720, 1280), 200, np.uint8)) is None


def test_none_input_returns_none():
    assert decode_qr(None) is None


def test_malformed_input_does_not_raise():
    """A bad frame is a bad frame, not a crashed scan station."""
    assert decode_qr(np.zeros((0, 0), np.uint8)) is None
    assert decode_qr("not an image") is None


def test_small_code_still_reads():
    """Roughly what a 25 mm code looks like at arm's length."""
    payload = encode(20, SECRET)
    assert decode_qr(frame(payload, size=90)) == payload


def test_reads_a_light_on_dark_code():
    """A code shown on a phone screen in dark mode."""
    payload = encode(3, SECRET)
    assert decode_qr(255 - frame(payload)) == payload


def test_ignores_a_non_qr_barcode():
    """Only QR is attendance. A barcode on a drinks carton is not a student."""
    import zxingcpp
    code = zxingcpp.create_barcode("TRK-1-3fb640d9", zxingcpp.BarcodeFormat.Code128)
    arr = np.array(zxingcpp.write_barcode_to_image(code, scale=3))
    canvas = np.full((720, 1280), 255, np.uint8)
    h, w = arr.shape[:2]
    canvas[100:100 + h, 100:100 + w] = arr if arr.ndim == 2 else arr[:, :, 0]
    assert decode_qr(canvas) is None
