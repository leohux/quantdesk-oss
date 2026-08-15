"""IBKR execution engine with fail-closed live support and mock mode.

This adapter is intentionally conservative:
- `gateway_mode=mock` uses an in-memory broker for tests and previews.
- `gateway_mode=paper` / `live` connect to IB Gateway or TWS via `ib_async`.
- advanced exits (`bracket_exit`, `oco_exit`) are exposed as helper methods for
  live hardening / lifecycle migration, while the abstract interface remains
  market-order centric for the engine.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from config.settings import get_settings
from core.execution.base import Account, ExecutionEngine, Order, OrderSide, OrderStatus, Position

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised in integration environments
    from ib_async import IB, Contract, MarketOrder, Order as IBOrder, Stock
except Exception:  # pragma: no cover - tests may run without the package
    IB = None
    Contract = None
    IBOrder = None
    MarketOrder = None
    Stock = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _usable_price(v: Any) -> float | None:
    """Coerce an IB tick to a tradable price, rejecting None/NaN/non-positive."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f <= 0:
        return None
    return f


@dataclass
class _MockBrokerState:
    account_id: str = "DU-QUANTDESK"
    cash: float = 100_000.0
    equity: float = 100_000.0
    buying_power: float = 100_000.0
    currency: str = "USD"
    positions: dict[str, Position] | None = None
    orders: list[Order] | None = None

    def __post_init__(self) -> None:
        self.positions = self.positions or {}
        self.orders = self.orders or []


class IBKRExecutionEngine(ExecutionEngine):
    """Interactive Brokers execution engine with mock and gateway-backed modes."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        gateway_mode: str | None = None,
        read_only: bool | None = None,
    ) -> None:
        settings = get_settings()
        self._host = host or settings.ibkr_host
        self._port = int(port or settings.ibkr_port)
        self._client_id = int(client_id or settings.ibkr_client_id)
        self._gateway_mode = (gateway_mode or settings.ibkr_gateway_mode).lower()
        self._read_only = settings.ibkr_read_only if read_only is None else bool(read_only)
        self._settings = settings
        self._ib = None
        self._mock = _MockBrokerState()

    @property
    def name(self) -> str:
        return "ibkr"

    @property
    def mode(self) -> str:
        return "paper" if self._settings.ibkr_trading_mode.lower() == "paper" else "live"

    def _connect(self):
        if self._gateway_mode == "mock":
            return None
        if IB is None:
            raise RuntimeError("ib_async is not installed")
        if self._ib is None:
            self._ib = IB()
        if not self._ib.isConnected():
            self._ib.connect(
                host=self._host,
                port=self._port,
                clientId=self._client_id,
                timeout=float(self._settings.ibkr_connect_timeout_sec),
                readonly=self._read_only,
            )
        return self._ib

    def _stock(self, symbol: str):
        if Stock is None:
            raise RuntimeError("ib_async not available")
        return Stock(symbol.upper(), "SMART", "USD")

    def is_connected(self) -> bool:
        if self._gateway_mode == "mock":
            return True
        try:
            ib = self._connect()
            return bool(ib and ib.isConnected())
        except Exception:
            return False

    def get_account(self) -> Account:
        if self._gateway_mode == "mock":
            invested = sum(p.market_value for p in self._mock.positions.values())
            equity = self._mock.cash + invested
            self._mock.equity = equity
            return Account(
                cash=self._mock.cash,
                equity=equity,
                buying_power=self._mock.buying_power,
                positions_count=len(self._mock.positions),
                invested_pct=round(invested / equity * 100, 2) if equity else 0.0,
                currency=self._mock.currency,
                mode=self.mode,
                account_id=self._mock.account_id,
                broker="ibkr",
            )

        ib = self._connect()
        summary = self._account_summary(ib)
        cash = _safe_float(summary.get("TotalCashValue"))
        equity = _safe_float(summary.get("NetLiquidation"))
        buying_power = _safe_float(summary.get("BuyingPower") or summary.get("AvailableFunds"))
        account_id = summary.get("AccountType") or self._settings.ibkr_account
        positions = self.get_positions()
        invested = sum(p.market_value for p in positions)
        return Account(
            cash=cash,
            equity=equity,
            buying_power=buying_power,
            positions_count=len(positions),
            invested_pct=round(invested / equity * 100, 2) if equity else 0.0,
            currency="USD",
            mode=self.mode,
            account_id=str(self._settings.ibkr_account or account_id or ""),
            broker="ibkr",
        )

    def _account_summary(self, ib: Any) -> dict[str, str]:
        values = ib.accountSummary() or []
        return {(getattr(v, "tag", "") or ""): getattr(v, "value", "") for v in values}

    def _market_prices(self, ib: Any, raw_positions: list[Any]) -> dict[int, float]:
        """Map conId -> live price for held contracts.

        Uses IBKR's complimentary non-consolidated US equity feed (Cboe One +
        IEX), so quotes are real-time but not NBBO. Contracts that never tick
        are simply absent from the result.
        """
        contracts = [
            c
            for c in (getattr(p, "contract", None) for p in raw_positions)
            if c is not None and getattr(c, "conId", None)
        ]
        if not contracts:
            return {}

        try:
            tickers = ib.reqTickers(*contracts)
        except Exception as exc:
            logger.warning("IBKR reqTickers failed, falling back to cost basis: %s", exc)
            return {}

        prices: dict[int, float] = {}
        for ticker in tickers or []:
            con_id = getattr(getattr(ticker, "contract", None), "conId", None)
            if not con_id:
                continue
            candidates: list[Any] = []
            market_price = getattr(ticker, "marketPrice", None)
            if callable(market_price):
                try:
                    candidates.append(market_price())
                except Exception:
                    pass
            candidates.extend(
                [
                    getattr(ticker, "last", None),
                    getattr(ticker, "close", None),
                    getattr(ticker, "bid", None),
                    getattr(ticker, "ask", None),
                ]
            )
            for candidate in candidates:
                price = _usable_price(candidate)
                if price is not None:
                    prices[con_id] = price
                    break

        missing = len(contracts) - len(prices)
        if missing:
            logger.warning("IBKR: no usable quote for %d/%d positions", missing, len(contracts))
        return prices

    def get_positions(self) -> list[Position]:
        if self._gateway_mode == "mock":
            acct = self.get_account()
            eq = max(acct.equity, 1e-9)
            out = []
            for pos in self._mock.positions.values():
                pos.weight_pct = round(pos.market_value / eq * 100, 2)
                out.append(pos)
            return out

        ib = self._connect()
        # Read equity straight from the summary: get_account() calls back into
        # this method, so reusing it here would recurse forever.
        eq = max(_safe_float(self._account_summary(ib).get("NetLiquidation")), 1e-9)
        raw_positions = list(ib.positions() or [])
        prices = self._market_prices(ib, raw_positions)
        out: list[Position] = []
        for p in raw_positions:
            contract = getattr(p, "contract", None)
            symbol = getattr(contract, "symbol", "")
            qty = _safe_float(getattr(p, "position", 0))
            avg = _safe_float(getattr(p, "avgCost", 0))
            # Falling back to cost yields a 0 P&L row rather than a wrong one.
            current = prices.get(getattr(contract, "conId", None)) or avg
            mv = qty * current
            upl = (current - avg) * qty
            out.append(
                Position(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY if qty >= 0 else OrderSide.SELL,
                    avg_entry_price=avg,
                    current_price=current,
                    market_value=mv,
                    unrealized_pnl=upl,
                    unrealized_pnl_pct=(upl / (abs(qty) * avg) * 100) if qty and avg else 0.0,
                    weight_pct=round(mv / eq * 100, 2) if eq else 0.0,
                )
            )
        return out

    def _map_status(self, value: Any) -> OrderStatus:
        raw = str(value or "").lower()
        if "fill" in raw:
            return OrderStatus.FILLED
        if "cancel" in raw:
            return OrderStatus.CANCELLED
        if "reject" in raw or "inactive" in raw:
            return OrderStatus.REJECTED
        if "partial" in raw:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.PENDING

    def get_orders(self, status: str = "all", limit: int = 50) -> list[Order]:
        if self._gateway_mode == "mock":
            orders = list(self._mock.orders)
        else:
            ib = self._connect()
            trades = list(ib.trades() or [])
            orders = []
            for tr in trades:
                order = getattr(tr, "order", None)
                status_obj = getattr(tr, "orderStatus", None)
                contract = getattr(tr, "contract", None)
                if not order:
                    continue
                orders.append(
                    Order(
                        id=str(getattr(order, "permId", None) or getattr(order, "orderId", "")),
                        symbol=str(getattr(contract, "symbol", "")),
                        side=OrderSide.BUY if str(getattr(order, "action", "BUY")).upper() == "BUY" else OrderSide.SELL,
                        qty=_safe_float(getattr(order, "totalQuantity", 0)),
                        status=self._map_status(getattr(status_obj, "status", "")),
                        filled_qty=_safe_float(getattr(status_obj, "filled", 0)),
                        filled_price=_safe_float(getattr(status_obj, "avgFillPrice", 0), None),
                        submitted_at=_now(),
                        limit_price=_safe_float(getattr(order, "lmtPrice", 0), None),
                        stop_price=_safe_float(getattr(order, "auxPrice", 0), None),
                        metadata={
                            "client_order_id": getattr(order, "orderRef", None),
                            "why_held": getattr(status_obj, "whyHeld", None),
                        },
                    )
                )
        if status != "all":
            want = status.lower()
            orders = [o for o in orders if o.status.value == want or want == "open" and o.status in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}]
        return orders[-limit:]

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        client_order_id: str | None = None,
    ) -> Order:
        symbol = symbol.upper()
        client_order_id = client_order_id or f"ibkr-{symbol}-{int(datetime.now().timestamp())}"
        if self._gateway_mode == "mock":
            order = Order(
                id=f"mock-{len(self._mock.orders)+1}",
                symbol=symbol,
                side=side,
                qty=float(qty),
                status=OrderStatus.PENDING,
                submitted_at=_now(),
                metadata={"client_order_id": client_order_id, "gateway_mode": "mock"},
            )
            self._mock.orders.append(order)
            return order

        ib = self._connect()
        contract = self._stock(symbol)
        ib.qualifyContracts(contract)
        ib_order = MarketOrder("BUY" if side == OrderSide.BUY else "SELL", float(qty), orderRef=client_order_id)
        trade = ib.placeOrder(contract, ib_order)
        return Order(
            id=str(getattr(getattr(trade, "order", None), "orderId", "")),
            symbol=symbol,
            side=side,
            qty=float(qty),
            status=self._map_status(getattr(getattr(trade, "orderStatus", None), "status", "")),
            submitted_at=_now(),
            metadata={"client_order_id": client_order_id},
        )

    def cancel_order(self, order_id: str) -> bool:
        if self._gateway_mode == "mock":
            for order in self._mock.orders:
                if order.id == order_id:
                    order.status = OrderStatus.CANCELLED
                    return True
            return False

        ib = self._connect()
        for trade in ib.trades() or []:
            ib_order = getattr(trade, "order", None)
            ib_id = str(getattr(ib_order, "permId", None) or getattr(ib_order, "orderId", ""))
            if ib_id == order_id:
                ib.cancelOrder(ib_order)
                return True
        return False

    def close_position(self, symbol: str) -> Order:
        symbol = symbol.upper()
        positions = {p.symbol: p for p in self.get_positions()}
        pos = positions.get(symbol)
        if pos is None or pos.qty <= 0:
            raise RuntimeError(f"No long position to close for {symbol}")
        return self.submit_order(symbol=symbol, qty=pos.qty, side=OrderSide.SELL)

    def oco_exit(
        self,
        *,
        symbol: str,
        qty: float,
        take_profit_price: float,
        stop_price: float,
        client_order_id: str | None = None,
    ) -> Order:
        client_order_id = client_order_id or f"oco-{symbol}-{int(datetime.now().timestamp())}"
        if self._gateway_mode == "mock":
            order = Order(
                id=f"mock-oco-{len(self._mock.orders)+1}",
                symbol=symbol.upper(),
                side=OrderSide.SELL,
                qty=float(qty),
                status=OrderStatus.PENDING,
                submitted_at=_now(),
                limit_price=float(take_profit_price),
                stop_price=float(stop_price),
                metadata={"client_order_id": client_order_id, "order_class": "oco"},
            )
            self._mock.orders.append(order)
            return order
        raise NotImplementedError("IBKR OCO exits are only enabled in mock/paper validation for now")
