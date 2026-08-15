"""Strategy Runner - manages multiple independent strategy instances.

Each strategy runs independently with its own signal state, lifecycle,
and health tracking. Thread-safe for concurrent access.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

import pandas as pd

from core.strategy.engine import run_signal_fn

logger = logging.getLogger(__name__)


class StrategyState(str, Enum):
    """Lifecycle states for a strategy instance."""
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


# Valid state transitions
_TRANSITIONS: dict[StrategyState, set[StrategyState]] = {
    StrategyState.IDLE: {StrategyState.STARTING},
    StrategyState.STARTING: {StrategyState.RUNNING, StrategyState.ERROR},
    StrategyState.RUNNING: {StrategyState.PAUSING, StrategyState.STOPPING, StrategyState.ERROR},
    StrategyState.PAUSING: {StrategyState.PAUSED, StrategyState.ERROR},
    StrategyState.PAUSED: {StrategyState.RUNNING, StrategyState.STOPPING, StrategyState.ERROR},
    StrategyState.STOPPING: {StrategyState.STOPPED, StrategyState.ERROR},
    StrategyState.STOPPED: {StrategyState.IDLE},
    StrategyState.ERROR: {StrategyState.IDLE, StrategyState.STARTING},
}


@dataclass
class StrategyInstance:
    """Represents a single strategy with its configuration and runtime state."""
    id: str
    name: str
    code: str
    params: dict[str, Any]
    symbols: list[str]
    sizing_pct: float
    state: StrategyState = StrategyState.IDLE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    stats: dict[str, Any] = field(default_factory=lambda: {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "signals_generated": 0,
        "bars_processed": 0,
        "errors": 0,
    })


@dataclass
class StrategyHealthCheck:
    """Health monitoring for a strategy instance."""
    strategy_id: str
    last_signal_time: datetime | None = None
    last_bar_time: datetime | None = None
    error_count: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    auto_restart: bool = True
    max_consecutive_errors: int = 5

    def check(self, strategy_id: str) -> dict[str, Any]:
        """Return health status dict for the strategy."""
        now = datetime.now(timezone.utc)
        stale_seconds = None
        if self.last_bar_time:
            stale_seconds = (now - self.last_bar_time).total_seconds()

        healthy = (
            self.consecutive_errors < self.max_consecutive_errors
            and (stale_seconds is None or stale_seconds < 300)
        )
        return {
            "strategy_id": strategy_id,
            "healthy": healthy,
            "last_signal_time": self.last_signal_time.isoformat() if self.last_signal_time else None,
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "error_count": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "stale_seconds": stale_seconds,
            "auto_restart": self.auto_restart,
        }

    def record_success(self) -> None:
        self.consecutive_errors = 0
        self.last_bar_time = datetime.now(timezone.utc)

    def record_signal(self) -> None:
        self.last_signal_time = datetime.now(timezone.utc)

    def record_error(self, error: str) -> None:
        self.error_count += 1
        self.consecutive_errors += 1
        self.last_error = error


class StrategyRunner:
    """Manages multiple independent strategy instances with lifecycle control."""

    def __init__(self, on_signal: Callable[[str, str, pd.Series, pd.Series], None] | None = None) -> None:
        self._lock = threading.RLock()
        self._strategies: dict[str, StrategyInstance] = {}
        self._health: dict[str, StrategyHealthCheck] = {}
        self._compiled: dict[str, Any] = {}  # cached compiled strategy code
        self._signal_buffers: dict[str, dict[str, dict[str, pd.Series]]] = {}  # strategy_id -> symbol -> {entries, exits}
        self._on_signal = on_signal

    def _transition(self, inst: StrategyInstance, new_state: StrategyState) -> None:
        allowed = _TRANSITIONS.get(inst.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Strategy {inst.id}: cannot transition from {inst.state.value} to {new_state.value}"
            )
        inst.state = new_state

    def add_strategy(
        self,
        name: str,
        code: str,
        params: dict[str, Any],
        symbols: list[str],
        sizing_pct: float,
        auto_restart: bool = True,
    ) -> StrategyInstance:
        """Add and register a new strategy (starts in IDLE state)."""
        with self._lock:
            sid = uuid.uuid4().hex[:12]
            inst = StrategyInstance(
                id=sid,
                name=name,
                code=code,
                params=params,
                symbols=list(symbols),
                sizing_pct=sizing_pct,
            )
            self._strategies[sid] = inst
            self._health[sid] = StrategyHealthCheck(strategy_id=sid, auto_restart=auto_restart)
            self._signal_buffers[sid] = {}
            logger.info("Strategy added: %s (%s)", name, sid)
            return inst

    def remove_strategy(self, strategy_id: str) -> None:
        """Remove a strategy. Must be STOPPED or IDLE or ERROR."""
        with self._lock:
            inst = self._get_inst(strategy_id)
            if inst.state not in (StrategyState.IDLE, StrategyState.STOPPED, StrategyState.ERROR):
                raise ValueError(f"Cannot remove strategy in state {inst.state.value}")
            del self._strategies[strategy_id]
            self._health.pop(strategy_id, None)
            self._signal_buffers.pop(strategy_id, None)
            self._compiled.pop(strategy_id, None)
            logger.info("Strategy removed: %s", strategy_id)

    def start_strategy(self, strategy_id: str) -> None:
        """Compile and start a strategy."""
        with self._lock:
            inst = self._get_inst(strategy_id)
            self._transition(inst, StrategyState.STARTING)
            try:
                # Pre-compile and validate the strategy code
                fn = run_signal_fn(pd.Series(dtype=float), inst.code, inst.params)
                self._compiled[strategy_id] = inst.code  # store code for later use
                self._transition(inst, StrategyState.RUNNING)
                inst.started_at = datetime.now(timezone.utc)
                inst.stopped_at = None
                logger.info("Strategy started: %s", strategy_id)
            except Exception as exc:
                self._transition(inst, StrategyState.ERROR)
                self._health[strategy_id].record_error(str(exc))
                logger.error("Strategy start failed: %s - %s", strategy_id, exc)
                raise

    def stop_strategy(self, strategy_id: str) -> None:
        """Stop a running or paused strategy."""
        with self._lock:
            inst = self._get_inst(strategy_id)
            if inst.state in (StrategyState.RUNNING, StrategyState.PAUSED):
                self._transition(inst, StrategyState.STOPPING)
                self._transition(inst, StrategyState.STOPPED)
                inst.stopped_at = datetime.now(timezone.utc)
                logger.info("Strategy stopped: %s", strategy_id)
            elif inst.state == StrategyState.STOPPED:
                return  # already stopped
            else:
                raise ValueError(f"Cannot stop strategy in state {inst.state.value}")

    def pause_strategy(self, strategy_id: str) -> None:
        """Pause a running strategy (it will skip bars until resumed)."""
        with self._lock:
            inst = self._get_inst(strategy_id)
            self._transition(inst, StrategyState.PAUSING)
            self._transition(inst, StrategyState.PAUSED)
            logger.info("Strategy paused: %s", strategy_id)

    def resume_strategy(self, strategy_id: str) -> None:
        """Resume a paused strategy."""
        with self._lock:
            inst = self._get_inst(strategy_id)
            self._transition(inst, StrategyState.RUNNING)
            logger.info("Strategy resumed: %s", strategy_id)

    def get_strategy(self, strategy_id: str) -> StrategyInstance:
        """Get a strategy instance by ID."""
        with self._lock:
            return self._get_inst(strategy_id)

    def list_strategies(self) -> list[dict[str, Any]]:
        """List all strategy instances with their current state."""
        with self._lock:
            result = []
            for inst in self._strategies.values():
                health = self._health.get(inst.id)
                result.append({
                    "id": inst.id,
                    "name": inst.name,
                    "state": inst.state.value,
                    "symbols": inst.symbols,
                    "sizing_pct": inst.sizing_pct,
                    "created_at": inst.created_at.isoformat(),
                    "started_at": inst.started_at.isoformat() if inst.started_at else None,
                    "stopped_at": inst.stopped_at.isoformat() if inst.stopped_at else None,
                    "stats": dict(inst.stats),
                    "healthy": health.check(inst.id)["healthy"] if health else False,
                })
            return result

    def process_bar(self, bar: dict[str, Any]) -> None:
        """Route a bar to all running strategies subscribed to its symbol.

        bar must have at least: symbol, close (as pd.Series or scalar).
        If close is a scalar, it's wrapped in a Series; the strategy accumulates
        bars over time via the signal buffer.
        """
        symbol = bar.get("symbol")
        if not symbol:
            raise ValueError("Bar must contain 'symbol'")

        close_val = bar.get("close")
        if close_val is None:
            raise ValueError("Bar must contain 'close'")

        with self._lock:
            for sid, inst in self._strategies.items():
                if inst.state != StrategyState.RUNNING:
                    continue
                if symbol not in inst.symbols:
                    continue

                try:
                    self._process_bar_for_strategy(inst, symbol, close_val)
                except Exception as exc:
                    self._handle_strategy_error(inst, exc)

    def _process_bar_for_strategy(
        self, inst: StrategyInstance, symbol: str, close_val: Any
    ) -> None:
        """Process a bar for a single strategy."""
        health = self._health[inst.id]
        buffers = self._signal_buffers[inst.id]

        # Accumulate close prices for this symbol
        if symbol not in buffers:
            buffers[symbol] = {"close": pd.Series(dtype=float)}

        buf = buffers[symbol]
        if isinstance(close_val, pd.Series):
            buf["close"] = pd.concat([buf["close"], close_val]).tail(500)
        else:
            new_point = pd.Series([float(close_val)])
            buf["close"] = pd.concat([buf["close"], new_point]).tail(500)

        # Need at least a few bars to generate signals
        if len(buf["close"]) < 2:
            health.record_success()
            inst.stats["bars_processed"] += 1
            return

        # Run strategy signal generation
        entries, exits = run_signal_fn(buf["close"], inst.code, inst.params)
        health.record_success()
        inst.stats["bars_processed"] += 1

        # Check for new signals on the latest bar
        has_entry = bool(entries.iloc[-1]) if len(entries) > 0 else False
        has_exit = bool(exits.iloc[-1]) if len(exits) > 0 else False

        if has_entry or has_exit:
            inst.stats["signals_generated"] += 1
            health.record_signal()

            if self._on_signal:
                try:
                    self._on_signal(inst.id, symbol, entries, exits)
                except Exception as exc:
                    logger.error("on_signal callback error for %s: %s", inst.id, exc)

    def _handle_strategy_error(self, inst: StrategyInstance, exc: Exception) -> None:
        """Handle an error during strategy processing."""
        health = self._health[inst.id]
        health.record_error(str(exc))
        inst.stats["errors"] += 1
        logger.error("Strategy %s error: %s", inst.id, exc)

        # Auto-restart if configured and too many consecutive errors
        if health.auto_restart and health.consecutive_errors >= health.max_consecutive_errors:
            logger.warning(
                "Strategy %s hit %d consecutive errors, restarting",
                inst.id, health.consecutive_errors,
            )
            try:
                self._transition(inst, StrategyState.ERROR)
                # Reset and attempt restart
                health.consecutive_errors = 0
                self._transition(inst, StrategyState.IDLE)
                self._transition(inst, StrategyState.STARTING)
                self._transition(inst, StrategyState.RUNNING)
                inst.started_at = datetime.now(timezone.utc)
                logger.info("Strategy %s auto-restarted successfully", inst.id)
            except Exception as restart_exc:
                logger.error("Strategy %s auto-restart failed: %s", inst.id, restart_exc)

    def get_health(self, strategy_id: str) -> dict[str, Any]:
        """Get health status for a strategy."""
        with self._lock:
            if strategy_id not in self._health:
                raise KeyError(f"Strategy not found: {strategy_id}")
            return self._health[strategy_id].check(strategy_id)

    def _get_inst(self, strategy_id: str) -> StrategyInstance:
        if strategy_id not in self._strategies:
            raise KeyError(f"Strategy not found: {strategy_id}")
        return self._strategies[strategy_id]
