"""Portfolio Manager - multi-stock position & cash tracking."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.trading.order_manager import OrderSide


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    side: OrderSide = OrderSide.BUY

    def update_price(self, price: float) -> None:
        self.current_price = price
        self.market_value = self.qty * price
        if self.qty > 0:
            self.unrealized_pnl = (price - self.avg_cost) * self.qty
        else:
            self.unrealized_pnl = 0.0

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_cost": round(self.avg_cost, 4),
            "current_price": round(self.current_price, 4),
            "market_value": round(self.market_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "side": self.side.value,
        }


@dataclass
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    invested: float


class PortfolioManager:
    """Multi-stock portfolio with cash, equity curve, and exposure tracking."""

    def __init__(self, init_cash: float = 100_000.0) -> None:
        self._init_cash = init_cash
        self._cash = init_cash
        self._positions: dict[str, Position] = {}
        self._equity_curve: list[EquityPoint] = []
        self._total_realized_pnl: float = 0.0

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def invested(self) -> float:
        return sum(p.market_value for p in self._positions.values())

    @property
    def equity(self) -> float:
        return self._cash + self.invested

    @property
    def total_realized_pnl(self) -> float:
        return self._total_realized_pnl

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def exposure_pct(self) -> float:
        eq = self.equity
        return (self.invested / eq * 100) if eq > 0 else 0.0

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol.upper())

    def open_position(
        self,
        symbol: str,
        qty: float,
        price: float,
        side: OrderSide = OrderSide.BUY,
        commission: float = 0.0,
    ) -> None:
        """Open or add to a position."""
        symbol = symbol.upper()
        cost = qty * price + commission

        if side == OrderSide.BUY:
            self._cash -= cost
            pos = self._positions.get(symbol)
            if pos is None:
                pos = Position(symbol=symbol, side=OrderSide.BUY)
                self._positions[symbol] = pos
            # Average cost calculation
            total_qty = pos.qty + qty
            if total_qty > 0:
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / total_qty
            pos.qty = total_qty
            pos.update_price(price)

        elif side == OrderSide.SELL:
            pos = self._positions.get(symbol)
            if pos is None or pos.qty < qty:
                raise ValueError(f"Insufficient position: {symbol} (have {pos.qty if pos else 0})")
            # Realized PnL
            realized = (price - pos.avg_cost) * qty - commission
            pos.realized_pnl += realized
            self._total_realized_pnl += realized
            self._cash += qty * price - commission
            pos.qty -= qty
            if pos.qty <= 0:
                del self._positions[symbol]
            else:
                pos.update_price(price)

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update all position prices."""
        for symbol, pos in self._positions.items():
            if symbol in prices:
                pos.update_price(prices[symbol])

    def snapshot(self) -> EquityPoint:
        """Record current equity to curve."""
        point = EquityPoint(
            timestamp=datetime.utcnow(),
            equity=self.equity,
            cash=self._cash,
            invested=self.invested,
        )
        self._equity_curve.append(point)
        return point

    def get_equity_curve(self) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": p.timestamp.isoformat(),
                "equity": round(p.equity, 2),
                "cash": round(p.cash, 2),
                "invested": round(p.invested, 2),
            }
            for p in self._equity_curve
        ]

    def get_exposure(self) -> dict[str, Any]:
        """Get portfolio exposure breakdown."""
        eq = self.equity
        return {
            "equity": round(eq, 2),
            "cash": round(self._cash, 2),
            "invested": round(self.invested, 2),
            "exposure_pct": round(self.exposure_pct, 2),
            "cash_pct": round((self._cash / eq * 100) if eq > 0 else 0, 2),
            "positions_count": len(self._positions),
            "total_realized_pnl": round(self._total_realized_pnl, 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            "per_position": {
                sym: {
                    "value": round(p.market_value, 2),
                    "weight_pct": round(p.market_value / eq * 100, 2) if eq > 0 else 0,
                }
                for sym, p in self._positions.items()
            },
        }

    def to_account_dict(self) -> dict[str, Any]:
        return {
            "equity": round(self.equity, 2),
            "cash": round(self._cash, 2),
            "invested": round(self.invested, 2),
            "exposure_pct": round(self.exposure_pct, 2),
            "total_realized_pnl": round(self._total_realized_pnl, 2),
            "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            "positions_count": len(self._positions),
            "init_cash": self._init_cash,
            "total_return_pct": round((self.equity / self._init_cash - 1) * 100, 2),
        }

    def reset(self, init_cash: float | None = None) -> None:
        self._init_cash = init_cash or self._init_cash
        self._cash = self._init_cash
        self._positions.clear()
        self._equity_curve.clear()
        self._total_realized_pnl = 0.0
