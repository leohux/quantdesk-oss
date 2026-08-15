"""In-memory paper trading execution engine."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from core.execution.base import (
    Account,
    ExecutionEngine,
    Order,
    OrderSide,
    OrderStatus,
    Position,
)


class PaperExecutionEngine(ExecutionEngine):
    """In-memory paper trading engine.

    No broker connection required. Useful for testing and development.
    """

    def __init__(self, init_cash: float = 100_000.0) -> None:
        self._cash = init_cash
        self._init_cash = init_cash
        self._positions: dict[str, dict[str, Any]] = {}  # symbol -> {qty, avg_price}
        self._orders: list[Order] = []
        self._prices: dict[str, float] = {}  # last known prices

    @property
    def name(self) -> str:
        return "paper"

    @property
    def mode(self) -> str:
        return "paper"

    def _update_price(self, symbol: str, price: float) -> None:
        self._prices[symbol.upper()] = price

    def _portfolio_value(self) -> float:
        value = self._cash
        for sym, pos in self._positions.items():
            price = self._prices.get(sym, pos["avg_price"])
            value += pos["qty"] * price
        return value

    def get_account(self) -> Account:
        equity = self._portfolio_value()
        invested = sum(
            pos["qty"] * self._prices.get(sym, pos["avg_price"])
            for sym, pos in self._positions.items()
        )
        return Account(
            cash=self._cash,
            equity=equity,
            buying_power=self._cash,
            positions_count=len(self._positions),
            invested_pct=round(invested / equity * 100, 2) if equity else 0,
            mode="paper",
        )

    def get_positions(self) -> list[Position]:
        result = []
        equity = self._portfolio_value()
        for sym, pos in self._positions.items():
            price = self._prices.get(sym, pos["avg_price"])
            mv = pos["qty"] * price
            cost = pos["qty"] * pos["avg_price"]
            result.append(Position(
                symbol=sym,
                qty=pos["qty"],
                side=OrderSide.BUY,
                avg_entry_price=pos["avg_price"],
                current_price=price,
                market_value=mv,
                unrealized_pnl=mv - cost,
                unrealized_pnl_pct=((mv / cost) - 1) * 100 if cost else 0,
                weight_pct=round(mv / equity * 100, 2) if equity else 0,
            ))
        return result

    def get_orders(self, status: str = "all", limit: int = 50) -> list[Order]:
        orders = self._orders
        if status != "all":
            orders = [o for o in orders if o.status.value == status]
        return orders[-limit:]

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        price: float | None = None,
    ) -> Order:
        """Submit a paper order. If price is None, uses last known price."""
        symbol = symbol.upper()
        if price:
            self._update_price(symbol, price)

        last_price = self._prices.get(symbol)
        if last_price is None:
            raise ValueError(f"No price data for {symbol}. Set price first.")

        order = Order(
            id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            qty=qty,
            status=OrderStatus.FILLED,
            filled_qty=qty,
            filled_price=last_price,
            submitted_at=datetime.utcnow().isoformat(),
            filled_at=datetime.utcnow().isoformat(),
        )

        if side == OrderSide.BUY:
            cost = qty * last_price
            if cost > self._cash:
                order.status = OrderStatus.REJECTED
                self._orders.append(order)
                raise ValueError(f"Insufficient cash: need ${cost:.2f}, have ${self._cash:.2f}")

            self._cash -= cost
            pos = self._positions.get(symbol, {"qty": 0, "avg_price": 0})
            total_qty = pos["qty"] + qty
            if total_qty > 0:
                pos["avg_price"] = (pos["qty"] * pos["avg_price"] + cost) / total_qty
            pos["qty"] = total_qty
            self._positions[symbol] = pos

        elif side == OrderSide.SELL:
            pos = self._positions.get(symbol)
            if not pos or pos["qty"] < qty:
                order.status = OrderStatus.REJECTED
                self._orders.append(order)
                raise ValueError(f"Insufficient position: {symbol}")

            proceeds = qty * last_price
            self._cash += proceeds
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                del self._positions[symbol]

        self._orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        for order in self._orders:
            if order.id == order_id and order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
                return True
        return False

    def close_position(self, symbol: str) -> Order:
        symbol = symbol.upper()
        pos = self._positions.get(symbol)
        if not pos:
            raise ValueError(f"No position in {symbol}")
        return self.submit_order(symbol, pos["qty"], OrderSide.SELL)
