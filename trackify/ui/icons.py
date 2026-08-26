"""Line icons, drawn in code.

**Not emoji.** Raspberry Pi OS frequently ships without an emoji font, so a glyph that
looks fine on the development laptop renders as an empty box on the one machine that
matters -- and nobody finds out until setup day. Same reasoning that kept OpenCV and
pyzbar out of this project: no dependency that is present here and absent there.

**Not asset files either.** A directory of PNGs is one careless copy away from a screen
full of blank squares. These are SVG path strings in this module, rendered through
QtSvg (which ships with PySide6), so they travel with the source and tint to whatever
the palette says.

Everything is a 24x24 viewBox with a 1.6 stroke, no fill -- consistent weight matters
more than detail at the size these are shown.
"""

from __future__ import annotations

from qtpy.QtCore import QRectF, Qt
from qtpy.QtGui import QIcon, QPainter, QPixmap
from qtpy.QtSvg import QSvgRenderer

STROKE = 1.6

# name -> the markup inside <svg>. Deliberately simple shapes: these are read at a
# glance by someone holding a bag open, not studied.
ICONS: dict[str, str] = {
    # --- item categories ---------------------------------------------------
    "common_items": (          # a phone
        '<rect x="7" y="2.5" width="10" height="19" rx="2"/>'
        '<line x1="10.5" y1="18.5" x2="13.5" y2="18.5"/>'
    ),
    "school_tool": (           # scissors
        '<circle cx="6" cy="18" r="2.6"/>'
        '<circle cx="18" cy="18" r="2.6"/>'
        '<line x1="7.8" y1="16.1" x2="19" y2="3.5"/>'
        '<line x1="16.2" y1="16.1" x2="5" y2="3.5"/>'
    ),
    "bladed": (                # a knife, tip to the left, handle to the right
        '<path d="M2 15 L7.5 8.5 H14.5 V15 Z"/>'
        '<rect x="14.5" y="10.2" width="7.5" height="4" rx="1.8"/>'
    ),
    "blunt": (                 # a hammer, head across the top
        '<rect x="3.5" y="3.5" width="13" height="5.2" rx="1.6"/>'
        '<path d="M11.5 8.7 V19 a2 2 0 0 0 4 0 V8.7"/>'
    ),
    "pointed": (               # a spike / awl
        '<path d="M12 2 L14.5 12 h-5 Z"/>'
        '<rect x="9" y="12" width="6" height="8" rx="1.4"/>'
        '<line x1="9" y1="15.5" x2="15" y2="15.5"/>'
    ),
    "other": (                 # a question mark in a rounded square
        '<rect x="3" y="3" width="18" height="18" rx="4"/>'
        '<path d="M9.6 9.4 a2.5 2.5 0 1 1 3.3 2.4 c-0.7 0.3 -0.9 0.8 -0.9 1.5 '
        'v0.5"/>'
        '<line x1="12" y1="17" x2="12" y2="17.2"/>'
    ),
    # --- attendance statuses ------------------------------------------------
    # Bare marks, no enclosing circle: at register size a circle collapses into a
    # smudge and the check and the cross stop being distinguishable, which is the
    # only job these two have.
    "present": '<path d="M4.5 12.5 L9.5 17.5 L19.5 6.5"/>',
    "absent": (
        '<line x1="6" y1="6" x2="18" y2="18"/>'
        '<line x1="18" y1="6" x2="6" y2="18"/>'
    ),

    # --- outcomes and navigation -------------------------------------------
    "no_metal": (              # a tick in a circle
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M8 12.3 L11 15.2 L16.2 9"/>'
    ),
    "metal_detected": (        # an alert triangle
        '<path d="M12 3.5 L21.5 20 h-19 Z"/>'
        '<line x1="12" y1="9.5" x2="12" y2="14"/>'
        '<line x1="12" y1="16.8" x2="12" y2="17"/>'
    ),
    "not_screened": (          # a slashed circle
        '<circle cx="12" cy="12" r="9"/>'
        '<line x1="5.6" y1="18.4" x2="18.4" y2="5.6"/>'
    ),
    "unfinished": (            # a clock
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M12 6.8 V12 l3.4 2.2"/>'
    ),
    "back": (                  # a left arrow
        '<line x1="20" y1="12" x2="4.5" y2="12"/>'
        '<path d="M10.5 5.5 L4 12 l6.5 6.5"/>'
    ),
}


def _svg(name: str, colour: str) -> bytes:
    try:
        body = ICONS[name]
    except KeyError:
        raise KeyError(
            f"no icon named {name!r}. Available: {', '.join(sorted(ICONS))}"
        ) from None
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{colour}" stroke-width="{STROKE}" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode("utf8")


def pixmap(name: str, size: int, colour: str) -> QPixmap:
    """Render one icon at `size` logical pixels in `colour`.

    devicePixelRatio is honoured, so the strokes stay crisp on a scaled display
    instead of going soft the way a fixed-size raster asset would.
    """
    renderer = QSvgRenderer(_svg(name, colour))

    ratio = 2.0            # cheap and always sharp; these are tiny images
    canvas = QPixmap(int(size * ratio), int(size * ratio))
    canvas.fill(Qt.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()

    canvas.setDevicePixelRatio(ratio)
    return canvas


def icon(name: str, size: int, colour: str) -> QIcon:
    return QIcon(pixmap(name, size, colour))
