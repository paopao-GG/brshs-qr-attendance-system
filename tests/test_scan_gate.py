"""The sensor-level de-duplication, tested without a camera or a clock.

A webcam sees the same code about ten times a second. Everything here exists to make
one presentation of one card mean exactly one scan.
"""

from trackify.scan.gate import ScanGate

A = "TRK-1-3fb640d9"
B = "TRK-2-bc18c072"


class FakeClock:
    """Time only moves when a test says so, so nothing here sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def gate(**kwargs):
    kwargs.setdefault("absence_frames", 5)
    kwargs.setdefault("cooldown_ms", 0)
    return ScanGate(**kwargs)


def test_one_presentation_fires_once():
    g = gate()
    fired = [g.offer(A) for _ in range(30)]
    assert [f for f in fired if f] == [A], "a held card must not scan thirty times"


def test_card_that_never_leaves_never_refires():
    """The reason re-arming is absence-based and not a timer."""
    clock = FakeClock()
    g = gate(clock=clock)
    assert g.offer(A) == A
    for _ in range(500):
        clock.advance(1.0)              # eight minutes of staring at the same card
        assert g.offer(A) is None


def test_removing_and_re_presenting_fires_again():
    g = gate(absence_frames=5)
    assert g.offer(A) == A
    for _ in range(5):
        assert g.offer(None) is None    # card taken away
    assert g.offer(A) == A, "a genuine second presentation must register"


def test_brief_gap_does_not_re_arm():
    """A dropped frame or two is not the student walking away."""
    g = gate(absence_frames=5)
    assert g.offer(A) == A
    for _ in range(4):
        g.offer(None)
    assert g.offer(A) is None


def test_a_different_card_fires_while_the_first_is_latched():
    """Two students at the lens are two scans, not one."""
    g = gate()
    assert g.offer(A) == A
    assert g.offer(B) == B
    assert g.offer(B) is None


def test_cooldown_blocks_everything():
    clock = FakeClock()
    g = gate(cooldown_ms=3000, clock=clock)
    assert g.offer(A) == A
    assert g.offer(B) is None, "a result on screen must not be overwritten"
    clock.advance(3.1)
    assert g.offer(B) == B


def test_hold_extends_but_never_shortens():
    clock = FakeClock()
    g = gate(cooldown_ms=0, clock=clock)
    g.hold(5000)
    g.hold(1000)                        # a shorter hold must not cancel the longer one
    clock.advance(2.0)
    assert g.offer(A) is None
    clock.advance(3.1)
    assert g.offer(A) == A


def test_absence_still_counts_while_blocked():
    """A card removed during the hold is properly re-armed by the time it lifts."""
    clock = FakeClock()
    g = gate(absence_frames=3, cooldown_ms=2000, clock=clock)
    assert g.offer(A) == A
    for _ in range(3):
        g.offer(None)
    assert g.latched is None
    clock.advance(2.1)
    assert g.offer(A) == A


def test_none_alone_never_fires():
    g = gate()
    assert all(g.offer(None) is None for _ in range(20))
