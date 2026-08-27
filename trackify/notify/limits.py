"""Rate limiting. Three layers, protecting three different things.

The outbound circuit breaker is the one that matters most. On an unli-text SIM there
is no per-message cost to act as a brake, so this cap is the only thing between a loop
bug and several thousand texts to real guardians. Formerly prepaid credits are
prepaid, so a loop bug in a notification trigger can burn the whole budget in minutes.
The breaker is the difference between a bug costing PHP 20 and PHP 3,000: it halts the
worker and raises a visible alarm rather than continuing.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date as Date

from ..core.db import utcnow


class TokenBucket:
    """Rate limiter for scan input and outbound API calls.

    Used on the kiosk input handler so a stuck scanner or a held key cannot flood
    the queue, and on the outbound sender so a burst at the morning bell does not
    trigger provider-side 429s.
    """

    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking. Used for scan input, where dropping is correct."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def time_until(self, tokens: float = 1.0) -> float:
        """Seconds until `tokens` are available. Used to pace the send worker."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                return 0.0
            return (tokens - self._tokens) / self.rate


class BreakerTripped(RuntimeError):
    """A spend cap refused a send.

    Two caps raise this and they need OPPOSITE handling, so `halts` says which rather
    than the caller matching on the message text. It used to: queue.drain decided
    whether to stop the worker with `if "Daily SMS cap" in str(exc)`, so rewording an
    f-string in this file would have silently turned the daily cap into "suppress the
    whole queue one message at a time" -- with every test still passing.
    """

    halts = False


class DailyCapReached(BreakerTripped):
    """The whole day's budget is gone. The worker must stop, not continue.

    Almost always a notification trigger looping, and continuing would burn the rest of
    the SIM's credit proving it.
    """

    halts = True


class RecipientCapReached(BreakerTripped):
    """One guardian has had their day's allowance. Suppress this message and carry on --
    everybody else's messages are unaffected and must still go out."""

    halts = False


@dataclass(frozen=True)
class BreakerState:
    date: str
    sent_today: int
    cap: int
    tripped: bool

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.sent_today)


class SpendBreaker:
    """Hard daily cap on messages sent, plus a per-recipient daily cap.

    Counts are persisted, so a restart cannot reset the budget -- which is exactly
    what a crash-loop bug would otherwise do.
    """

    def __init__(self, conn: sqlite3.Connection, daily_cap: int, per_recipient_cap: int):
        self.conn = conn
        self.daily_cap = daily_cap
        self.per_recipient_cap = per_recipient_cap

    def _today(self) -> str:
        return Date.today().isoformat()

    def state(self) -> BreakerState:
        day = self._today()
        row = self.conn.execute(
            "SELECT sent_count, breaker_hit FROM sms_ledger WHERE date = ?", (day,)
        ).fetchone()
        sent = row["sent_count"] if row else 0
        tripped = bool(row["breaker_hit"]) if row else False
        return BreakerState(day, sent, self.daily_cap, tripped)

    def check(self, recipient: str) -> None:
        """Raise BreakerTripped if this send must not happen."""
        state = self.state()
        if state.tripped or state.sent_today >= self.daily_cap:
            self._trip()
            raise DailyCapReached(
                f"Daily SMS cap of {self.daily_cap} reached ({state.sent_today} sent). "
                "Sending halted. Investigate before raising the cap -- this usually "
                "means a notification trigger is looping."
            )

        day = self._today()
        to_recipient = self.conn.execute(
            """SELECT COUNT(*) FROM notifications
               WHERE guardian_mobile = ? AND status = 'sent'
                 AND substr(sent_at, 1, 10) = ?""",
            (recipient, day),
        ).fetchone()[0]
        if to_recipient >= self.per_recipient_cap:
            raise RecipientCapReached(
                f"Per-recipient daily cap of {self.per_recipient_cap} reached for "
                f"{recipient}. Message suppressed."
            )

    def record_sent(self, count: int = 1) -> None:
        day = self._today()
        self.conn.execute(
            """INSERT INTO sms_ledger (date, sent_count) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET sent_count = sent_count + ?""",
            (day, count, count),
        )

    def _trip(self) -> None:
        day = self._today()
        self.conn.execute(
            """INSERT INTO sms_ledger (date, sent_count, breaker_hit, breaker_at)
               VALUES (?, 0, 1, ?)
               ON CONFLICT(date) DO UPDATE SET breaker_hit = 1, breaker_at = ?""",
            (day, utcnow(), utcnow()),
        )

    def reset_today(self) -> None:
        """Admin action after investigating a trip. Always audited by the caller."""
        self.conn.execute(
            "UPDATE sms_ledger SET breaker_hit = 0, breaker_at = NULL WHERE date = ?",
            (self._today(),),
        )
