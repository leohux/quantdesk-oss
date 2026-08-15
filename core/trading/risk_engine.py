"""Pre-trade Risk Engine - all orders must pass risk checks before submission."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.trading.order_manager import Order, OrderSide
from core.trading.portfolio import PortfolioManager

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    """Configurable risk limits."""
    max_position_pct: float = 20.0       # Max % of equity in single position
    max_daily_loss_pct: float = 5.0      # Max daily loss before halting
    max_drawdown_pct: float = 20.0       # Max drawdown from peak equity
    max_exposure_pct: float = 100.0      # Max total exposure
    min_cash_pct: float = 5.0            # Minimum cash reserve
    max_order_value_pct: float = 25.0    # Max single order as % of equity
    max_open_orders: int = 10            # Max concurrent active orders


@dataclass
class RiskCheckResult:
    passed: bool
    reject_reason: str = ""
    warnings: list[str] | None = None

    def __bool__(self) -> bool:
        return self.passed


class RiskEngine:
    """Pre-trade risk checking engine.

    Every order must pass through this before reaching the broker.
    """

    def __init__(
        self,
        portfolio: PortfolioManager,
        limits: RiskLimits | None = None,
    ) -> None:
        self._portfolio = portfolio
        self.limits = limits or RiskLimits()
        self._peak_equity: float = portfolio.equity
        self._daily_start_equity: float = portfolio.equity
        self._halted: bool = False
        self._halt_reason: str = ""
        self._rejected_count: int = 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def update_peak(self) -> None:
        """Update peak equity for drawdown calculation."""
        current = self._portfolio.equity
        if current > self._peak_equity:
            self._peak_equity = current

    def reset_daily(self) -> None:
        """Reset daily tracking (call at market open)."""
        self._daily_start_equity = self._portfolio.equity
        self._halted = False
        self._halt_reason = ""

    def check_order(
        self,
        order: Order,
        current_price: float,
        active_orders_count: int = 0,
    ) -> RiskCheckResult:
        """Run all pre-trade risk checks on an order."""
        warnings: list[str] = []

        # 0. Halted?
        if self._halted:
            return RiskCheckResult(False, f"Trading halted: {self._halt_reason}")

        # 1. Daily loss check
        equity = self._portfolio.equity
        daily_pnl = equity - self._daily_start_equity
        daily_loss_pct = abs(daily_pnl / self._daily_start_equity * 100) if self._daily_start_equity > 0 and daily_pnl < 0 else 0
        if daily_loss_pct >= self.limits.max_daily_loss_pct:
            self._halted = True
            self._halt_reason = f"Daily loss limit hit: {daily_loss_pct:.1f}%"
            logger.warning("RISK HALT: %s", self._halt_reason)
            return RiskCheckResult(False, self._halt_reason)

        # 2. Max drawdown check
        drawdown_pct = ((self._peak_equity - equity) / self._peak_equity * 100) if self._peak_equity > 0 else 0
        if drawdown_pct >= self.limits.max_drawdown_pct:
            self._halted = True
            self._halt_reason = f"Max drawdown hit: {drawdown_pct:.1f}%"
            logger.warning("RISK HALT: %s", self._halt_reason)
            return RiskCheckResult(False, self._halt_reason)

        # 3. Max open orders
        if active_orders_count >= self.limits.max_open_orders:
            return RiskCheckResult(False, f"Max open orders ({self.limits.max_open_orders}) reached")

        # Buy-specific checks
        if order.side == OrderSide.BUY:
            order_value = order.qty * current_price

            # 4. Sufficient cash
            if order_value > self._portfolio.cash:
                return RiskCheckResult(False, f"Insufficient cash: need ${order_value:,.2f}, have ${self._portfolio.cash:,.2f}")

            # 5. Single order size limit
            order_pct = (order_value / equity * 100) if equity > 0 else 0
            if order_pct > self.limits.max_order_value_pct:
                return RiskCheckResult(False, f"Order too large: {order_pct:.1f}% (max {self.limits.max_order_value_pct}%)")

            # 6. Position concentration
            pos = self._portfolio.get_position(order.symbol)
            current_pos_value = pos.market_value if pos else 0
            new_pos_value = current_pos_value + order_value
            pos_pct = (new_pos_value / equity * 100) if equity > 0 else 0
            if pos_pct > self.limits.max_position_pct:
                return RiskCheckResult(False, f"Position concentration: {pos_pct:.1f}% (max {self.limits.max_position_pct}%)")

            # 7. Total exposure
            new_invested = self._portfolio.invested + order_value
            exposure_pct = (new_invested / equity * 100) if equity > 0 else 0
            if exposure_pct > self.limits.max_exposure_pct:
                return RiskCheckResult(False, f"Exposure limit: {exposure_pct:.1f}% (max {self.limits.max_exposure_pct}%)")

            # 8. Minimum cash reserve
            remaining_cash = self._portfolio.cash - order_value
            cash_pct = (remaining_cash / equity * 100) if equity > 0 else 0
            if cash_pct < self.limits.min_cash_pct:
                return RiskCheckResult(False, f"Cash reserve: {cash_pct:.1f}% (min {self.limits.min_cash_pct}%)")

            # Warnings
            if pos_pct > self.limits.max_position_pct * 0.8:
                warnings.append(f"Position approaching limit: {pos_pct:.1f}%")

        # Sell-specific checks
        elif order.side == OrderSide.SELL:
            pos = self._portfolio.get_position(order.symbol)
            if pos is None or pos.qty < order.qty:
                have = pos.qty if pos else 0
                return RiskCheckResult(False, f"Insufficient position: {order.symbol} (have {have}, selling {order.qty})")

        self._rejected_count = 0  # Reset on success
        return RiskCheckResult(True, warnings=warnings if warnings else None)

    def get_status(self) -> dict[str, Any]:
        equity = self._portfolio.equity
        drawdown_pct = ((self._peak_equity - equity) / self._peak_equity * 100) if self._peak_equity > 0 else 0
        daily_pnl = equity - self._daily_start_equity
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "peak_equity": round(self._peak_equity, 2),
            "current_drawdown_pct": round(drawdown_pct, 2),
            "daily_pnl": round(daily_pnl, 2),
            "limits": {
                "max_position_pct": self.limits.max_position_pct,
                "max_daily_loss_pct": self.limits.max_daily_loss_pct,
                "max_drawdown_pct": self.limits.max_drawdown_pct,
                "max_exposure_pct": self.limits.max_exposure_pct,
                "min_cash_pct": self.limits.min_cash_pct,
            },
        }
