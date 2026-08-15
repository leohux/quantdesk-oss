# -*- coding: utf-8 -*-
"""One-shot: attach GTC stop to naked daily longs (NVDA/SPY) if missing."""
from execution.alpaca_client import AlpacaPaperClient

# Default protective stop from avg entry (matches daily max |SL| band)
STOP_PCT = -0.08

def main():
    c = AlpacaPaperClient()
    open_orders = c.orders(status="open", limit=100)
    stops_by_sym = {
        o["symbol"]
        for o in open_orders
        if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit", "trailing_stop")
    }
    # also treat bracket legs
    for o in open_orders:
        if o.get("side") == "sell":
            stops_by_sym.add(o["symbol"])

    for p in c.positions():
        sym = p["symbol"]
        if sym == "AMC":
            continue  # already has bracket/limit from intraday
        if sym in stops_by_sym:
            print(f"{sym}: already has resting sell/stop ??skip")
            continue
        entry = float(p.get("avg_entry_price") or 0)
        qty = float(p.get("qty") or 0)
        px = float(p.get("current_price") or 0)
        if entry <= 0 or qty <= 0:
            continue
        sl = round(entry * (1.0 + STOP_PCT), 2)
        if px > 0 and sl >= px:
            # already through stop; tighten slightly under market so it can rest
            sl = round(px * 0.99, 2)
            print(f"{sym}: entry-stop above market; using 1% under last={px} -> {sl}")
        try:
            r = c.protective_stop(sym, qty, sl)
            print(f"{sym}: placed STOP qty={qty} @ {sl} id={r.get('id')} status={r.get('status')}")
        except Exception as e:
            print(f"{sym}: FAIL {e}")

if __name__ == "__main__":
    main()
