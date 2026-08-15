"""
Fault tolerance utilities: retry, circuit breaker, heartbeat, watchdog.

All components use only the standard library and are thread-safe where noted.
Import path assumes /app in sys.path.
"""

from __future__ import annotations

import enum
import functools
import logging
import random
import threading
import time
from typing import Any, Callable, Optional, Sequence, Tuple, Type, TypeVar, Union

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# 1. @retry decorator
# ---------------------------------------------------------------------------

def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_backoff: bool = True,
    jitter: bool = True,
    retryable_exceptions: Union[
        Type[BaseException], Tuple[Type[BaseException], ...]
    ] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable[[F], F]:
    """Decorator that retries a function on specified exceptions.

    Args:
        max_attempts: Total attempts (1 = no retry).
        base_delay: Initial delay in seconds between retries.
        max_delay: Cap on the delay.
        exponential_backoff: If True, delay doubles each attempt.
        jitter: If True, adds random jitter (±50 %) to delay.
        retryable_exceptions: Exception type(s) that trigger a retry.
        on_retry: Optional callback(attempt_number, exception) on each retry.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "retry: %s failed after %d attempts: %s",
                            func.__qualname__,
                            max_attempts,
                            exc,
                        )
                        raise
                    delay = base_delay
                    if exponential_backoff:
                        delay = base_delay * (2 ** (attempt - 1))
                    delay = min(delay, max_delay)
                    if jitter:
                        delay = delay * random.uniform(0.5, 1.5)
                    logger.warning(
                        "retry: %s attempt %d/%d failed (%s). "
                        "Retrying in %.2fs …",
                        func.__qualname__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    if on_retry is not None:
                        on_retry(attempt, exc)
                    time.sleep(delay)
            # Should be unreachable, but satisfy type checker
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# 2. CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Thread-safe circuit breaker.

    States:
        CLOSED  – normal operation; failures are counted.
        OPEN    – calls are rejected immediately for ``recovery_timeout``.
        HALF_OPEN – a limited number of probe calls are allowed through.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        on_state_change: Optional[Callable[[CircuitState, CircuitState], None]] = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._on_state_change = on_state_change

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0

    # -- public API ---------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._state

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of breaker internals."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self._failure_threshold,
                "last_failure_time": self._last_failure_time,
                "recovery_timeout": self._recovery_timeout,
                "half_open_calls": self._half_open_calls,
            }

    def record_success(self) -> None:
        """Record a successful call (resets counters, closes breaker)."""
        with self._lock:
            self._failure_count = 0
            self._half_open_calls = 0
            if self._state != CircuitState.CLOSED:
                self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN)
            elif self._failure_count >= self._failure_threshold:
                self._transition(CircuitState.OPEN)

    def allow_request(self) -> bool:
        """Return True if a call should be allowed through."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if (time.monotonic() - self._last_failure_time) >= self._recovery_timeout:
                    self._transition(CircuitState.HALF_OPEN)
                    return self._allow_half_open()
                return False
            # HALF_OPEN
            return self._allow_half_open()

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED."""
        with self._lock:
            self._failure_count = 0
            self._half_open_calls = 0
            self._transition(CircuitState.CLOSED)

    # -- internals ----------------------------------------------------------

    def _allow_half_open(self) -> bool:
        if self._half_open_calls < self._half_open_max_calls:
            self._half_open_calls += 1
            return True
        return False

    def _transition(self, new_state: CircuitState) -> None:
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._half_open_calls = 0
        logger.info("CircuitBreaker: %s → %s", old.value, new_state.value)
        if self._on_state_change is not None:
            try:
                self._on_state_change(old, new_state)
            except Exception:
                logger.exception("circuit_breaker on_state_change callback error")


def circuit_breaker(
    breaker: CircuitBreaker,
) -> Callable[[F], F]:
    """Decorator that gates a function through a ``CircuitBreaker``.

    Usage::

        cb = CircuitBreaker(failure_threshold=3)

        @circuit_breaker(cb)
        def call_remote(): ...

    Raises ``CircuitOpenError`` when the breaker rejects the call.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not breaker.allow_request():
                status = breaker.get_status()
                raise CircuitOpenError(
                    f"Circuit breaker is OPEN for {func.__qualname__} "
                    f"(failures={status['failure_count']})"
                )
            try:
                result = func(*args, **kwargs)
            except Exception:
                breaker.record_failure()
                raise
            else:
                breaker.record_success()
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


class CircuitOpenError(Exception):
    """Raised when a circuit breaker rejects a call."""


# ---------------------------------------------------------------------------
# 3. Heartbeat
# ---------------------------------------------------------------------------

class Heartbeat:
    """Periodic heartbeat with missed-beat detection.

    Args:
        interval_seconds: Seconds between heartbeats.
        on_heartbeat: Called each time the heartbeat fires.
        on_missed_heartbeat: Called after ``allowed_misses`` consecutive
            beats have been missed (no successful ``on_heartbeat``).
        allowed_misses: Number of consecutive misses before the alert fires.
    """

    def __init__(
        self,
        interval_seconds: float = 10.0,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_missed_heartbeat: Optional[Callable[[int], None]] = None,
        allowed_misses: int = 3,
    ) -> None:
        self._interval = interval_seconds
        self._on_heartbeat = on_heartbeat
        self._on_missed_heartbeat = on_missed_heartbeat
        self._allowed_misses = allowed_misses

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._beat_event = threading.Event()
        self._miss_count = 0

    def start(self) -> None:
        """Start the heartbeat thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Heartbeat already running")
            return
        self._stop_event.clear()
        self._beat_event.clear()
        self._miss_count = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="heartbeat"
        )
        self._thread.start()
        logger.info("Heartbeat started (interval=%.1fs)", self._interval)

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Signal the heartbeat thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Heartbeat stopped")

    def notify_beat(self) -> None:
        """External signal that a beat was successful (resets miss count)."""
        self._miss_count = 0
        self._beat_event.set()

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._beat_event.clear()
            # Execute heartbeat callback
            success = True
            if self._on_heartbeat is not None:
                try:
                    self._on_heartbeat()
                except Exception:
                    logger.exception("Heartbeat callback error")
                    success = False

            # If caller doesn't use notify_beat(), treat callback success
            # as a beat.  If notify_beat() *is* used, _beat_event controls.
            if success:
                self._miss_count = 0
            else:
                self._miss_count += 1
                if (
                    self._miss_count >= self._allowed_misses
                    and self._on_missed_heartbeat is not None
                ):
                    try:
                        self._on_missed_heartbeat(self._miss_count)
                    except Exception:
                        logger.exception("on_missed_heartbeat callback error")

            self._stop_event.wait(self._interval)


# ---------------------------------------------------------------------------
# 4. Watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    """Thread-based watchdog that monitors health-check functions.

    Each monitored task runs ``check_fn()`` at ``interval`` seconds.  If the
    check does not return a truthy value within ``timeout`` seconds the task
    is considered failed and ``restart_fn`` (or an auto-restart by re-running
    ``check_fn``) is triggered.  An ``alert`` callback fires on failure.
    """

    def __init__(
        self,
        alert: Optional[Callable[[str, Optional[BaseException]], None]] = None,
    ) -> None:
        self._alert = alert
        self._lock = threading.Lock()
        self._tasks: dict[str, _WatchdogTask] = {}
        self._running = False

    def monitor(
        self,
        name: str,
        check_fn: Callable[[], bool],
        interval: float = 10.0,
        timeout: float = 30.0,
        restart_fn: Optional[Callable[[], None]] = None,
        max_restarts: int = 3,
        restart_cooldown: float = 5.0,
    ) -> None:
        """Register a task for monitoring.

        Args:
            name: Human-readable label.
            check_fn: Callable returning True when healthy.
            interval: Seconds between checks.
            timeout: Seconds before a check is considered timed-out.
            restart_fn: Called when the task fails.  If None, ``check_fn`` is
                re-invoked as a simple "restart".
            max_restarts: Max consecutive restarts before giving up.
            restart_cooldown: Seconds to wait after a restart before
                re-checking.
        """
        with self._lock:
            if name in self._tasks:
                raise ValueError(f"Task '{name}' is already monitored")
            task = _WatchdogTask(
                name=name,
                check_fn=check_fn,
                interval=interval,
                timeout=timeout,
                restart_fn=restart_fn or check_fn,
                max_restarts=max_restarts,
                restart_cooldown=restart_cooldown,
            )
            self._tasks[name] = task
            if self._running:
                task.start(self._on_task_alert)

    def unmonitor(self, name: str) -> None:
        """Stop monitoring a task."""
        with self._lock:
            task = self._tasks.pop(name, None)
            if task is not None:
                task.stop()
                logger.info("Watchdog: unmonitored '%s'", name)

    def start_all(self) -> None:
        """Start monitoring all registered tasks."""
        with self._lock:
            self._running = True
            for task in self._tasks.values():
                task.start(self._on_task_alert)
        logger.info("Watchdog: started monitoring %d task(s)", len(self._tasks))

    def stop_all(self, timeout: Optional[float] = 5.0) -> None:
        """Stop all monitoring threads."""
        with self._lock:
            self._running = False
            for task in self._tasks.values():
                task.stop(timeout)
        logger.info("Watchdog: all tasks stopped")

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Return status of every monitored task."""
        with self._lock:
            return {name: t.get_status() for name, t in self._tasks.items()}

    # -- internals ----------------------------------------------------------

    def _on_task_alert(self, name: str, exc: Optional[BaseException]) -> None:
        logger.error("Watchdog ALERT: '%s' failed", name, exc_info=exc)
        if self._alert is not None:
            try:
                self._alert(name, exc)
            except Exception:
                logger.exception("Watchdog alert callback error for '%s'", name)


class _WatchdogTask:
    """Internal per-task monitor."""

    def __init__(
        self,
        name: str,
        check_fn: Callable[[], bool],
        interval: float,
        timeout: float,
        restart_fn: Callable[[], None],
        max_restarts: int,
        restart_cooldown: float,
    ) -> None:
        self.name = name
        self._check_fn = check_fn
        self._interval = interval
        self._timeout = timeout
        self._restart_fn = restart_fn
        self._max_restarts = max_restarts
        self._restart_cooldown = restart_cooldown

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._total_restarts = 0
        self._last_check_ok = True
        self._alert_cb: Optional[Callable[[str, Optional[BaseException]], None]] = None

    def start(self, alert_cb: Callable[[str, Optional[BaseException]], None]) -> None:
        self._alert_cb = alert_cb
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"watchdog-{self.name}"
        )
        self._thread.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            self._thread = None

    def get_status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self._thread is not None and self._thread.is_alive(),
            "last_check_ok": self._last_check_ok,
            "consecutive_failures": self._consecutive_failures,
            "total_restarts": self._total_restarts,
        }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Run the check with a timeout via a wrapper thread
            ok = self._run_check_with_timeout()
            self._last_check_ok = ok

            if ok:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._alert_cb is not None:
                    self._alert_cb(self.name, None)
                # Attempt restart
                if self._consecutive_failures <= self._max_restarts:
                    self._attempt_restart()
                else:
                    logger.error(
                        "Watchdog '%s': max restarts (%d) exhausted",
                        self.name,
                        self._max_restarts,
                    )

            self._stop_event.wait(self._interval)

    def _run_check_with_timeout(self) -> bool:
        result: list[Optional[bool]] = [None]
        exc_holder: list[Optional[BaseException]] = [None]

        def target() -> None:
            try:
                result[0] = bool(self._check_fn())
            except Exception as exc:
                exc_holder[0] = exc

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=self._timeout)

        if t.is_alive():
            logger.error("Watchdog '%s': check timed out after %.1fs", self.name, self._timeout)
            return False
        if exc_holder[0] is not None:
            logger.error("Watchdog '%s': check raised %s", self.name, exc_holder[0])
            return False
        return bool(result[0])

    def _attempt_restart(self) -> None:
        logger.warning("Watchdog '%s': attempting restart", self.name)
        try:
            self._restart_fn()
            self._total_restarts += 1
            self._stop_event.wait(self._restart_cooldown)
        except Exception as exc:
            logger.error("Watchdog '%s': restart failed: %s", self.name, exc)
            if self._alert_cb is not None:
                self._alert_cb(self.name, exc)
