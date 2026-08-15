#!/usr/bin/env python3
"""Resume Phase 7.1 safely across Alpaca's overnight pending-cancel window."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from data.loader import load_ohlcv
from execution.alpaca_client import AlpacaPaperClient

TARGETS = ("COIN", "PLTR", "NVDA")
TP = {"COIN": 208.06, "PLTR": 153.38}
EVENTS = Path("/app/data/store/strategy_migration_events.jsonl")
REPORT = Path("/app/data/store/phase7_legacy_hardening.json")
STATE = Path("/app/data/store/phase7_legacy_hardening.state.json")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


def raw_open(client):
    return list(
        client.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=200,
                nested=True,
                symbols=list(TARGETS),
            )
        )
    )


def symbol_orders(client, symbol):
    return [o for o in raw_open(client) if o.symbol == symbol]


def has_stop(client, symbol) -> bool:
    for o in symbol_orders(client, symbol):
        if o.stop_price is not None:
            return True
        for leg in getattr(o, "legs", None) or []:
            if leg.stop_price is not None:
                return True
    return False


def qty_info(client, symbol):
    p = {x["symbol"]: x for x in client.positions()}.get(symbol)
    if not p:
        raise RuntimeError(f"{symbol}: position disappeared")
    return p, float(p["qty"]), float(p.get("qty_available") or 0)


def atr_stop(symbol, entry, current):
    df = load_ohlcv(symbol, start="2026-01-01").dropna(
        subset=["High", "Low", "Close"]
    )
    high, low, close = (
        df["High"].astype(float),
        df["Low"].astype(float),
        df["Close"].astype(float),
    )
    prev = close.shift(1)
    tr = (
        (high - low)
        .to_frame("range")
        .join((high - prev).abs().rename("high_gap"))
        .join((low - prev).abs().rename("low_gap"))
        .max(axis=1)
    )
    atr = float(tr.rolling(14).mean().iloc[-1])
    magnitude = min(0.10, max(0.03, 2 * atr / entry))
    stop = round(entry * (1 - magnitude), 2)
    if stop >= current:
        stop = round(current * 0.99, 2)
    return atr, -magnitude, stop


def cancel_symbol(client, symbol):
    count = 0
    for o in symbol_orders(client, symbol):
        try:
            client.client.cancel_order_by_id(str(o.id))
            count += 1
        except Exception as exc:
            log(f"{symbol}: cancel {o.id} returned {exc}")
    return count


def wait_available(client, symbol, qty):
    while True:
        _, _, available = qty_info(client, symbol)
        if available >= qty:
            return
        log(f"{symbol}: waiting share release available={available}/{qty}")
        time.sleep(30)


def snapshot(client):
    positions = {
        p["symbol"]: p for p in client.positions() if p["symbol"] in TARGETS
    }
    orders = []
    for o in raw_open(client):
        orders.append(
            {
                "symbol": o.symbol,
                "id": str(o.id),
                "type": str(o.type),
                "class": str(o.order_class),
                "status": str(o.status),
                "qty": float(o.qty) if o.qty is not None else None,
                "limit": float(o.limit_price) if o.limit_price is not None else None,
                "stop": float(o.stop_price) if o.stop_price is not None else None,
                "legs": [
                    {
                        "id": str(x.id),
                        "type": str(x.type),
                        "status": str(x.status),
                        "limit": float(x.limit_price)
                        if x.limit_price is not None
                        else None,
                        "stop": float(x.stop_price)
                        if x.stop_price is not None
                        else None,
                    }
                    for x in (getattr(o, "legs", None) or [])
                ],
            }
        )
    return positions, orders


def main():
    client = AlpacaPaperClient()
    before = {
        p["symbol"]: float(p["qty"])
        for p in client.positions()
        if p["symbol"] in TARGETS
    }
    actions = []

    # NVDA shares are free; protect immediately and idempotently.
    if not has_stop(client, "NVDA"):
        p, qty, _ = qty_info(client, "NVDA")
        atr, sl_pct, stop = atr_stop(
            "NVDA", float(p["avg_entry_price"]), float(p["current_price"])
        )
        result = client.protective_stop("NVDA", qty, stop)
        actions.append(
            {
                "symbol": "NVDA",
                "action": "add_protective_stop",
                "qty": qty,
                "atr14": atr,
                "stop_pct": sl_pct,
                "stop_price": stop,
                "order": result,
            }
        )
        log(f"NVDA: protective stop placed qty={qty} stop={stop}")
    else:
        log("NVDA: stop already present")

    # COIN cancellation is already pending overnight; wait until shares unlock.
    p, coin_qty, coin_avail = qty_info(client, "COIN")
    if not has_stop(client, "COIN"):
        if coin_avail < coin_qty:
            log("COIN: existing limit is pending cancel; waiting for market session")
            wait_available(client, "COIN", coin_qty)
        atr, sl_pct, stop = atr_stop(
            "COIN", float(p["avg_entry_price"]), float(p["current_price"])
        )
        result = client.oco_exit("COIN", coin_qty, TP["COIN"], stop)
        actions.append(
            {
                "symbol": "COIN",
                "action": "replace_limit_with_oco",
                "qty": coin_qty,
                "preserved_take_profit": TP["COIN"],
                "atr14": atr,
                "stop_pct": sl_pct,
                "stop_price": stop,
                "order": result,
            }
        )
        log(f"COIN: OCO placed TP={TP['COIN']} stop={stop}")

    # Leave PLTR's TP active until market opens; then replace in one sequence.
    if not has_stop(client, "PLTR"):
        while not client.is_market_open():
            log("PLTR: old TP kept active; waiting for market open to replace")
            time.sleep(60)
        p, pltr_qty, _ = qty_info(client, "PLTR")
        canceled = cancel_symbol(client, "PLTR")
        log(f"PLTR: requested cancel of {canceled} TP order(s)")
        wait_available(client, "PLTR", pltr_qty)
        atr, sl_pct, stop = atr_stop(
            "PLTR", float(p["avg_entry_price"]), float(p["current_price"])
        )
        result = client.oco_exit("PLTR", pltr_qty, TP["PLTR"], stop)
        actions.append(
            {
                "symbol": "PLTR",
                "action": "replace_limit_with_oco",
                "qty": pltr_qty,
                "preserved_take_profit": TP["PLTR"],
                "atr14": atr,
                "stop_pct": sl_pct,
                "stop_price": stop,
                "order": result,
            }
        )
        log(f"PLTR: OCO placed TP={TP['PLTR']} stop={stop}")

    time.sleep(2)
    after_positions, orders = snapshot(client)
    after = {s: float(p["qty"]) for s, p in after_positions.items()}
    coverage = {s: has_stop(client, s) for s in TARGETS}
    if before != after:
        raise RuntimeError(f"position size changed before={before} after={after}")
    if not all(coverage.values()):
        raise RuntimeError(f"incomplete stop coverage {coverage}")

    event = {
        "timestamp": now(),
        "event": "strategy_migration",
        "phase": "Phase7.1_LegacyExposureHardening",
        "from": "Cursor-Surge",
        "to": "ERC_V2",
        "action": "legacy_position_protection",
        "positions": list(TARGETS),
        "position_change": False,
        "reason": "disabled strategy exposure protection",
        "method": "original daily ATR framework (2x ATR, 3%-10% clamp)",
        "actions": actions,
        "verification": {
            "before_qty": before,
            "after_qty": after,
            "qty_unchanged": before == after,
            "stop_coverage": coverage,
            "open_orders": orders,
        },
    }
    REPORT.write_text(json.dumps(event, indent=2), encoding="utf-8")
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    STATE.write_text(
        json.dumps({"status": "completed", **event}, indent=2), encoding="utf-8"
    )

    # Human-readable legacy exposure checklist for Phase 7 review
    status_md = Path("/app/data/store/phase7_legacy_exposure_status.md")
    lines = [
        "# Legacy Exposure Status",
        "",
        f"- Timestamp: {now()}",
        f"- position_change: false",
        "",
        "COIN:",
        "  qty unchanged",
        "  stop: active",
        "  tp: active",
        "",
        "PLTR:",
        "  qty unchanged",
        "  OCO: active",
        "",
        "NVDA:",
        "  qty unchanged",
        "  stop: active",
        "  strategy_owner: ERC_V2",
        "",
        "Verification:",
        f"  stop_coverage: {coverage}",
        f"  qty: {after}",
        "",
    ]
    status_md.write_text("\n".join(lines), encoding="utf-8")
    log("Phase 7.1 complete; all stops covered and quantities unchanged")
    log(f"Wrote {status_md}")

    # Auto-run Phase 7.2 observation snapshot (read-only)
    try:
        import phase7_observation_snapshot as snap

        snap.main()
        log("Phase 7.2 observation snapshot written")
    except Exception as exc:
        log(f"Phase 7.2 snapshot deferred: {exc!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        STATE.write_text(
            json.dumps(
                {"status": "error", "timestamp": now(), "error": repr(exc)},
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"FATAL: {exc!r}")
        raise
