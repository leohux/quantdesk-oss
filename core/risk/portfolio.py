"""Portfolio-level risk management."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortfolioLimits:
    """Portfolio-level risk limits."""
    max_position_pct: float = 20.0       # Max % of portfolio in single position
    max_sector_pct: float = 40.0         # Max % in single sector
    max_total_exposure_pct: float = 100.0  # Max total exposure
    max_correlated_positions: int = 3     # Max positions in correlated assets
    min_cash_pct: float = 5.0            # Minimum cash reserve %


@dataclass
class PortfolioState:
    """Current portfolio state for risk checks."""
    equity: float
    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # symbol -> market_value
    sectors: dict[str, float] = field(default_factory=dict)    # sector -> total_value


class PortfolioRiskManager:
    """Portfolio-level risk checks."""

    def __init__(self, limits: PortfolioLimits | None = None) -> None:
        self.limits = limits or PortfolioLimits()

    def check_new_position(
        self,
        state: PortfolioState,
        symbol: str,
        order_value: float,
        sector: str = "unknown",
    ) -> tuple[bool, list[str]]:
        """Check if a new position passes portfolio risk limits.

        Returns (allowed, list_of_reasons_if_rejected)
        """
        reasons = []

        # Check single position limit
        current_value = state.positions.get(symbol, 0)
        new_total = current_value + order_value
        position_pct = (new_total / state.equity) * 100 if state.equity else 0
        if position_pct > self.limits.max_position_pct:
            reasons.append(
                f"Position would be {position_pct:.1f}% (max {self.limits.max_position_pct}%)"
            )

        # Check sector limit
        sector_value = state.sectors.get(sector, 0) + order_value
        sector_pct = (sector_value / state.equity) * 100 if state.equity else 0
        if sector_pct > self.limits.max_sector_pct:
            reasons.append(
                f"Sector {sector} would be {sector_pct:.1f}% (max {self.limits.max_sector_pct}%)"
            )

        # Check total exposure
        total_invested = sum(state.positions.values()) + order_value
        exposure_pct = (total_invested / state.equity) * 100 if state.equity else 0
        if exposure_pct > self.limits.max_total_exposure_pct:
            reasons.append(
                f"Total exposure would be {exposure_pct:.1f}% (max {self.limits.max_total_exposure_pct}%)"
            )

        # Check minimum cash
        remaining_cash = state.cash - order_value
        cash_pct = (remaining_cash / state.equity) * 100 if state.equity else 0
        if cash_pct < self.limits.min_cash_pct:
            reasons.append(
                f"Cash would drop to {cash_pct:.1f}% (min {self.limits.min_cash_pct}%)"
            )

        return (len(reasons) == 0, reasons)

    def max_allowed_position_value(
        self,
        state: PortfolioState,
        symbol: str,
    ) -> float:
        """Calculate max allowed additional value for a position."""
        current = state.positions.get(symbol, 0)
        max_value = state.equity * (self.limits.max_position_pct / 100)
        return max(0, max_value - current)
