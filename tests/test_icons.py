"""Icons drawn in code.

The failure these guard against is quiet: a malformed SVG path renders as a blank
square rather than raising, so a card just looks empty and nobody knows why.
"""
import pytest

pytest.importorskip("qtpy")

from trackify.core import screening
from trackify.ui import icons


def test_every_prohibited_category_has_an_icon():
    for cat in screening.CATEGORIES:
        if cat.code == "tool":          # shown as School tool, which has its own
            continue
        assert cat.code in icons.ICONS, cat.code


def test_every_screening_button_has_an_icon():
    for name in ("common_items", "school_tool", "no_metal", "metal_detected",
                 "not_screened", "unfinished", "back"):
        assert name in icons.ICONS, name


def test_icons_render_to_a_real_image(qtbot):
    """A blank square is exactly what a broken path looks like on screen."""
    for name in icons.ICONS:
        pixmap = icons.pixmap(name, 48, "#E6EDEA")
        assert not pixmap.isNull(), name

        image = pixmap.toImage()
        drawn = sum(
            1 for y in range(0, image.height(), 3) for x in range(0, image.width(), 3)
            if image.pixelColor(x, y).alpha() > 0
        )
        assert drawn > 10, f"{name} rendered almost nothing"


def test_size_and_ratio_are_honoured(qtbot):
    pixmap = icons.pixmap("bladed", 32, "#FFFFFF")
    assert pixmap.devicePixelRatio() == 2.0
    assert pixmap.width() == 64                 # 32 logical at 2x


def test_colour_is_applied(qtbot):
    """Tinting is what lets one set of paths serve both the safe and danger cards."""
    light = icons.pixmap("bladed", 48, "#FFFFFF").toImage()
    red = icons.pixmap("bladed", 48, "#FF0000").toImage()
    assert light != red


def test_an_unknown_name_lists_the_valid_ones(qtbot):
    with pytest.raises(KeyError) as exc:
        icons.pixmap("knife", 24, "#FFFFFF")
    assert "bladed" in str(exc.value)


def test_icons_are_transparent_not_boxed(qtbot):
    """They sit on cards of two different colours, so the background must be clear."""
    image = icons.pixmap("back", 40, "#FFFFFF").toImage()
    assert image.pixelColor(0, 0).alpha() == 0
