"""Alpaca execution engine (paper & live)."""
from __future__ import annotations

from typing import Any

from core.execution.base import (
    Account,
    ExecutionEngine,
    Order,
    OrderSide,
    OrderStatus,
    Position,
)


class AlpacaExecutionEngine(ExecutionEngine):
    """Alpaca broker execution engine."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        from alpaca.trading.client import TradingClient

        self._paper = paper
        self._client = TradingClient(api_key, secret_key, paper=paper)

    @property
    def name(self) -> str:
        return "alpaca"

    @property
    def mode(self) -> str:
        return "paper" if self._paper else "live"

    def is_connected(self) -> bool:
        try:
            self._client.get_account()
            return True
        except Exception:
            return False

    def get_account(self) -> Account:
        acct = self._client.get_account()
        equity = float(acct.equity)
        invested = sum(
            float(p.market_value) for p in self._client.get_all_positions()
        )
        return Account(
            cash=float(acct.cash),
            equity=equity,
            buying_power=float(acct.buying_power),
            positions_count=len(self._client.get_all_positions()),
            invested_pct=round(invested / equity * 100, 2) if equity else 0,
            currency=acct.currency,
            mode=self.mode,
        )

    def get_positions(self) -> list[Position]:
        positions = self._client.get_all_positions()
        equity = float(self._client.get_account().equity)
        result = []
        for p in positions:
            mv = float(p.market_value)
            result.append(Position(
                symbol=p.symbol,
                qty=float(p.qty),
                side=OrderSide.BUY if str(p.side) == "long" else OrderSide.SELL,
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=mv,
                unrealized_pnl=float(p.unrealized_pl),
                unrealized_pnl_pct=float(p.unrealized_plpc) * 100,
                weight_pct=round(mv / equity * 100, 2) if equity else 0,
            ))
        return result

    def get_orders(self, status: str = "all", limit: int = 50) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=status, limit=limit)
        orders = self._client.get_orders(filter=req)
        return [
            Order(
                id=str(o.id),
                symbol=o.symbol,
                side=OrderSide.BUY if str(o.side) == "OrderSide.BUY" else OrderSide.SELL,
                qty=float(o.qty) if o.qty else 0,
                status=OrderStatus(str(o.status).lower().replace("orderstatus.", "")),
                filled_qty=float(o.filled_qty) if o.filled_qty else 0,
                submitted_at=o.submitted_at.isoformat() if o.submitted_at else None,
                filled_at=o.filled_at.isoformat() if o.filled_at else None,
            )
            for o in orders
        ]

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
    ) -> Order:
        from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side_enum = AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL
        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        order = self._client.submit_order(order_data=req)
        return Order(
            id=str(order.id),
            symbol=order.symbol,
            side=side,
            qty=float(order.qty) if order.qty else 0,
            status=OrderStatus.PENDING,
            submitted_at=order.submitted_at.isoformat() if order.submitted_at else None,
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def close_position(self, symbol: str) -> Order:
        order = self._client.close_position(symbol.upper())
        return Order(
            id=str(order.id),
            symbol=order.symbol,
            side=OrderSide.SELL,
            qty=float(order.qty) if order.qty else 0,
            status=OrderStatus.PENDING,
        )
