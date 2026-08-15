"""
Check the fill-confirmation layer against the cases a real broker produces.
===========================================================================
Alpaca paper fills everything instantly, so the interesting states — partial,
rejected, still queued, lookup failure — cannot be produced on demand. A fake
broker replays them, then the last case runs against the live broker to confirm
the real response shape still parses.

Usage:  python /app/scripts/fills_selftest.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from core.portfolio.fills import await_fill

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


class FakeBroker:
    """Returns a scripted sequence of order snapshots, one per poll."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def get_order(self, order_id):
        self.calls += 1
        if not self.snapshots:
            raise RuntimeError("no more snapshots")
        return self.snapshots.pop(0)


def snap(status, filled=0.0, price=None, qty=100.0):
    return {
        "id": "o1",
        "qty": qty,
        "filled_qty": filled,
        "filled_avg_price": price,
        "status": status,
    }


def main() -> None:
    submitted = {"id": "o1", "qty": 100.0, "filled_qty": 0.0, "status": "accepted"}

    print("\n1. an order that fills after a couple of polls")
    b = FakeBroker([snap("accepted"), snap("filled", 100.0, 51.25)])
    f = await_fill(b, submitted, timeout_sec=10, poll_sec=0)
    check("reported filled", f.filled, True)
    check("actual quantity", f.filled_qty, 100.0)
    check("actual price", f.avg_price, 51.25)
    check("not treated as dead", f.dead, False)
    check("stopped polling once terminal", b.calls, 2)

    print("\n2. a partial fill books only what filled")
    b = FakeBroker([snap("partially_filled", 30.0, 51.10), snap("canceled", 30.0, 51.10)])
    f = await_fill(b, submitted, timeout_sec=10, poll_sec=0)
    check("recognised as partial", f.partial, True)
    check("quantity is the filled part", f.filled_qty, 30.0)
    check("price fallback not used", f.price_or(99.0), 51.10)
    check("not dead — shares were acquired", f.dead, False)

    print("\n3. a rejected order must give the claim back")
    b = FakeBroker([snap("rejected")])
    f = await_fill(b, submitted, timeout_sec=10, poll_sec=0)
    check("dead", f.dead, True)
    check("nothing filled", f.filled, False)
    check("caller told to release", (f.dead and not f.filled), True)

    print("\n4. an order still resting keeps the claim but books nothing")
    b = FakeBroker([snap("accepted"), snap("accepted"), snap("new")])
    f = await_fill(b, submitted, timeout_sec=0.01, poll_sec=0)
    check("pending", f.pending, True)
    check("not dead", f.dead, False)
    check("nothing filled", f.filled_qty, 0.0)

    print("\n5. a broker lookup failure degrades instead of losing the order")
    class Broken:
        def get_order(self, order_id):
            raise RuntimeError("connection reset")

    f = await_fill(Broken(), submitted, timeout_sec=10, poll_sec=0)
    check("error captured", f.error, "connection reset")
    check("not declared dead", f.dead, False)
    check("falls back to the estimate", f.price_or(47.5), 47.5)

    print("\n6. a dry-run order never touches the broker")
    f = await_fill(Broken(), {"id": "dry-run", "qty": 10, "status": "dry"}, timeout_sec=10)
    check("returned immediately", f.terminal, True)
    check("no error", f.error, None)

    print("\n7. the live broker's real response shape parses")
    from execution.alpaca_client import AlpacaPaperClient

    client = AlpacaPaperClient()
    recent = client.orders(status="closed", limit=5)
    if not recent:
        print("  [SKIP] no historical orders to replay")
    else:
        real = recent[0]
        f = await_fill(client, real, timeout_sec=5, poll_sec=0)
        print(
            f"    {real['symbol']} {real['side']} status={f.status} "
            f"{f.describe()}"
        )
        check("terminal historical order", f.terminal, True)
        check("no lookup error", f.error, None)

    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
