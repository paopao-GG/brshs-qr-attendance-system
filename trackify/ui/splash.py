"""The startup screen: media/startup.jpg, held briefly, then faded into the kiosk.

**A child of the window, not a window of its own.** The first version was a second
top-level window sized to match, which is not the same thing: the window manager
places two top-level windows independently, so on a laptop the splash landed wherever
it liked and the kiosk appeared somewhere else underneath it. As a child covering its
parent's rect it is the app's position and size by construction, follows every resize,
and fades to reveal exactly the window it was sitting on.

The first animation in this codebase -- everything else in trackify/ui/ times state
with a plain QTimer (see kiosk.py), never motion. Kept in its own module rather than
grown inside kiosk.py so that precedent stays true for the rest of the UI.

media/*.jpg is a deliberate exception to icons.py's "no image asset files" policy --
see the comment there -- made for this startup image and the scan-station backdrop
(backdrop.py), not a general precedent for icons or UI chrome.
"""

from __future__ import annotations

from qtpy.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QImage, QPixmap
from qtpy.QtWidgets import QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

from ..core.config import PROJECT_ROOT
from . import backdrop

STARTUP_IMAGE = PROJECT_ROOT / "media" / "startup.jpg"

# Two seconds of the logo, then a quick hand-off to the scanning page.
HOLD_MS = 2000
FADE_MS = 400


class SplashScreen(QWidget):
    """Covers its parent, holds, then fades out.

    Construct it with the kiosk window as parent, then call start(). `finished` fires
    once the splash is gone and it is safe to reveal -- and start the camera on -- the
    window underneath.
    """

    finished = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Splash")
        self.setAttribute(Qt.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        source = QImage(str(STARTUP_IMAGE))
        # Null when the file is missing or unreadable: the splash then holds and fades
        # on the plain ground rather than refusing to start the station.
        blank = source.isNull()
        self._source = None if blank else QPixmap.fromImage(source)
        # startup.jpg's OWN edges, not the kiosk's ground: this image is a green
        # gradient and the backdrop is a pale photograph, so a shared bar colour would
        # be wrong for one of them. Same reasoning as backdrop.ground().
        bars = (QColor(backdrop.FALLBACK_GROUND) if blank
                else backdrop.ground(source))
        image.setStyleSheet(f"background: {bars.name()};")
        self._image = image
        layout.addWidget(image)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(1.0)

        # Cover the parent now and on every later resize. An event filter rather than
        # a resizeEvent override on KioskWindow: the splash is a one-shot thing and
        # should not leave permanent scaffolding in the window it covers.
        parent.installEventFilter(self)
        self._cover()
        self.raise_()

    # -- geometry -----------------------------------------------------------

    def _cover(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())

    def eventFilter(self, watched, event) -> bool:
        # Show as well as Resize. showFullScreen() is called inside KioskWindow's own
        # __init__, before this exists, but on some window managers the real
        # fullscreen geometry only lands once the window is mapped -- so the size at
        # construction can be the pre-fullscreen default and the splash would sit in
        # a corner of a much larger window. Qt also withholds Resize from a hidden
        # widget and delivers it on show, which the same branch picks up.
        if watched is self.parentWidget() and event.type() in (QEvent.Resize,
                                                               QEvent.Show):
            self._cover()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._source is not None:
            # KeepAspectRatio, not ByExpanding: startup.jpg has a border frame drawn
            # into it, and cropping to fill would cut that frame off on any window
            # whose aspect is not the image's 16:9.
            self._image.setPixmap(self._source.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # -- the one-shot ------------------------------------------------------

    def start(self) -> None:
        """Hold, then fade out. finished fires exactly once, however this ends."""
        QTimer.singleShot(HOLD_MS, self._fade_out)

    def _fade_out(self) -> None:
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(FADE_MS)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.finished.connect(self._close)
        self._animation.start()

    def _close(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        self.close()
        self.finished.emit()
