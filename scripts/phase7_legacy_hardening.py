#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 7.1: protect legacy Surge exposure without changing position size."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from data.loader import load_ohlcv
from execution.alpaca_client import AlpacaPaperClient

TARGETS = {"COIN", "PLTR", "NVDA"}
SURGE_ID = "cursor-surge-nvda-052828-63859c-82d552"
EVENTS = Path("/app/data/store/strategy_migration_events.jsonl")
REPORT = Path("/app/data/store/phase7_legacy_hardening.json")


def atr_stop(symbol: str, entry: float, current: float) -> dict:
    """Original daily risk framework: entry - 2*ATR, clamped to 3..10%."""
    df = load_ohlcv(symbol, start="2026-01-01").dropna(
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
    magnitude = min(0.10, max(0.03, 2.0 * atr / entry))
    stop = round(entry * (1.0 - magnitude), 2)
    if stop >= current:
        stop = round(current * 0.99, 2)
    return {"atr14": atr, "stop_pct": -magnitude, "stop_price": stop}


def raw_open_orders(client: AlpacaPaperClient) -> list:
    return list(
        client.client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
        )
    )


def wait_available(client: AlpacaPaperClient, symbol: str, qty: float) -> bool:
    for _ in range(20):
        positions = {p["symbol"]: p for p in client.positions()}
        p = positions.get(symbol)
        if not p:
            return False
        if float(p.get("qty_available") or 0) >= qty:
            return True
        time.sleep(0.5)
    return False


def main():
    client = AlpacaPaperClient()
    before_positions = {
        p["symbol"]: p for p in client.positions() if p["symbol"] in TARGETS
    }
    before_qty = {s: float(p["qty"]) for s, p in before_positions.items()}

    raw = raw_open_orders(client)
    existing_tp = {}
    existing_ids = {}
    for order in raw:
        if (
            order.symbol in {"COIN", "PLTR"}
            and str(getattr(order.side, "value", order.side)).lower() == "sell"
            and order.limit_price is not None
        ):
            existing_tp[order.symbol] = float(order.limit_price)
            existing_ids.setdefault(order.symbol, []).append(str(order.id))

    actions = []
    for symbol in ("COIN", "PLTR", "NVDA"):
        p = before_positions.get(symbol)
        if not p:
            actions.append({"symbol": symbol, "status": "skip_no_position"})
            continue
        qty = float(p["qty"])
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        risk = atr_stop(symbol, entry, current)

        if symbol in {"COIN", "PLTR"}:
            tp = existing_tp.get(symbol)
            if tp is None:
                raise RuntimeError(f"{symbol}: existing limit sell missing; abort")
            canceled = 0
            for order_id in existing_ids.get(symbol, []):
                client.client.cancel_order_by_id(order_id)
                canceled += 1
            if not wait_available(client, symbol, qty):
                raise RuntimeError(
                    f"{symbol}: shares not released after cancel; no replacement submitted"
                )
            result = client.oco_exit(
                symbol=symbol,
                qty=qty,
                take_profit_price=tp,
                stop_price=risk["stop_price"],
            )
            actions.append(
                {
                    "symbol": symbol,
                    "action": "replace_limit_with_oco",
                    "qty": qty,
                    "position_change": False,
                    "preserved_take_profit": tp,
                    "canceled_orders": canceled,
                    **risk,
                    "order": result,
                }
            )
        else:
            result = client.protective_stop(
                symbol=symbol,
                qty=qty,
                stop_price=risk["stop_price"],
            )
            actions.append(
                {
                    "symbol": symbol,
                    "action": "add_protective_stop",
                    "qty": qty,
                    "position_change": False,
                    **risk,
                    "order": result,
                }
            )

    # Verify quantities and resting stop coverage.
    after_positions = {
        p["symbol"]: p for p in client.positions() if p["symbol"] in TARGETS
    }
    after_qty = {s: float(p["qty"]) for s, p in after_positions.items()}
    qty_unchanged = all(after_qty.get(s) == q for s, q in before_qty.items())
    if not qty_unchanged:
        raise RuntimeError(f"position quantity changed: before={before_qty} after={after_qty}")

    raw_after = raw_open_orders(client)
    coverage = {}
    order_snapshot = []
    for symbol in TARGETS:
        symbol_orders = [o for o in raw_after if o.symbol == symbol]
        has_stop = any(o.stop_price is not None for o in symbol_orders)
        # OCO root may expose the stop as a leg rather than top-level order.
        for o in symbol_orders:
            for leg in getattr(o, "legs", None) or []:
                if leg.stop_price is not None:
                    has_stop = True
        coverage[symbol] = has_stop
        for o in symbol_orders:
            order_snapshot.append(
                {
                    "symbol": symbol,
                    "id": str(o.id),
                    "side": str(o.side),
                    "type": str(o.type),
                    "order_class": str(o.order_class),
                    "qty": float(o.qty) if o.qty is not None else None,
                    "limit_price": float(o.limit_price)
                    if o.limit_price is not None
                    else None,
                    "stop_price": float(o.stop_price)
                    if o.stop_price is not None
                    else None,
                    "status": str(o.status),
                    "legs": [
                        {
                            "id": str(leg.id),
                            "type": str(leg.type),
                            "limit_price": float(leg.limit_price)
                            if leg.limit_price is not None
                            else None,
                            "stop_price": float(leg.stop_price)
                            if leg.stop_price is not None
                            else None,
                            "status": str(leg.status),
                        }
                        for leg in (getattr(o, "legs", None) or [])
                    ],
                }
            )
    if not all(coverage.values()):
        raise RuntimeError(f"missing resting stop after hardening: {coverage}")

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "strategy_migration",
        "phase": "Phase7.1_LegacyExposureHardening",
        "from": "Cursor-Surge",
        "from_strategy_id": SURGE_ID,
        "to": "ERC_V2",
        "action": "legacy_position_protection",
        "positions": ["COIN", "PLTR", "NVDA"],
        "position_change": False,
        "reason": "disabled strategy exposure protection",
        "method": "original daily ATR framework (2x ATR, 3%-10% clamp)",
        "actions": actions,
        "verification": {
            "before_qty": before_qty,
            "after_qty": after_qty,
            "qty_unchanged": qty_unchanged,
            "stop_coverage": coverage,
            "open_orders": order_snapshot,
        },
    }
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    REPORT.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(event, indent=2, ensure_ascii=False))
    print("Wrote", REPORT, EVENTS)


if __name__ == "__main__":
    main()
