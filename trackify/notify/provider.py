"""Notification provider abstraction.

Three implementations ship: Gsm (a SIM800C on a USB serial port) for production,
Console for development, and Null for the pilot. Development and the pilot never spend
load and never text a real parent, which is what makes it safe to exercise the whole
pipeline end to end before go-live.

This abstraction earned itself when the transport changed from an HTTP API to a serial
modem: the queue, coalescing, spend breaker, worker and kiosk needed no change at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send attempt.

    `ambiguous` is the important field. It means the message may or may not have been
    submitted -- a timeout, a dropped connection, or a modem browning out after the
    body was written. On a GSM module that is common rather than rare, because the
    transmit burst is what causes the brownout in the first place. The
    queue must NOT auto-retry these: for SMS, at-most-once is the correct bias,
    because a missed text is recoverable and a duplicate erodes parent trust.
    """

    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    ambiguous: bool = False
    retry_after: float | None = None


@dataclass(frozen=True)
class Availability:
    """Whether the transport can send at all right now.

    Distinct from a failed send. "The module is not plugged in" is not a message that
    failed -- nothing was attempted, nothing should count against a retry budget, and
    the queue should wait rather than burn the backlog down to 'failed' while the
    hardware is absent. `reason` is written for the person standing at the kiosk, not
    for a log.
    """

    ok: bool
    reason: str = ""


class NotificationProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def send(self, recipient: str, body: str) -> SendResult:
        """Send one message to one normalised (639XXXXXXXXX) number."""

    def available(self) -> Availability:
        """Can this provider send right now?

        Called on every drain tick, so implementations must be cheap and must not
        block: this runs on the worker thread and gates the queue behind it. Software
        providers are always available; only the ones with hardware behind them have
        anything to answer.
        """
        return Availability(ok=True)

    def balance(self) -> int | None:
        """Remaining credits, or None when the provider cannot report it."""
        return None


class ConsoleProvider(NotificationProvider):
    """Prints instead of sending. The default during development."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, body: str) -> SendResult:
        self.sent.append((recipient, body))
        print(f"[SMS -> {recipient}] {body}")
        return SendResult(ok=True, provider_message_id=f"console-{len(self.sent)}")


class NullProvider(NotificationProvider):
    """Counts, prints nothing, sends nothing. For the pilot run."""

    name = "null"

    def __init__(self) -> None:
        self.count = 0

    def send(self, recipient: str, body: str) -> SendResult:
        self.count += 1
        return SendResult(ok=True, provider_message_id=f"null-{self.count}")
