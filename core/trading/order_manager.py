"""Order Manager - full order lifecycle management."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    qty: float = 0.0
    limit_price: float | None = None
    filled_qty: float = 0.0
    filled_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: str = ""
    submitted_at: datetime = field(default_factory=datetime.utcnow)
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_qty(self) -> float:
        return self.qty - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_done(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "qty": self.qty,
            "limit_price": self.limit_price,
            "filled_qty": self.filled_qty,
            "filled_price": self.filled_price,
            "status": self.status.value,
            "reject_reason": self.reject_reason,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
        }


@dataclass
class Transaction:
    """Record of a completed trade."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    qty: float = 0.0
    price: float = 0.0
    amount: float = 0.0  # qty * price
    commission: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "price": self.price,
            "amount": self.amount,
            "commission": self.commission,
            "timestamp": self.timestamp.isoformat(),
        }


class OrderManager:
    """Manages order lifecycle: create, fill, cancel, reject."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._transactions: list[Transaction] = []
        self._order_history: list[Order] = []

    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Order:
        """Create a new order."""
        order = Order(
            symbol=symbol.upper(),
            side=side,
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            status=OrderStatus.SUBMITTED,
            metadata=metadata or {},
        )
        self._orders[order.id] = order
        return order

    def fill_order(
        self,
        order_id: str,
        qty: float,
        price: float,
        commission: float = 0.0,
    ) -> Transaction:
        """Fill (or partially fill) an order."""
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if not order.is_active:
            raise ValueError(f"Order {order_id} is not active (status={order.status})")

        fill_qty = min(qty, order.remaining_qty)
        order.filled_qty += fill_qty
        # Update average filled price
        if order.filled_price == 0:
            order.filled_price = price
        else:
            total_amount = order.filled_price * (order.filled_qty - fill_qty) + price * fill_qty
            order.filled_price = total_amount / order.filled_qty

        if order.filled_qty >= order.qty:
            order.status = OrderStatus.FILLED
            order.filled_at = datetime.utcnow()
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        txn = Transaction(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=fill_qty,
            price=price,
            amount=fill_qty * price,
            commission=commission,
        )
        self._transactions.append(txn)
        return txn

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an active order."""
        order = self._orders.get(order_id)
        if order is None or not order.is_active:
            return False
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.utcnow()
        return True

    def reject_order(self, order_id: str, reason: str) -> None:
        """Reject an order with reason."""
        order = self._orders.get(order_id)
        if order is None:
            return
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_active]

    def get_orders_by_symbol(self, symbol: str) -> list[Order]:
        return [o for o in self._orders.values() if o.symbol == symbol.upper()]

    def get_orders_by_status(self, status: OrderStatus) -> list[Order]:
        return [o for o in self._orders.values() if o.status == status]

    def get_all_orders(self, limit: int = 100) -> list[Order]:
        return list(self._orders.values())[-limit:]

    def get_transactions(self, limit: int = 100) -> list[Transaction]:
        return self._transactions[-limit:]

    def archive_filled(self) -> None:
        """Move done orders to history."""
        done = [o for o in self._orders.values() if o.is_done]
        self._order_history.extend(done)
        for o in done:
            del self._orders[o.id]

    def reset(self) -> None:
        self._orders.clear()
        self._transactions.clear()
        self._order_history.clear()
