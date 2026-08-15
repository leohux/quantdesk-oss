"""
Ledger vs broker drift check.
=============================
Read-only. Prints who owns what and exits non-zero when the ledger disagrees
with the broker, so it can run from cron as an alert.

Drift is expected to be transient — both runners reconcile at the start of every
scan — so a persistent mismatch means positions are moving outside the runners
(manual trades, or an entry order that never filled).

Usage:  python /app/scripts/sleeve_status.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from core.portfolio.sleeve import UNASSIGNED, SleeveBook, broker_qty_map, ensure_schema
from execution.alpaca_client import AlpacaPaperClient


def main() -> None:
    ensure_schema()
    client = AlpacaPaperClient()
    account = client.account()
    positions = client.positions()
    equity = float(account["equity"])
    broker = broker_qty_map(positions)
    prices = {str(p["symbol"]): float(p.get("current_price") or 0) for p in positions}
    book = SleeveBook.load()
    held = book.holdings()

    by_strategy: dict[str, list[str]] = {}
    for symbol, pos in held.items():
        by_strategy.setdefault(pos.strategy_id, []).append(symbol)

    print(f"equity ${equity:,.0f} | broker {len(broker)} position(s) | ledger {len(held)}")
    for sid in sorted(by_strategy):
        symbols = sorted(by_strategy[sid])
        exposure = sum(held[s].qty * prices.get(s, 0) for s in symbols)
        pct = exposure / equity * 100 if equity else 0
        label = "UNASSIGNED (frozen)" if sid == UNASSIGNED else sid
        print(f"  {pct:5.1f}%  {len(symbols)}  {label}")
        for s in symbols:
            print(f"           {s:6s} {held[s].qty:>8.0f} @ {held[s].avg_price:8.2f}")

    gross = sum(abs(float(p.get("market_value") or 0)) for p in positions)
    print(f"  gross exposure {gross / equity * 100:.0f}% of equity" if equity else "")

    drift: list[str] = []
    for symbol, qty in broker.items():
        owned = held[symbol].qty if symbol in held else 0.0
        if abs(owned - qty) > 1e-9:
            drift.append(f"{symbol}: broker {qty:g} vs ledger {owned:g}")
    for symbol, pos in held.items():
        if symbol not in broker:
            drift.append(f"{symbol}: ledger {pos.qty:g} vs broker 0")

    if drift:
        print("\nDRIFT:")
        for d in drift:
            print(f"  {d}")
        sys.exit(1)

    print("\nledger matches broker")


if __name__ == "__main__":
    main()
