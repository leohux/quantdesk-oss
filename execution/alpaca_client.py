from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from config.settings import get_settings


def _enum_val(v: Any) -> str:
    """Normalize Alpaca enum / str values to lowercase plain tokens."""
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value).lower()
    s = str(v)
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.lower()


def _with_retry(fn, *, retries: int = 4, base_sleep: float = 0.8):
    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            retryable = any(
                x in msg
                for x in (
                    "too many requests",
                    "rate limit",
                    "429",
                    "50010000",
                    "internal server error",
                    "timeout",
                    "temporarily",
                )
            )
            if not retryable or i == retries - 1:
                raise
            time.sleep(base_sleep * (2**i))
    raise last  # pragma: no cover


class AlpacaPaperClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.has_alpaca_keys:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")

        # paper=True forces paper endpoint regardless of base URL
        self.client = TradingClient(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            paper=True,
        )

    def account(self) -> dict[str, Any]:
        acct = _with_retry(self.client.get_account)
        return {
            "id": str(acct.id),
            "status": _enum_val(acct.status),
            "currency": acct.currency,
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "equity": float(acct.equity),
            "last_equity": float(getattr(acct, "last_equity", None) or acct.equity),
            "portfolio_value": float(acct.portfolio_value),
            "pattern_day_trader": bool(acct.pattern_day_trader),
            "trading_blocked": bool(acct.trading_blocked),
        }

    def is_market_open(self) -> bool:
        clock = _with_retry(self.client.get_clock)
        return bool(getattr(clock, "is_open", False))

    def positions(self) -> list[dict[str, Any]]:
        positions = _with_retry(self.client.get_all_positions)
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "qty_available": float(getattr(p, "qty_available", None) or p.qty),
                "side": _enum_val(p.side),
                "market_value": float(p.market_value),
                "avg_entry_price": float(p.avg_entry_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "current_price": float(p.current_price),
            }
            for p in positions
        ]

    def orders(
        self,
        status: str = "all",
        limit: int = 50,
        after: datetime | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        status_map = {
            "all": QueryOrderStatus.ALL,
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
        }
        kwargs: dict[str, Any] = {
            "status": status_map.get(status.lower(), QueryOrderStatus.ALL),
            "limit": limit,
        }
        if after is not None:
            kwargs["after"] = after
        if symbols:
            kwargs["symbols"] = [s.upper() for s in symbols]
        req = GetOrdersRequest(**kwargs)
        orders = _with_retry(lambda: self.client.get_orders(filter=req))
        return [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "qty": float(o.qty) if o.qty is not None else None,
                "filled_qty": float(o.filled_qty) if o.filled_qty is not None else None,
                "filled_avg_price": float(o.filled_avg_price)
                if getattr(o, "filled_avg_price", None) is not None
                else None,
                "limit_price": float(o.limit_price)
                if getattr(o, "limit_price", None) is not None
                else None,
                "stop_price": float(o.stop_price)
                if getattr(o, "stop_price", None) is not None
                else None,
                "side": _enum_val(o.side),
                "type": _enum_val(o.type),
                "status": _enum_val(o.status),
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            }
            for o in orders
        ]

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Current state of one order, including how much of it actually filled."""
        o = _with_retry(lambda: self.client.get_order_by_id(order_id))
        return {
            "id": str(o.id),
            "symbol": o.symbol,
            "qty": float(o.qty) if o.qty is not None else None,
            "filled_qty": float(o.filled_qty) if o.filled_qty is not None else 0.0,
            "filled_avg_price": float(o.filled_avg_price)
            if getattr(o, "filled_avg_price", None) is not None
            else None,
            "side": _enum_val(o.side),
            "type": _enum_val(o.type),
            "status": _enum_val(o.status),
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        }

    def find_open_stop_order(self, symbol: str) -> dict[str, Any] | None:
        """Resting sell stop / stop_limit leg for ``symbol`` (bracket SL child)."""
        sym = symbol.upper()
        for o in self.orders(status="open", limit=200, symbols=[sym]):
            if str(o.get("symbol", "")).upper() != sym:
                continue
            if str(o.get("side", "")).lower() != "sell":
                continue
            otype = str(o.get("type", "")).lower()
            if "stop" in otype:  # stop, stop_limit, trailing_stop
                return o
        return None

    def replace_stop_price(self, order_id: str, stop_price: float) -> dict[str, Any]:
        """PATCH an existing stop leg's stop_price (prefer over cancel+resubmit)."""
        px = round(float(stop_price), 2)
        req = ReplaceOrderRequest(stop_price=px)
        order = _with_retry(
            lambda: self.client.replace_order_by_id(order_id=order_id, order_data=req)
        )
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "type": _enum_val(order.type),
            "status": _enum_val(order.status),
            "stop_price": float(order.stop_price)
            if getattr(order, "stop_price", None) is not None
            else px,
        }

    def cancel_open_orders(self, symbol: str | None = None) -> int:
        """Cancel open orders (optionally for one symbol). Returns cancel count."""
        open_orders = self.orders(status="open", limit=200)
        canceled = 0
        target = symbol.upper() if symbol else None
        for o in open_orders:
            if target and str(o.get("symbol", "")).upper() != target:
                continue
            try:
                self.client.cancel_order_by_id(o["id"])
                canceled += 1
            except Exception:
                continue
        return canceled

    def cancel_stale_market_orders(self) -> list[dict[str, Any]]:
        """Cancel open MARKET orders stuck in accepted/new (post-close zombies).

        Leaves GTC limit/stop bracket legs alone so intraday SL/TP stays intact.
        """
        canceled: list[dict[str, Any]] = []
        for o in self.orders(status="open", limit=200):
            if str(o.get("type", "")).lower() != "market":
                continue
            try:
                self.client.cancel_order_by_id(o["id"])
                canceled.append(o)
            except Exception:
                continue
        if canceled:
            time.sleep(0.5)
        return canceled

    def market_order(self, symbol: str, qty: float, side: str) -> dict[str, Any]:
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "filled_qty": float(order.filled_qty)
            if getattr(order, "filled_qty", None) is not None
            else 0.0,
            "side": _enum_val(order.side),
            "status": _enum_val(order.status),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def limit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float,
        *,
        extended_hours: bool = False,
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        """Simple limit (no bracket). Use extended_hours=True for premarket/AH.

        Alpaca rejects bracket + extended_hours; arm OCO after fill instead.
        """
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY if str(time_in_force).lower() == "day" else TimeInForce.GTC
        lim = round(float(limit_price), 2)
        req = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=float(qty),
            side=side_enum,
            time_in_force=tif,
            limit_price=lim,
            extended_hours=bool(extended_hours),
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "filled_qty": float(order.filled_qty)
            if getattr(order, "filled_qty", None) is not None
            else 0.0,
            "filled_avg_price": float(order.filled_avg_price)
            if getattr(order, "filled_avg_price", None) is not None
            else None,
            "limit_price": lim,
            "side": _enum_val(order.side),
            "type": "limit",
            "status": _enum_val(order.status),
            "extended_hours": bool(extended_hours),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def bracket_order(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        stop_loss_pct: float = -0.08,
        take_profit_pct: float = 0.15,
        side: str = "buy",
    ) -> dict[str, Any]:
        """Submit a bracket BUY: market entry + broker-side SL + TP legs.

        The stop-loss leg is a REAL resting order at Alpaca, so it triggers
        intraday without our runner being online. stop_loss_pct is negative.
        """
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        sl_price = round(entry_price * (1.0 + stop_loss_pct), 2)
        tp_price = round(entry_price * (1.0 + take_profit_pct), 2)
        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp_price),
            stop_loss=StopLossRequest(stop_price=sl_price),
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "status": _enum_val(order.status),
            "order_class": "bracket",
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def limit_bracket_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        side: str = "buy",
    ) -> dict[str, Any]:
        """GTC limit entry + broker SL/TP legs (for MiMo dip buy zones)."""
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        lim = round(float(limit_price), 2)
        sl = round(float(stop_loss), 2)
        tp = round(float(take_profit), 2)
        if side.lower() == "buy" and not (sl < lim < tp):
            raise ValueError(f"bad bracket geometry sl={sl} limit={lim} tp={tp}")
        req = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=float(qty),
            side=side_enum,
            time_in_force=TimeInForce.GTC,
            limit_price=lim,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=tp),
            stop_loss=StopLossRequest(stop_price=sl),
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "status": _enum_val(order.status),
            "order_class": "bracket",
            "type": "limit",
            "limit_price": lim,
            "stop_loss": sl,
            "take_profit": tp,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def protective_stop(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
    ) -> dict[str, Any]:
        """GTC stop-loss for an existing long (broker-side, works intraday)."""
        req = StopOrderRequest(
            symbol=symbol.upper(),
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(float(stop_price), 2),
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "type": "stop",
            "status": _enum_val(order.status),
            "stop_price": round(float(stop_price), 2),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def oco_exit(
        self,
        symbol: str,
        qty: float,
        take_profit_price: float,
        stop_price: float,
    ) -> dict[str, Any]:
        """GTC OCO exit for an existing long; one leg filling cancels the other."""
        tp = round(float(take_profit_price), 2)
        sl = round(float(stop_price), 2)
        if not (0 < sl < tp):
            raise ValueError(f"bad OCO geometry stop={sl} take_profit={tp}")
        req = LimitOrderRequest(
            symbol=symbol.upper(),
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=tp),
            stop_loss=StopLossRequest(stop_price=sl),
        )
        order = _with_retry(lambda: self.client.submit_order(order_data=req))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "type": "limit",
            "status": _enum_val(order.status),
            "order_class": "oco",
            "stop_price": sl,
            "take_profit_price": tp,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        }

    def close_position(self, symbol: str) -> dict[str, Any]:
        order = _with_retry(lambda: self.client.close_position(symbol.upper()))
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty is not None else None,
            "side": _enum_val(order.side),
            "status": _enum_val(order.status),
        }

    def unlock_and_sell(
        self, symbol: str, qty: float | None = None, release_wait_sec: float = 10.0
    ) -> dict[str, Any]:
        """Cancel resting bracket/legs that lock shares, then sell available qty.

        Cancelling is asynchronous: the broker keeps the shares reserved for a
        moment after acknowledging it, so `qty_available` is polled rather than
        read once behind a fixed sleep. A partial sell that read 0 too early
        used to fall through to close_position and liquidate the whole name.
        """
        sym = symbol.upper()
        canceled = self.cancel_open_orders(sym)

        deadline = time.time() + release_wait_sec
        pos = None
        available = total = 0
        while True:
            positions = {p["symbol"]: p for p in self.positions()}
            pos = positions.get(sym)
            if not pos:
                raise RuntimeError(
                    f"no position for {sym} after canceling {canceled} open order(s)"
                )
            available = int(float(pos.get("qty_available") or 0))
            total = int(float(pos.get("qty") or 0))
            want = int(qty) if qty is not None else total
            if available >= min(want, total) or time.time() >= deadline:
                break
            time.sleep(1.0)

        want = int(qty) if qty is not None else available
        sell_qty = min(want, available, total)
        if sell_qty <= 0:
            # Only a caller asking for a full exit may fall back to a broker
            # close; doing it for a partial trim would sell shares nobody asked
            # to sell.
            if qty is None and total > 0:
                result = self.close_position(sym)
                result["canceled_orders"] = canceled
                result["note"] = "close_position fallback"
                return result
            raise RuntimeError(
                f"shares still locked for {sym} after {release_wait_sec}s "
                f"(want={want}, available={available}, total={total}, canceled={canceled})"
            )
        result = self.market_order(sym, sell_qty, "sell")
        result["canceled_orders"] = canceled
        result["qty"] = float(sell_qty)
        result["requested_qty"] = float(want)
        return result
