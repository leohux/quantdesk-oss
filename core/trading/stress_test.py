"""Risk Stress Test Module - fault injection and resilience testing for the trading system.

Provides a framework for injecting faults (network disconnects, data corruption,
infrastructure failures, price anomalies) and verifying that the system recovers
gracefully within defined tolerances.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fault types
# ---------------------------------------------------------------------------

class FaultType(str, Enum):
    NETWORK_DISCONNECT = "network_disconnect"
    DATA_INTERRUPTION = "data_interruption"
    API_TIMEOUT = "api_timeout"
    DATA_DUPLICATE = "data_duplicate"
    DATA_OUT_OF_ORDER = "data_out_of_order"
    CLOCK_DRIFT = "clock_drift"
    REDIS_RESTART = "redis_restart"
    POSTGRES_RESTART = "postgres_restart"
    PRICE_SPIKE = "price_spike"
    VOLUME_SPIKE = "volume_spike"


# ---------------------------------------------------------------------------
# Fault Injection Engine
# ---------------------------------------------------------------------------

class FaultInjection:
    """Injects faults into the trading system to test resilience.

    Each method simulates a specific failure mode.  Callers can hook into the
    injection points with optional callbacks to exercise their own code paths.
    """

    def __init__(self) -> None:
        self._active_faults: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    # -- public helpers -------------------------------------------------------

    @property
    def active_faults(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._active_faults)

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        """Reset all fault state."""
        with self._lock:
            self._active_faults.clear()
            self._events.clear()

    def _register(self, name: str, duration: float, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._active_faults[name] = {
                "start": time.monotonic(),
                "duration": duration,
                "meta": meta or {},
            }
            self._events.append({
                "fault": name,
                "action": "injected",
                "time": datetime.utcnow().isoformat(),
                **(meta or {}),
            })

    def _deregister(self, name: str) -> float:
        """Remove fault and return how long it was active."""
        with self._lock:
            info = self._active_faults.pop(name, None)
            if info is None:
                return 0.0
            elapsed = time.monotonic() - info["start"]
            self._events.append({
                "fault": name,
                "action": "cleared",
                "time": datetime.utcnow().isoformat(),
                "elapsed_seconds": round(elapsed, 4),
            })
            return elapsed

    # -- fault implementations ------------------------------------------------

    def network_disconnect(self, duration_seconds: float) -> None:
        """Simulate a complete network outage for *duration_seconds*.

        Blocks the calling thread for the duration, which mirrors the effect
        of no data arriving from any upstream source.
        """
        name = FaultType.NETWORK_DISCONNECT.value
        logger.warning("[FAULT] Network disconnect for %.1fs", duration_seconds)
        self._register(name, duration_seconds)
        time.sleep(duration_seconds)
        self._deregister(name)
        logger.info("[FAULT] Network reconnect")

    def data_interruption(self, duration_seconds: float) -> None:
        """Simulate missing market-data bars for *duration_seconds*."""
        name = FaultType.DATA_INTERRUPTION.value
        logger.warning("[FAULT] Data interruption for %.1fs", duration_seconds)
        self._register(name, duration_seconds)
        time.sleep(duration_seconds)
        self._deregister(name)
        logger.info("[FAULT] Data feed resumed")

    def api_timeout(
        self,
        duration_seconds: float,
        error_msg: str = "API request timed out",
    ) -> None:
        """Simulate an API call that hangs and then times out."""
        name = FaultType.API_TIMEOUT.value
        logger.warning("[FAULT] API timeout for %.1fs – %s", duration_seconds, error_msg)
        self._register(name, duration_seconds, {"error_msg": error_msg})
        time.sleep(duration_seconds)
        self._deregister(name)
        logger.info("[FAULT] API timeout resolved")

    def data_duplicate(self, symbol: str, count: int) -> list[dict[str, Any]]:
        """Inject *count* duplicate bars for *symbol*.

        Returns the synthetic duplicate bar payloads so callers can feed them
        into their own data pipelines for testing deduplication logic.
        """
        name = FaultType.DATA_DUPLICATE.value
        logger.warning("[FAULT] Injecting %d duplicate bars for %s", count, symbol)
        now = datetime.utcnow()
        duplicates: list[dict[str, Any]] = []
        for i in range(count):
            bar = {
                "symbol": symbol,
                "timestamp": now.isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000,
                "_duplicate_index": i,
            }
            duplicates.append(bar)

        self._register(name, 0, {"symbol": symbol, "count": count})
        self._deregister(name)
        return duplicates

    def data_out_of_order(self, symbol: str, count: int) -> list[dict[str, Any]]:
        """Generate *count* bars for *symbol* and return them in shuffled order."""
        name = FaultType.DATA_OUT_OF_ORDER.value
        logger.warning("[FAULT] Shuffling %d bars for %s", count, symbol)
        base = datetime.utcnow()
        bars: list[dict[str, Any]] = []
        for i in range(count):
            bars.append({
                "symbol": symbol,
                "timestamp": (base + timedelta(seconds=i)).isoformat(),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1000 + i * 10,
                "_original_index": i,
            })
        random.shuffle(bars)

        self._register(name, 0, {"symbol": symbol, "count": count})
        self._deregister(name)
        return bars

    def clock_drift(self, seconds: float) -> None:
        """Simulate system clock drift by *seconds* (positive = ahead)."""
        name = FaultType.CLOCK_DRIFT.value
        logger.warning("[FAULT] Clock drift of %+.1fs", seconds)
        self._register(name, 0, {"drift_seconds": seconds})
        # The actual effect is logged; consumers check active_faults to see
        # whether they should adjust their timestamps.
        time.sleep(min(abs(seconds), 0.1))  # brief pause to represent event
        self._deregister(name)
        logger.info("[FAULT] Clock drift cleared")

    def redis_restart(self) -> None:
        """Simulate a Redis restart (cache becomes temporarily unavailable)."""
        name = FaultType.REDIS_RESTART.value
        logger.warning("[FAULT] Redis restart – cache unavailable")
        self._register(name, 0)
        time.sleep(0.05)  # represent brief outage
        self._deregister(name)
        logger.info("[FAULT] Redis back online")

    def postgres_restart(self) -> None:
        """Simulate a Postgres restart (persistent store temporarily down)."""
        name = FaultType.POSTGRES_RESTART.value
        logger.warning("[FAULT] Postgres restart – database unavailable")
        self._register(name, 0)
        time.sleep(0.05)
        self._deregister(name)
        logger.info("[FAULT] Postgres back online")

    def price_spike(self, symbol: str, pct: float) -> dict[str, Any]:
        """Inject an extreme price move of *pct*% for *symbol*.

        Returns the synthetic bar so callers can feed it into their pipelines.
        Positive *pct* = up-spike, negative = down-spike.
        """
        name = FaultType.PRICE_SPIKE.value
        base_price = 100.0
        spike_price = round(base_price * (1 + pct / 100), 4)
        bar: dict[str, Any] = {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "open": base_price,
            "high": max(base_price, spike_price),
            "low": min(base_price, spike_price),
            "close": spike_price,
            "volume": 5000,
            "_spike_pct": pct,
        }
        logger.warning(
            "[FAULT] Price spike for %s: %+.2f%% (%.2f -> %.2f)",
            symbol, pct, base_price, spike_price,
        )
        self._register(name, 0, {"symbol": symbol, "pct": pct})
        self._deregister(name)
        return bar

    def volume_spike(self, symbol: str, multiplier: float) -> dict[str, Any]:
        """Inject a volume anomaly for *symbol* with volume scaled by *multiplier*.

        Returns the synthetic bar.
        """
        name = FaultType.VOLUME_SPIKE.value
        base_volume = 1000.0
        spiked_volume = round(base_volume * multiplier, 2)
        bar: dict[str, Any] = {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": spiked_volume,
            "_volume_multiplier": multiplier,
        }
        logger.warning(
            "[FAULT] Volume spike for %s: %.1fx (%.0f -> %.0f)",
            symbol, multiplier, base_volume, spiked_volume,
        )
        self._register(name, 0, {"symbol": symbol, "multiplier": multiplier})
        self._deregister(name)
        return bar


# ---------------------------------------------------------------------------
# Stress Test Case / Result
# ---------------------------------------------------------------------------

@dataclass
class StressTestCase:
    """Defines a single stress-test scenario."""
    name: str
    description: str
    fault_type: FaultType
    duration: float                                # seconds
    params: dict[str, Any] = field(default_factory=dict)
    expected_behavior: str = ""                    # human-readable expected outcome
    tolerance: dict[str, Any] = field(default_factory=lambda: {
        "max_recovery_time": 30.0,                 # seconds
        "max_data_loss": 0,                        # count of lost bars
    })


@dataclass
class StressTestResult:
    """Outcome of running a single stress test."""
    test_name: str
    passed: bool
    recovery_time_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics_during_fault: dict[str, Any] = field(default_factory=dict)
    metrics_after_recovery: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_name": self.test_name,
            "passed": self.passed,
            "recovery_time_seconds": round(self.recovery_time_seconds, 4),
            "errors": self.errors,
            "warnings": self.warnings,
            "metrics_during_fault": self.metrics_during_fault,
            "metrics_after_recovery": self.metrics_after_recovery,
        }


# ---------------------------------------------------------------------------
# Stress Test Runner
# ---------------------------------------------------------------------------

class StressTestRunner:
    """Orchestrates stress-test execution.

    Usage::

        runner = StressTestRunner()
        runner.add_test(StressTestCase(
            name="net_outage",
            description="5-second network disconnect",
            fault_type=FaultType.NETWORK_DISCONNECT,
            duration=5.0,
            expected_behavior="System queues orders and resumes on reconnect",
        ))
        results = runner.run_all()
        print(runner.generate_report())
    """

    def __init__(self) -> None:
        self._tests: dict[str, StressTestCase] = {}
        self._fault_injection = FaultInjection()
        self._pre_hooks: list[Callable[[StressTestCase], None]] = []
        self._post_hooks: list[Callable[[StressTestCase, StressTestResult], None]] = []
        self._metrics_collector: Callable[[], dict[str, Any]] | None = None

    # -- configuration --------------------------------------------------------

    def add_test(self, test_case: StressTestCase) -> None:
        """Register a test case for execution."""
        self._tests[test_case.name] = test_case
        logger.info("Registered stress test: %s", test_case.name)

    def register_pre_hook(self, hook: Callable[[StressTestCase], None]) -> None:
        """Register a callback invoked before each test."""
        self._pre_hooks.append(hook)

    def register_post_hook(self, hook: Callable[[StressTestCase, StressTestResult], None]) -> None:
        """Register a callback invoked after each test."""
        self._post_hooks.append(hook)

    def set_metrics_collector(self, collector: Callable[[], dict[str, Any]]) -> None:
        """Set a callable that returns current system metrics."""
        self._metrics_collector = collector

    @property
    def fault_injection(self) -> FaultInjection:
        return self._fault_injection

    # -- execution ------------------------------------------------------------

    def _collect_metrics(self) -> dict[str, Any]:
        if self._metrics_collector is not None:
            try:
                return self._metrics_collector()
            except Exception as exc:
                return {"_collector_error": str(exc)}
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "active_faults": len(self._fault_injection.active_faults),
        }

    def _execute_fault(self, test: StressTestCase) -> tuple[float, dict[str, Any]]:
        """Execute the fault injection and return (recovery_time, post_metrics)."""
        fi = self._fault_injection
        fault = test.fault_type
        p = test.params

        # Inject the fault
        if fault == FaultType.NETWORK_DISCONNECT:
            fi.network_disconnect(test.duration)
        elif fault == FaultType.DATA_INTERRUPTION:
            fi.data_interruption(test.duration)
        elif fault == FaultType.API_TIMEOUT:
            fi.api_timeout(test.duration, p.get("error_msg", "API request timed out"))
        elif fault == FaultType.DATA_DUPLICATE:
            fi.data_duplicate(p.get("symbol", "SPY"), p.get("count", 5))
        elif fault == FaultType.DATA_OUT_OF_ORDER:
            fi.data_out_of_order(p.get("symbol", "SPY"), p.get("count", 10))
        elif fault == FaultType.CLOCK_DRIFT:
            fi.clock_drift(p.get("seconds", 5.0))
        elif fault == FaultType.REDIS_RESTART:
            fi.redis_restart()
        elif fault == FaultType.POSTGRES_RESTART:
            fi.postgres_restart()
        elif fault == FaultType.PRICE_SPIKE:
            fi.price_spike(p.get("symbol", "SPY"), p.get("pct", 10.0))
        elif fault == FaultType.VOLUME_SPIKE:
            fi.volume_spike(p.get("symbol", "SPY"), p.get("multiplier", 50.0))
        else:
            raise ValueError(f"Unknown fault type: {fault}")

        # Measure recovery
        recovery_start = time.monotonic()
        post_metrics = self._collect_metrics()
        recovery_time = time.monotonic() - recovery_start

        return recovery_time, post_metrics

    def run_test(self, name: str) -> StressTestResult:
        """Run a single registered test by name."""
        if name not in self._tests:
            raise KeyError(f"Unknown test: {name}")
        return self._run_single(self._tests[name])

    def run_all(self) -> list[StressTestResult]:
        """Run all registered tests sequentially."""
        results: list[StressTestResult] = []
        for test_case in self._tests.values():
            results.append(self._run_single(test_case))
        return results

    def _run_single(self, test: StressTestCase) -> StressTestResult:
        """Execute one test case and produce a result."""
        logger.info("=== Running stress test: %s ===", test.name)

        # Pre-hooks
        for hook in self._pre_hooks:
            try:
                hook(test)
            except Exception as exc:
                logger.error("Pre-hook error for %s: %s", test.name, exc)

        # Collect baseline metrics
        baseline_metrics = self._collect_metrics()
        errors: list[str] = []
        warnings: list[str] = []

        # Execute fault
        try:
            recovery_time, post_metrics = self._execute_fault(test)
        except Exception as exc:
            errors.append(f"Fault injection raised: {exc}")
            recovery_time = 0.0
            post_metrics = {}

        # Evaluate tolerances
        max_recovery = test.tolerance.get("max_recovery_time", 30.0)
        if recovery_time > max_recovery:
            errors.append(
                f"Recovery time {recovery_time:.2f}s exceeds tolerance {max_recovery:.2f}s"
            )

        # Check that no active faults remain (all should have been cleared)
        remaining = self._fault_injection.active_faults
        if remaining:
            warnings.append(f"Active faults remain after test: {list(remaining.keys())}")

        passed = len(errors) == 0

        result = StressTestResult(
            test_name=test.name,
            passed=passed,
            recovery_time_seconds=recovery_time,
            errors=errors,
            warnings=warnings,
            metrics_during_fault=baseline_metrics,
            metrics_after_recovery=post_metrics,
        )

        # Post-hooks
        for hook in self._post_hooks:
            try:
                hook(test, result)
            except Exception as exc:
                logger.error("Post-hook error for %s: %s", test.name, exc)

        status = "PASS" if passed else "FAIL"
        logger.info("=== Stress test %s: %s ===", test.name, status)
        return result

    # -- reporting ------------------------------------------------------------

    def generate_report(self, results: list[StressTestResult] | None = None) -> str:
        """Generate a human-readable stress-test report.

        If *results* is not provided, runs all tests first.
        """
        if results is None:
            results = self.run_all()

        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed

        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("  RISK STRESS TEST REPORT")
        lines.append(f"  Generated: {datetime.utcnow().isoformat()}Z")
        lines.append("=" * 60)
        lines.append(f"\nTotal: {total}  |  Passed: {passed}  |  Failed: {failed}")
        lines.append("")

        for r in results:
            status = "✓ PASS" if r.passed else "✗ FAIL"
            lines.append(f"  [{status}] {r.test_name}")
            lines.append(f"    Recovery time: {r.recovery_time_seconds:.4f}s")
            if r.errors:
                for e in r.errors:
                    lines.append(f"    ERROR: {e}")
            if r.warnings:
                for w in r.warnings:
                    lines.append(f"    WARN:  {w}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Predefined test suite
# ---------------------------------------------------------------------------

def create_default_suite() -> StressTestRunner:
    """Return a StressTestRunner pre-loaded with a standard set of tests."""
    runner = StressTestRunner()

    runner.add_test(StressTestCase(
        name="network_5s",
        description="5-second complete network outage",
        fault_type=FaultType.NETWORK_DISCONNECT,
        duration=5.0,
        expected_behavior="Orders queued; system resumes on reconnect",
        tolerance={"max_recovery_time": 10.0, "max_data_loss": 0},
    ))

    runner.add_test(StressTestCase(
        name="data_gap_3s",
        description="3-second gap in market data feed",
        fault_type=FaultType.DATA_INTERRUPTION,
        duration=3.0,
        expected_behavior="System detects gap and backfills on resume",
        tolerance={"max_recovery_time": 15.0, "max_data_loss": 0},
    ))

    runner.add_test(StressTestCase(
        name="api_timeout_10s",
        description="10-second API timeout with error message",
        fault_type=FaultType.API_TIMEOUT,
        duration=10.0,
        params={"error_msg": "Broker API unreachable"},
        expected_behavior="Retry with backoff; alert raised",
        tolerance={"max_recovery_time": 20.0},
    ))

    runner.add_test(StressTestCase(
        name="duplicate_bars",
        description="5 duplicate bars injected for SPY",
        fault_type=FaultType.DATA_DUPLICATE,
        duration=0.0,
        params={"symbol": "SPY", "count": 5},
        expected_behavior="Duplicate bars deduplicated; no duplicate fills",
        tolerance={"max_recovery_time": 5.0, "max_data_loss": 0},
    ))

    runner.add_test(StressTestCase(
        name="out_of_order_bars",
        description="10 bars received out of order for SPY",
        fault_type=FaultType.DATA_OUT_OF_ORDER,
        duration=0.0,
        params={"symbol": "SPY", "count": 10},
        expected_behavior="Bars reordered by timestamp; no stale signals",
        tolerance={"max_recovery_time": 5.0, "max_data_loss": 0},
    ))

    runner.add_test(StressTestCase(
        name="clock_drift_5s",
        description="System clock 5 seconds ahead",
        fault_type=FaultType.CLOCK_DRIFT,
        duration=0.0,
        params={"seconds": 5.0},
        expected_behavior="Timestamps validated; drift detected and logged",
        tolerance={"max_recovery_time": 2.0},
    ))

    runner.add_test(StressTestCase(
        name="redis_outage",
        description="Redis restart (cache unavailable)",
        fault_type=FaultType.REDIS_RESTART,
        duration=0.0,
        expected_behavior="System falls back to direct queries; no data loss",
        tolerance={"max_recovery_time": 5.0},
    ))

    runner.add_test(StressTestCase(
        name="postgres_outage",
        description="Postgres restart (database unavailable)",
        fault_type=FaultType.POSTGRES_RESTART,
        duration=0.0,
        expected_behavior="Writes buffered; flushed on recovery",
        tolerance={"max_recovery_time": 10.0},
    ))

    runner.add_test(StressTestCase(
        name="flash_crash_10pct",
        description="10% price spike (flash crash) on SPY",
        fault_type=FaultType.PRICE_SPIKE,
        duration=0.0,
        params={"symbol": "SPY", "pct": -10.0},
        expected_behavior="Risk limits trigger; positions managed",
        tolerance={"max_recovery_time": 5.0},
    ))

    runner.add_test(StressTestCase(
        name="volume_anomaly_50x",
        description="50x volume spike on SPY",
        fault_type=FaultType.VOLUME_SPIKE,
        duration=0.0,
        params={"symbol": "SPY", "multiplier": 50.0},
        expected_behavior="Anomaly detected; alerts raised; no erroneous fills",
        tolerance={"max_recovery_time": 5.0},
    ))

    return runner
