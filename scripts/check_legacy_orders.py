#!/usr/bin/env python3
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from data.loader import load_ohlcv
from execution.alpaca_client import AlpacaPaperClient

c = AlpacaPaperClient()
syms = {"COIN", "PLTR", "NVDA"}

print("RAW OPEN")
for o in c.client.get_orders(
    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
):
    if o.symbol not in syms:
        continue
    print(
        o.symbol,
        "id=", str(o.id),
        "type=", o.type,
        "class=", o.order_class,
        "side=", o.side,
        "qty=", o.qty,
        "limit=", o.limit_price,
        "stop=", o.stop_price,
        "parent=", getattr(o, "parent_order_id", None),
        "legs=", len(getattr(o, "legs", None) or []),
    )

print("ATR PLAN")
for p in c.positions():
    if p["symbol"] not in syms:
        continue
    df = load_ohlcv(p["symbol"], start="2026-01-01").dropna(
        subset=["High", "Low", "Close"]
    )
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev = close.shift(1)
    tr = (
        (high - low)
        .to_frame("range")
        .join((high - prev).abs().rename("high_gap"))
        .join((low - prev).abs().rename("low_gap"))
        .max(axis=1)
    )
    atr = float(tr.rolling(14).mean().iloc[-1])
    entry = float(p["avg_entry_price"])
    current = float(p["current_price"])
    magnitude = min(0.10, max(0.03, 2 * atr / entry))
    stop = round(entry * (1 - magnitude), 2)
    if stop >= current:
        stop = round(current * 0.99, 2)
    print(
        p["symbol"],
        "entry=", entry,
        "current=", current,
        "ATR14=", round(atr, 2),
        "sl_pct=", round(-magnitude, 4),
        "stop=", stop,
        "qty=", p["qty"],
        "available=", p["qty_available"],
    )
