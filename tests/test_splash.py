"""The startup splash: covers its parent exactly, holds, fades, finishes once.

The geometry tests are the point of this module. The splash used to be a second
top-level window sized to match the kiosk, which is not the same as being the same
size and position -- the window manager placed the two independently and they did not
line up. As a child covering its parent's rect it cannot drift.
"""

import pytest

pytest.importorskip("qtpy")

from qtpy.QtWidgets import QWidget

from trackify.ui.splash import FADE_MS, HOLD_MS, SplashScreen


@pytest.fixture
def parent(qtbot):
    widget = QWidget()
    widget.resize(900, 600)
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def splash(parent):
    # Not qtbot.addWidget(widget): SplashScreen sets WA_DeleteOnClose so it does not
    # linger over the kiosk after the fade, and start() closes it itself on finish --
    # qtbot's teardown closing it a second time hits an already-deleted C++ object.
    return SplashScreen(parent)


def test_the_logo_is_held_for_two_seconds(splash):
    """What was actually asked for: two seconds of the logo, then the scanning page.
    The fade is deliberately shorter than the hold -- it is a hand-off, not a feature."""
    assert HOLD_MS == 2000
    assert FADE_MS < HOLD_MS


def test_the_startup_image_loads(splash):
    """media/startup.jpg is a real file this repo ships; a missing or unreadable one
    would leave the label with a null pixmap instead of raising."""
    assert splash._source is not None
    assert not splash._source.isNull()


def test_the_splash_covers_its_parent_exactly(parent, splash):
    """Not "the same size as" -- the same rect, which is what makes it the app's own
    position and size rather than a second window placed near it."""
    assert splash.parentWidget() is parent
    assert splash.geometry() == parent.rect()


def test_the_splash_follows_the_parent_when_it_resizes(qtbot, parent, splash):
    """The case that matters on the Pi: showFullScreen() can land its real geometry
    only once the window is mapped, so the size at construction is not necessarily the
    size the splash has to cover. Shown first because Qt withholds Resize from a
    hidden widget and delivers it on show."""
    parent.show()
    qtbot.wait(50)
    parent.resize(1280, 800)
    qtbot.wait(50)

    assert splash.geometry() == parent.rect()
    assert splash.width() == 1280 and splash.height() == 800


def test_a_resize_while_hidden_is_caught_when_the_window_appears(qtbot, parent, splash):
    """Qt batches the resize rather than dropping it; the splash must not be left at
    the old size once the window it covers becomes visible."""
    parent.resize(1360, 900)
    parent.show()
    qtbot.wait(50)

    assert splash.geometry() == parent.rect()


def test_the_splash_holds_then_fades_and_finishes(qtbot, splash):
    with qtbot.waitSignal(splash.finished, timeout=HOLD_MS + FADE_MS + 2000):
        splash.start()


def test_finished_fires_exactly_once(qtbot, splash):
    calls = []
    splash.finished.connect(lambda: calls.append(1))

    with qtbot.waitSignal(splash.finished, timeout=HOLD_MS + FADE_MS + 2000):
        splash.start()
    qtbot.wait(100)

    assert calls == [1]


def test_the_parent_survives_the_splash_closing(qtbot, parent, splash):
    """The event filter is installed on the parent, so it has to come off again --
    a filter left on a deleted child is a crash waiting for the next resize."""
    with qtbot.waitSignal(splash.finished, timeout=HOLD_MS + FADE_MS + 2000):
        splash.start()
    qtbot.wait(100)

    parent.resize(1000, 700)          # would touch the dead filter if it were still on
    qtbot.wait(50)
    assert parent.width() == 1000
