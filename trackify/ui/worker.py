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
        """Ask the worker to stop, from the UI thread, and wait for it to happen."""
        QMetaObject.invokeMethod(self, "stop", Qt.BlockingQueuedConnection)

    def _tick(self) -> None:
        if self._halted or self._conn is None:
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
                breaker_tripped=True,
                error=str(exc),
            ))
            return
        except Exception as exc:                      # never kill the thread
            self.stats_changed.emit(QueueStats(
                unsent=queue.unsent_count(self._conn),
                provider=self.provider.name,
                error=str(exc),
            ))
            return

        self.stats_changed.emit(QueueStats(
            unsent=queue.unsent_count(self._conn),
            sent_this_pass=stats["sent"],
            messages_this_pass=stats["messages"],
            provider=self.provider.name,
        ))
