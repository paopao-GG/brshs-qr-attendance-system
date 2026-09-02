"""The scan-station backdrop: media/scan.jpg, drawn as it was designed.

**The image is not processed, and that is deliberate.** An earlier version remapped it
onto a dark band so the kiosk's light-on-dark text would keep its contrast. That is the
wrong way round: the photograph is the design, and the text is what moves. The waiting
page now carries dark ink instead -- see the QWidget#Waiting block in style.qss, whose
colours were measured against the regions of this photo each label actually sits over.
If the backdrop ever looks too light again, the fix is in that block, not here.

Two things this module does still decide:

  * **Fit, not fill.** scan.jpg has a border frame drawn into it, and scaling to cover
    would crop that frame off on any window whose aspect is not the image's 16:9.
  * **The letterbox ground.** Fitting leaves bars, and a dark bar either side of a
    light photograph reads as a broken frame. ground() samples the image's own edges,
    so the bars are the colour the picture already ends on and disappear into it.

Nothing here raises. A missing or unreadable file returns None and the caller falls
back to a flat fill -- a station that opens without its wallpaper is a cosmetic
problem, one that will not open at all is not.

media/*.jpg is a deliberate exception to icons.py's "no image asset files" policy --
see the comment there -- made for this backdrop and the startup screen, not as a
general precedent for icons or UI chrome.
"""

from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QImage, QPixmap

from ..core.config import PROJECT_ROOT

SCAN_IMAGE = PROJECT_ROOT / "media" / "scan.jpg"

# Used when an image cannot be loaded at all. The theme's own near-black, which is what
# the rest of the kiosk is built on; QWidget#Kiosk in style.qss carries the same value.
FALLBACK_GROUND = "#0E1613"

# How deep to sample when averaging an edge. A single row picks up JPEG noise; a wide
# band starts averaging in the picture itself rather than the colour it ends on.
EDGE_BAND = 4

_image: QImage | None = None
_loaded = False
_scaled: QPixmap | None = None
_scaled_for: tuple[int, int] | None = None


def _load() -> QImage | None:
    """The backdrop image, read once. None if it cannot be read."""
    global _image, _loaded
    if _loaded:
        return _image
    _loaded = True

    if not SCAN_IMAGE.is_file():
        return None
    image = QImage(str(SCAN_IMAGE))
    if image.isNull() or not image.width() or not image.height():
        return None
    _image = image
    return _image


def ground(image: QImage | None = None) -> QColor:
    """The colour to fill the letterbox bars with, sampled from an image's edges.

    Defaults to the backdrop's own. Averaging all four edges rather than one keeps a
    single dark corner from setting the colour for every bar.
    """
    image = _load() if image is None else image
    if image is None:
        return QColor(FALLBACK_GROUND)

    width, height = image.width(), image.height()
    band = min(EDGE_BAND, width, height)
    edges = (
        (0, 0, band, height),                     # left
        (width - band, 0, band, height),          # right
        (0, 0, width, band),                      # top
        (0, height - band, width, band),          # bottom
    )

    totals = [0, 0, 0]
    for rect in edges:
        # Scaling a band to a single pixel is Qt averaging it in C++. Doing the same
        # arithmetic in Python would be ~24k pixelColor() calls on a 1920x1080 image,
        # on the path that opens the station in the morning.
        averaged = image.copy(*rect).scaled(
            1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).pixelColor(0, 0)
        totals[0] += averaged.red()
        totals[1] += averaged.green()
        totals[2] += averaged.blue()
    return QColor(*(channel // len(edges) for channel in totals))


def backdrop(width: int, height: int) -> QPixmap | None:
    """The backdrop scaled to fit inside width x height, or None if unavailable.

    The caller centres what it gets and fills the rest with ground().
    """
    global _scaled, _scaled_for
    if width <= 0 or height <= 0:
        return None
    if _scaled_for == (width, height):
        return _scaled

    image = _load()
    if image is None:
        return None

    _scaled = QPixmap.fromImage(image).scaled(
        width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    _scaled_for = (width, height)
    return _scaled
