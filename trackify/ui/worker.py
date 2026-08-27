"""SMS drain worker.

Runs the notification queue on a QThread so the Qt UI thread never blocks on network
I/O. A 2G SMS submit takes three to ten seconds, and would otherwise freeze the scan
station mid-queue -- the most common way PyQt applications fail in the field. With a
serial modem this is not a worst case, it is every single message.

Rules enforced here:
  * the worker owns its OWN SQLite connection (connections are never shared across
    threads)
  * no widget is ever touched from this thread; everything leaves via signals
"""

from __future__ import annotations

from dataclasses import dataclass

from qtpy.QtCore import QMetaObject, QObject, Qt, QTimer, Signal, Slot

from ..core.config import Config
from ..core.db import connect
from ..notify import queue
from ..notify.limits import BreakerTripped, SpendBreaker, TokenBucket
from ..notify.provider import NotificationProvider


@dataclass(frozen=True)
class QueueStats:
    unsent: int = 0
    sent_this_pass: int = 0
    messages_this_pass: int = 0
    provider: str = ""
    breaker_tripped: bool = False
    error: str = ""
    provider_available: bool = True
    provider_detail: str = ""
    # Whether a text will actually reach a handset: the provider has to be a real
    # transport AND the station has to be live. Defaults True so a QueueStats built
    # without it -- as most tests do -- keeps meaning what it did.
    provider_sends: bool = True


class SmsWorker(QObject):
    """Lives on a QThread. Drains the queue on a timer."""

    stats_changed = Signal(object)   # QueueStats
    alarm = Signal(str)              # circuit breaker or fatal error

    def __init__(
        self,
        provider: NotificationProvider,
        config: Config,
        db_path=None,
        interval_ms: int = 4000,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.config = config
        self.db_path = db_path
        self.interval_ms = interval_ms
        self._conn = None
        self._breaker = None
        self._bucket = None
        self._timer = None
        self._halted = False
        self._stopping = False

    def start(self) -> None:
        """Called once the thread is running, never from the UI thread."""
        # This connection belongs to the worker thread and to nothing else.
        self._conn = connect(self.db_path)
        self._breaker = SpendBreaker(
            self._conn,
            self.config.limits.daily_message_cap,
            self.config.limits.per_recipient_daily_cap,
        )
        self._bucket = TokenBucket(self.config.limits.requests_per_second)

        # Anything left mid-send by a previous crash becomes 'unknown', never
        # 'pending' -- re-sending risks a duplicate parent notification.
        recovered = queue.reconcile_stale(self._conn)
        if recovered:
            self.alarm.emit(
                f"{recovered} notification(s) were interrupted mid-send and need "
                "checking by hand. A GSM module has no provider dashboard to "
                "reconcile against, so these need a human decision."
            )

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.interval_ms)
        self._tick()

    @Slot()
    def stop(self) -> None:
        """Stop draining. Must reach this object on ITS OWN thread.

        The timer belongs to the worker thread, and Qt refuses to stop a timer from
        another one -- calling this directly from the UI thread logs
        "QObject::killTimer: Timers cannot be stopped from another thread" and leaves
        the timer running. Callers should use stop_from_ui().
        """
        if self._timer is not None:
            self._timer.stop()

    def stop_from_ui(self) -> None:
        """Ask the worker to stop, from the UI thread, without waiting on it.

        The flag is set first and read by _tick, so no further drain begins even if the
        queued call has to wait behind work already running.

        Deliberately NOT a BlockingQueuedConnection. That blocked the UI thread until
        the worker returned to its event loop, which with a serial modem could be a
        60-second send or -- against a port that never answers -- minutes of AT
        timeouts. Quitting the kiosk froze for exactly as long as the hardware was
        broken. The caller's thread.quit()/wait(3000) bounds the shutdown instead.
        """
        self._stopping = True
        QMetaObject.invokeMethod(self, "stop", Qt.QueuedConnection)

    @property
    def _sends(self) -> bool:
        """Will a message queued right now actually reach a guardian?

        Both halves have to hold: a transport that really sends (not console or null)
        AND SMS_LIVE set on this station. The kiosk shows "(not sending)" when this is
        false, so an operator can see that the gate is recording arrivals and telling
        nobody -- whichever of the two reasons is responsible.
        """
        return self.provider.sends_real_messages and self.config.secrets.sms_live

    @property
    def _not_sending_reason(self) -> str:
        """Why nothing is going out, for the status bar tooltip.

        Worth distinguishing, because the two causes leave DIFFERENT rows behind: a
        software provider reports ok and the notification is marked 'sent' though
        nothing was sent, while SMS_LIVE=false marks it 'suppressed'. Somebody reading
        the table afterwards needs to know which they are looking at.

        Computed here rather than in the kiosk because only the worker holds both the
        provider and the config, and trackify/ui carries no domain logic.
        """
        if not self.provider.sends_real_messages:
            return (
                f"The {self.provider.name} provider does not send anything"
                + (" -- messages are printed to the log."
                   if self.provider.name == "console" else ".")
                + "\n\nNotifications are still marked 'sent' in the database even "
                "though nothing left this machine, so do not read those counts as "
                "evidence a guardian was told."
            )
        return (
            "SMS_LIVE is false in .env, so this station is not sending.\n\n"
            "Scanning, attendance and queueing all work normally; each notification "
            "is recorded as 'suppressed' instead of being delivered. Set SMS_LIVE=true "
            "and restart to go live."
        )

    def _tick(self) -> None:
        if self._halted or self._stopping or self._conn is None:
            return

        # Asked before anything is claimed. A missing module is not a failed message:
        # draining into one would run every queued notification through _retry_or_fail,
        # and the backoff ladder reaches retry_limit in under two hours -- so the
        # morning's notifications would be permanently 'failed' by mid-morning and
        # plugging the module in at noon would send nothing. Left pending, they go out
        # on the first tick after it comes back.
        availability = self.provider.available()
        if not availability.ok:
            self.stats_changed.emit(QueueStats(
                unsent=queue.unsent_count(self._conn),
                provider=self.provider.name,
                provider_sends=self._sends,
                provider_available=False,
                provider_detail=availability.reason,
            ))
            return

        try:
            stats = queue.drain(
                self._conn, self.provider, self.config, self._breaker, self._bucket
            )
        except BreakerTripped as exc:
            self._halted = True
            self.alarm.emit(str(exc))
            self.stats_changed.emit(QueueStats(
                unsent=queue.unsent_count(self._conn),
                provider=self.provider.name,
                provider_sends=self._sends,
                provider_detail="" if self._sends else self._not_sending_reason,
                breaker_tripped=True,
                error=str(exc),
            ))
            return
        except Exception as exc:  # noqa: BLE001 - never kill the worker thread
            self.stats_changed.emit(QueueStats(
                unsent=queue.unsent_count(self._conn),
                provider=self.provider.name,
                provider_sends=self._sends,
                provider_detail="" if self._sends else self._not_sending_reason,
                error=str(exc),
            ))
            return

        self.stats_changed.emit(QueueStats(
            unsent=queue.unsent_count(self._conn),
            sent_this_pass=stats["sent"],
            messages_this_pass=stats["messages"],
            provider=self.provider.name,
            provider_sends=self._sends,
            provider_detail="" if self._sends else self._not_sending_reason,
        ))
