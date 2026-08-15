"""Abstract execution engine interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    qty: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float | None = None
    submitted_at: str | None = None
    filled_at: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class Position:
    symbol: str
    qty: float
    side: OrderSide
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    weight_pct: float = 0.0


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float
    positions_count: int
    invested_pct: float
    currency: str = "USD"
    mode: str = "paper"  # paper | live
    account_id: str = ""
    broker: str = ""


class ExecutionEngine(ABC):
    """Abstract execution engine.

    Implement this for each broker: Alpaca, IBKR, Paper, etc.
    Strategies and backtests never call this directly.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Engine identifier (e.g., 'alpaca', 'ibkr', 'paper')."""
        ...

    @property
    @abstractmethod
    def mode(self) -> str:
        """'paper' or 'live'."""
        ...

    @abstractmethod
    def get_account(self) -> Account:
        """Get current account state."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        ...

    @abstractmethod
    def get_orders(
        self,
        status: str = "all",
        limit: int = 50,
    ) -> list[Order]:
        """Get order history."""
        ...

    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        client_order_id: str | None = None,
    ) -> Order:
        """Submit a market order."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        ...

    @abstractmethod
    def close_position(self, symbol: str) -> Order:
        """Close an entire position."""
        ...

    def is_connected(self) -> bool:
        """Check if broker connection is alive."""
        return True
