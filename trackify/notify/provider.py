"""Notification provider abstraction.

Three implementations ship: PhilSMS for production, Console for development, and
Null for the pilot. Development and the pilot never spend credits and never text a
real parent, which is what makes it safe to exercise the whole pipeline end to end
before go-live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send attempt.

    `ambiguous` is the important field. It means the request may or may not have
    reached the provider -- a timeout or a dropped connection after the write. The
    queue must NOT auto-retry these: for SMS, at-most-once is the correct bias,
    because a missed text is recoverable and a duplicate erodes parent trust.
    """

    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    ambiguous: bool = False
    retry_after: float | None = None


class NotificationProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def send(self, recipient: str, body: str) -> SendResult:
        """Send one message to one normalised (639XXXXXXXXX) number."""

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
