"""QR decoding over zxing-cpp.

Kept free of Qt so it can be tested with a plain numpy array and no event loop, and so
the choice of decoder stays swappable. zxing-cpp reads numpy arrays, PIL images and
QImages alike, so this module never needs to know which one it was handed.

Why zxing-cpp and not the obvious alternatives:

  opencv-python   ~90 MB, and on Linux it ships its own Qt plugins that hijack
                  QT_QPA_PLATFORM_PLUGIN_PATH and break PySide6 with a "could not load
                  the Qt platform plugin xcb" error -- painful to diagnose on
                  deployment day.
  pyzbar          needs the libzbar0 system library: an apt step on the Pi and a VC++
                  redistributable dependency on Windows.

zxing-cpp is a 1 MB wheel whose aarch64 build needs only glibc >= 2.26 -- a looser
requirement than PySide6 itself, so it can never be the thing that blocks the Pi.
"""

from __future__ import annotations

try:
    import zxingcpp
except ImportError:                                   # degrade, never crash
    zxingcpp = None


def available() -> bool:
    """False when zxing-cpp is not installed.

    The kiosk stays fully usable in that case -- the preview panel says so and the
    keyboard path is untouched. A missing optional decoder must not take the scan
    station down at a school gate.
    """
    return zxingcpp is not None


def decode_qr(image) -> str | None:
    """Return the QR payload in `image`, or None if there isn't a readable one.

    `image` may be a grayscale or BGR numpy array, a PIL image, or a QImage.

    Restricted to QRCode: nothing else in this system is a barcode, and narrowing the
    format both speeds up the search and stops an unrelated barcode -- on a notebook, a
    drinks carton -- from being read as an attendance event.
    """
    if zxingcpp is None or image is None:
        return None

    try:
        result = zxingcpp.read_barcode(
            image, formats=zxingcpp.BarcodeFormat.QRCode
        )
    except Exception:
        # A malformed or zero-sized frame is a bad frame, not a bad program. The next
        # one arrives in 100 ms.
        return None

    if result is None or not result.valid:
        return None
    text = (result.text or "").strip()
    return text or None
