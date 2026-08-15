"""Hard risk limits that automatically stop trading when breached.

QuantDesk v1.0.0 — Phase 3 Trading Validation
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Try importing audit logger; degrade gracefully if DB unavailable
try:
    from core.audit import log_event
except Exception:  # pragma: no cover
    def log_event(user: str, action: str, detail: str = "", ip: str = "") -> None:
        log.warning("audit unavailable — log_event(%s, %s, %s)", user, action, detail)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUDIT_USER = "kill_switch"
HALT_ACTION = "KILL_SWITCH_HALT"
RESUME_ACTION = "KILL_SWITCH_RESUME"


@dataclass
class _Limits:
    """Configurable hard limits with sensible defaults."""
    daily_max_loss_pct: float = 5.0
    max_position_pct: float = 25.0
    max_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 10
    single_trade_risk_pct: float = 3.0
    max_open_positions: int = 20
    broker_error_threshold: int = 5


class KillSwitch:
    """Singleton kill-switch that enforces hard risk limits.

    Thread-safe: all state mutations are guarded by a ``threading.Lock``.
    """

    _instance: KillSwitch | None = None
    _init_lock = threading.Lock()

    def __new__(cls, **kwargs: Any) -> KillSwitch:
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialised = False
                    cls._instance = inst
        return cls._instance

    def __init__(self, **overrides: Any) -> None:
        if self._initialised:
            return
        self._lock = threading.Lock()

        # Limits
        self._limits = _Limits(**{
            k: v for k, v in overrides.items() if hasattr(_Limits, k)
        })

        # Mutable state
        self.daily_pnl: float = 0.0
        self.peak_equity: float = 0.0
        self.consecutive_losses: int = 0
        self.broker_errors: int = 0
        self.is_halted: bool = False
        self.halt_reason: str = ""
        self.last_trade_pnl: float = 0.0

        self._initialised = True
        log.info("KillSwitch initialised with limits: %s", self._limits)

    # ---- convenience accessors for limits ----
    @property
    def limits(self) -> _Limits:
        return self._limits

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        price: float,
        equity: float,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` — checks all limits before an order."""
        with self._lock:
            if self.is_halted:
                return False, f"Trading halted: {self.halt_reason}"

            if equity <= 0:
                return False, "Equity is zero or negative"

            # 1. Daily loss limit
            if equity > 0:
                daily_loss_pct = (-self.daily_pnl / equity) * 100
                if daily_loss_pct >= self._limits.daily_max_loss_pct:
                    self._halt(f"Daily loss {daily_loss_pct:.2f}% >= {self._limits.daily_max_loss_pct}%")
                    return False, self.halt_reason

            # 2. Single-trade risk limit
            trade_value = qty * price
            if equity > 0:
                risk_pct = (trade_value / equity) * 100
                if risk_pct > self._limits.single_trade_risk_pct:
                    return False, (
                        f"Single trade risk {risk_pct:.2f}% > "
                        f"{self._limits.single_trade_risk_pct}%"
                    )

            # 3. Position size limit
            if equity > 0:
                pos_pct = (trade_value / equity) * 100
                if pos_pct > self._limits.max_position_pct:
                    return False, (
                        f"Position size {pos_pct:.2f}% > "
                        f"{self._limits.max_position_pct}%"
                    )

            return True, "OK"

    def record_trade(self, pnl: float) -> None:
        """Update consecutive losses and daily P&L after a trade completes."""
        with self._lock:
            self.last_trade_pnl = pnl
            self.daily_pnl += pnl
            if pnl < 0:
                self.consecutive_losses += 1
                if self.consecutive_losses >= self._limits.max_consecutive_losses:
                    self._halt(
                        f"Consecutive losses {self.consecutive_losses} >= "
                        f"{self._limits.max_consecutive_losses}"
                    )
            else:
                self.consecutive_losses = 0

    def record_broker_error(self) -> None:
        """Increment consecutive broker error count; halt if threshold reached."""
        with self._lock:
            self.broker_errors += 1
            log.warning("Broker error #%d", self.broker_errors)
            if self.broker_errors >= self._limits.broker_error_threshold:
                self._halt(
                    f"Broker errors {self.broker_errors} >= "
                    f"{self._limits.broker_error_threshold}"
                )

    def record_broker_success(self) -> None:
        """Reset consecutive broker error counter."""
        with self._lock:
            self.broker_errors = 0

    def update_equity(self, equity: float) -> None:
        """Update peak equity and check drawdown."""
        with self._lock:
            if equity > self.peak_equity:
                self.peak_equity = equity

            if self.peak_equity > 0:
                drawdown_pct = ((self.peak_equity - equity) / self.peak_equity) * 100
                if drawdown_pct >= self._limits.max_drawdown_pct:
                    self._halt(
                        f"Drawdown {drawdown_pct:.2f}% >= "
                        f"{self._limits.max_drawdown_pct}%"
                    )

    def reset_daily(self) -> None:
        """Reset daily P&L — call at market open."""
        with self._lock:
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            log.info("KillSwitch daily counters reset")

    def halt(self, reason: str) -> None:
        """Manual halt with *reason*."""
        with self._lock:
            self._halt(reason)

    def resume(self) -> None:
        """Manual resume — clears halt state."""
        with self._lock:
            if self.is_halted:
                log.warning("KillSwitch RESUMED (was halted: %s)", self.halt_reason)
                log_event(AUDIT_USER, RESUME_ACTION, f"Resumed. Previous reason: {self.halt_reason}")
                self.is_halted = False
                self.halt_reason = ""
                self.broker_errors = 0
                self.consecutive_losses = 0

    def is_trading_allowed(self) -> bool:
        """Quick check: is trading currently permitted?"""
        with self._lock:
            return not self.is_halted

    def status(self) -> dict[str, Any]:
        """Return a snapshot of all current state."""
        with self._lock:
            return {
                "is_halted": self.is_halted,
                "halt_reason": self.halt_reason,
                "daily_pnl": self.daily_pnl,
                "peak_equity": self.peak_equity,
                "consecutive_losses": self.consecutive_losses,
                "broker_errors": self.broker_errors,
                "last_trade_pnl": self.last_trade_pnl,
                "limits": {
                    "daily_max_loss_pct": self._limits.daily_max_loss_pct,
                    "max_position_pct": self._limits.max_position_pct,
                    "max_drawdown_pct": self._limits.max_drawdown_pct,
                    "max_consecutive_losses": self._limits.max_consecutive_losses,
                    "single_trade_risk_pct": self._limits.single_trade_risk_pct,
                    "max_open_positions": self._limits.max_open_positions,
                    "broker_error_threshold": self._limits.broker_error_threshold,
                },
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _halt(self, reason: str) -> None:
        """Set halted state and log. **Must be called with ``_lock`` held.**"""
        if not self.is_halted:
            self.is_halted = True
            self.halt_reason = reason
            log.critical("⚠️  KILL SWITCH TRIGGERED: %s", reason)
            print(f"⚠️  KILL SWITCH TRIGGERED: {reason}")
            log_event(AUDIT_USER, HALT_ACTION, reason)

    @classmethod
    def _reset_singleton(cls) -> None:
        """Testing helper — clear the singleton so a fresh instance is created."""
        cls._instance = None
