"""Central notification manager with queue-based dispatch."""

import logging
import queue
import threading
from typing import Optional

from .base import Notifier, Level

logger = logging.getLogger(__name__)

# Map level strings to Level enum for quick lookups
_LEVEL_MAP = {name.lower(): member for name, member in Level.__members__.items()}


def _resolve_level(level: str) -> Level:
    """Convert a level string to a Level enum member (defaults to INFO)."""
    return _LEVEL_MAP.get(level.lower(), Level.INFO)


class _DispatchItem:
    """Internal wrapper for items placed on the dispatch queue."""

    __slots__ = ("fn", "args", "kwargs")

    def __init__(self, fn, args=(), kwargs=None):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs or {}

    def run(self):
        try:
            self.fn(*self.args, **self.kwargs)
        except Exception:
            logger.exception("Unhandled error in notification dispatch")


class NotificationManager:
    """Registers multiple :class:`Notifier` backends and dispatches alerts
    through a background thread so the caller is never blocked."""

    def __init__(self, min_level: str = "info"):
        """
        Args:
            min_level: Minimum severity level that will be dispatched.
                       Messages below this level are silently dropped.
        """
        self._notifiers: list[Notifier] = []
        self._min_level = _resolve_level(min_level)
        self._queue: queue.Queue[Optional[_DispatchItem]] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background dispatch thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="notification-dispatch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the dispatch thread to drain and exit."""
        if not self._running:
            return
        self._running = False
        self._queue.put(None)  # sentinel
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _dispatch_loop(self) -> None:
        """Background loop: pull items from the queue and execute them."""
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                # Drain remaining items before exiting
                while not self._queue.empty():
                    try:
                        remaining = self._queue.get_nowait()
                        if remaining is not None:
                            remaining.run()
                    except queue.Empty:
                        break
                break
            item.run()

    # ------------------------------------------------------------------
    # Notifier registration
    # ------------------------------------------------------------------
    def register(self, notifier: Notifier) -> None:
        """Add a notifier backend."""
        with self._lock:
            if notifier not in self._notifiers:
                self._notifiers.append(notifier)

    def unregister(self, notifier: Notifier) -> None:
        """Remove a notifier backend."""
        with self._lock:
            try:
                self._notifiers.remove(notifier)
            except ValueError:
                pass

    @property
    def notifiers(self) -> list[Notifier]:
        with self._lock:
            return list(self._notifiers)

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------
    def _enqueue(self, fn, args=(), kwargs=None) -> None:
        """Place a callable on the dispatch queue."""
        self._queue.put(_DispatchItem(fn, args, kwargs))

    def _send_to_all(self, message: str, level: str) -> None:
        """Actually call send() on every registered notifier."""
        level_val = _resolve_level(level)
        if level_val < self._min_level:
            return
        with self._lock:
            notifiers = list(self._notifiers)
        for n in notifiers:
            try:
                n.send(message, level)
            except Exception:
                logger.exception(
                    "Notifier %s failed to send message", type(n).__name__
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def dispatch(self, message: str, level: str = "info") -> None:
        """Enqueue a plain message for delivery to all notifiers."""
        self._enqueue(self._send_to_all, (message, level))

    def dispatch_trade_alert(
        self, symbol: str, side: str, qty: float, price: float, status: str
    ) -> None:
        """Enqueue a trade alert."""
        level_val = _resolve_level("info")
        if level_val < self._min_level:
            return

        def _do():
            with self._lock:
                notifiers = list(self._notifiers)
            for n in notifiers:
                try:
                    n.send_trade_alert(symbol, side, qty, price, status)
                except Exception:
                    logger.exception(
                        "Notifier %s failed on trade alert", type(n).__name__
                    )

        self._enqueue(_do)

    def dispatch_risk_alert(self, reason: str, details: str) -> None:
        """Enqueue a risk alert."""
        level_val = _resolve_level("warning")
        if level_val < self._min_level:
            return

        def _do():
            with self._lock:
                notifiers = list(self._notifiers)
            for n in notifiers:
                try:
                    n.send_risk_alert(reason, details)
                except Exception:
                    logger.exception(
                        "Notifier %s failed on risk alert", type(n).__name__
                    )

        self._enqueue(_do)

    def dispatch_system_alert(self, message: str, severity: str = "error") -> None:
        """Enqueue a system alert."""
        level_val = _resolve_level(severity)
        if level_val < self._min_level:
            return

        def _do():
            with self._lock:
                notifiers = list(self._notifiers)
            for n in notifiers:
                try:
                    n.send_system_alert(message, severity)
                except Exception:
                    logger.exception(
                        "Notifier %s failed on system alert", type(n).__name__
                    )

        self._enqueue(_do)
