"""The scan-station backdrop.

Two properties, and they pull against the history of this module. The photo must be
drawn EXACTLY as designed -- an earlier version remapped it onto a dark band to protect
the light-on-dark text, and the fix for that contrast now lives in style.qss's
QWidget#Waiting block instead. And the letterbox bars must take the photo's own edge
colour, because a dark bar either side of a pale photograph reads as a broken frame.
"""

import pytest

pytest.importorskip("qtpy")

from qtpy.QtGui import QImage

from trackify.ui import backdrop


@pytest.fixture(autouse=True)
def _fresh_cache():
    """backdrop caches at module level; reset around each test so ordering cannot make
    one test depend on another's scaling."""
    backdrop._image = None
    backdrop._loaded = False
    backdrop._scaled = None
    backdrop._scaled_for = None
    yield


def test_the_photo_is_returned_untouched(qapp):
    """The regression guard. If anyone reintroduces a remap to "fix" the contrast,
    these pixels move and this fails -- the contrast fix belongs in style.qss."""
    loaded = backdrop._load()
    original = QImage(str(backdrop.SCAN_IMAGE))

    assert loaded is not None, "media/scan.jpg ships with the repo"
    assert (loaded.width(), loaded.height()) == (original.width(), original.height())
    for x, y in ((0, 0), (960, 540), (1919, 1079), (400, 900)):
        assert loaded.pixelColor(x, y) == original.pixelColor(x, y)


def test_the_backdrop_is_light(qapp):
    """It is a pale photograph, which is why the waiting page carries dark ink. If this
    ever fails the ink in style.qss has to be revisited with it."""
    image = backdrop._load()
    total, samples = 0, 0
    for x in range(0, image.width(), 97):
        for y in range(0, image.height(), 97):
            colour = image.pixelColor(x, y)
            total += 0.2126 * colour.red() + 0.7152 * colour.green() + 0.0722 * colour.blue()
            samples += 1

    assert total / samples > 150, "a dark backdrop would make the dark ink unreadable"


def test_it_fits_rather_than_fills(qapp):
    """scan.jpg has a border frame drawn into it; cropping to cover would cut it off."""
    pixmap = backdrop.backdrop(1000, 1000)

    assert pixmap is not None
    assert pixmap.width() <= 1000 and pixmap.height() <= 1000
    # 16:9 fitted into a square is limited by width, so it keeps the full width.
    assert pixmap.width() == 1000
    assert abs(pixmap.height() - 1000 * 1080 / 1920) <= 1


def test_the_ground_comes_from_the_images_own_edges(qapp):
    """So the bars disappear into the picture instead of framing it."""
    image = backdrop._load()
    sampled = backdrop.ground()

    # Recomputed independently from the left edge: the average of all four edges has to
    # land near it, and nowhere near the dark fallback.
    left = image.copy(0, 0, backdrop.EDGE_BAND, image.height())
    reference = left.scaled(1, 1).pixelColor(0, 0)

    assert abs(sampled.red() - reference.red()) < 40
    assert abs(sampled.green() - reference.green()) < 40
    assert sampled.green() > 100, "scan.jpg's edges are pale, not the dark fallback"


def test_the_ground_can_be_asked_about_another_image(qapp):
    """splash.py uses this for startup.jpg, which is a different picture entirely and
    needs its own bar colour."""
    from trackify.ui.splash import STARTUP_IMAGE

    startup = backdrop.ground(QImage(str(STARTUP_IMAGE)))
    scan = backdrop.ground()

    assert startup != scan, "two different images should not share a bar colour"


def test_a_degenerate_size_is_not_a_crash(qapp):
    assert backdrop.backdrop(0, 0) is None
    assert backdrop.backdrop(-5, 100) is None


def test_a_missing_file_falls_back_instead_of_raising(qapp, monkeypatch, tmp_path):
    """A station that opens without its wallpaper is a cosmetic problem; one that will
    not open at all is not."""
    monkeypatch.setattr(backdrop, "SCAN_IMAGE", tmp_path / "not-here.jpg")

    assert backdrop._load() is None
    assert backdrop.backdrop(800, 600) is None
    assert backdrop.ground().name() == backdrop.FALLBACK_GROUND.lower()


def test_the_fallback_ground_matches_the_stylesheet():
    """QWidget#Kiosk is the ground painted when the image cannot be loaded at all; if
    the two drift apart that failure looks like a rendering bug instead of a missing
    file."""
    from pathlib import Path
    qss = (Path(__file__).resolve().parents[1]
           / "trackify" / "ui" / "style.qss").read_text(encoding="utf8")

    assert f"QWidget#Kiosk {{ background: {backdrop.FALLBACK_GROUND}; }}" in qss
