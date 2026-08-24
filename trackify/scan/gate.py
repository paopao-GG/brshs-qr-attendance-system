"""The fourth de-duplication mechanism, at the sensor level.

The system already de-duplicates three times: debounce (duplicate attendance rows),
idempotency (duplicate sends), and guardian coalescing (siblings). A camera needs one
more, upstream of all of them, because it differs from a HID scanner in a way that
matters: it sees the same code roughly ten times a second. One presentation of one card
would otherwise become ten scans.

Two rules, each with exactly one job:

  Same-code latch    The code just read cannot fire again until it has been ABSENT for
                     N consecutive decode attempts. A card left lying in front of the
                     lens does not re-fire, no matter how long it sits there.

  Global cooldown    After any fire, nothing fires for the duration of the result the
                     kiosk is currently showing. A queue of students crowding the lens
                     cannot overwrite a result before anyone has read it.

Re-arming is absence-based, not timer-based. That distinction is the whole point: a timer
would eventually re-fire a stationary card, which is precisely the failure being
prevented.

This sits BEFORE the kiosk's token bucket. The bucket is the right defence against a
stuck HID scanner and the wrong one against a camera -- without this gate it would show
"Scanning too fast" continuously.
"""

from __future__ import annotations

import time


class ScanGate:
    """Decides which decoded payloads are real presentations and which are repeats.

    Not thread-safe by design: it lives entirely on the decode thread, so suppressed
    frames never cross a thread boundary. The UI raises the cooldown through a queued
    signal rather than by touching this object.
    """

    def __init__(
        self,
        *,
        absence_frames: int = 5,
        cooldown_ms: int = 1500,
        clock=time.monotonic,
    ) -> None:
        if absence_frames < 1:
            raise ValueError("absence_frames must be at least 1")
        self.absence_frames = absence_frames
        self.cooldown_ms = cooldown_ms
        self._clock = clock

        self._latched: str | None = None    # the code that may not fire again yet
        self._absent = 0                    # consecutive attempts without _latched
        self._blocked_until = 0.0           # monotonic seconds

    # -- input --------------------------------------------------------------

    def offer(self, payload: str | None) -> str | None:
        """Feed one decode attempt. Returns a payload to act on, or None.

        `payload` is None when the frame contained no readable code -- that is not a
        non-event, it is what re-arms the latch.
        """
        if payload is None:
            self._note_absence()
            return None

        if payload == self._latched:
            # Same card still in view. Absence is what clears it, not time.
            self._absent = 0
            return None

        # A different code appeared, so whatever was latched is no longer in frame.
        self._note_absence()

        if self._clock() < self._blocked_until:
            return None

        self._latched = payload
        self._absent = 0
        self.hold(self.cooldown_ms)
        return payload

    def _note_absence(self) -> None:
        # A run of failed decodes while the card is still physically in frame will
        # eventually clear the latch and let the same card fire twice. That is
        # tolerable and deliberate: absence_frames at decode_fps is half a second of
        # continuous failure, and the domain debounce catches the consequence -- the
        # second scan renders as amber "Already recorded", writing no extra row and
        # sending no extra text. The cost is a cosmetic flash, not bad data.
        if self._latched is None:
            return
        self._absent += 1
        if self._absent >= self.absence_frames:
            self._latched = None
            self._absent = 0

    # -- cooldown -----------------------------------------------------------

    def hold(self, ms: int) -> None:
        """Block all firing for `ms`, never shortening an existing hold.

        The kiosk calls this with the actual presentation hold time, so a red
        'code not recognised' (5 s) blocks longer than a green IN (3 s). The
        configured cooldown_ms is only the floor.
        """
        until = self._clock() + max(ms, 0) / 1000.0
        self._blocked_until = max(self._blocked_until, until)

    # -- introspection, for the diagnostic script and tests -----------------

    @property
    def latched(self) -> str | None:
        return self._latched

    def is_blocked(self) -> bool:
        return self._clock() < self._blocked_until
