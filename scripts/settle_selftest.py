"""
End-to-end check for broker-exit settlement.
============================================
Uses real filled sell orders from the broker, but books them against a
throwaway strategy id so no live sleeve or journal row is affected. Everything
it writes is deleted again at the end.

Usage:  python /app/scripts/settle_selftest.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")

from core.portfolio.settle import LOOKBACK_DAYS, _filled_sells, settle_broker_exits
from core.portfolio.sleeve import SleeveBook, ensure_schema
from execution.alpaca_client import AlpacaPaperClient

TEST_SID = "__settle_selftest__"

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def cleanup() -> None:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        s.execute(text("DELETE FROM trade_journal WHERE strategy_id = :sid"), {"sid": TEST_SID})
        s.execute(text("DELETE FROM transactions WHERE strategy_id = :sid"), {"sid": TEST_SID})
        s.execute(text("DELETE FROM orders WHERE strategy_id = :sid"), {"sid": TEST_SID})
        s.execute(text("DELETE FROM strategy_positions WHERE strategy_id = :sid"), {"sid": TEST_SID})
        s.execute(text("DELETE FROM strategies WHERE id = :sid"), {"sid": TEST_SID})
        s.commit()
    finally:
        s.close()


def _backdate(symbol: str, fill_iso: str, minutes_before: int) -> None:
    """Move the ledger row's updated_at to just before a known broker fill."""
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    when = datetime.fromisoformat(fill_iso.replace("Z", "+00:00")) - timedelta(
        minutes=minutes_before
    )
    s = SyncSessionLocal()
    try:
        s.execute(
            text(
                "UPDATE strategy_positions SET updated_at = :ts "
                "WHERE strategy_id = :sid AND symbol = :sym"
            ),
            {"ts": when, "sid": TEST_SID, "sym": symbol},
        )
        s.commit()
    finally:
        s.close()


def journal_rows() -> list[tuple]:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        return list(
            s.execute(
                text(
                    "SELECT symbol, qty, entry_price, exit_price, status, realized_pnl "
                    "FROM trade_journal WHERE strategy_id = :sid"
                ),
                {"sid": TEST_SID},
            ).fetchall()
        )
    finally:
        s.close()


def main() -> None:
    ensure_schema()
    cleanup()
    client = AlpacaPaperClient()

    print(f"\n1. broker returns filled sells with a fill price (last {LOOKBACK_DAYS}d)")
    held = {p["symbol"] for p in client.positions()}
    recent = client.orders(
        status="closed",
        limit=200,
        after=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
    )
    sells = [
        o
        for o in recent
        if str(o.get("side")).lower() == "sell"
        and o.get("filled_at")
        and (o.get("filled_qty") or 0) > 0
        and o.get("filled_avg_price")
        # a symbol still held would make reconcile see no exit at all
        and o["symbol"] not in held
    ]
    for o in sells[:6]:
        print(
            f"    {o['symbol']:6s} qty={o['filled_qty']:>6.0f} "
            f"@ ${o['filled_avg_price']:.2f}  {o['filled_at'][:19]}  {o['type']}"
        )
    check("found a usable filled sell", bool(sells), True)
    if not sells:
        print("\nno recent bracket exits to replay — nothing to verify")
        cleanup()
        sys.exit(1)

    sample = sells[0]
    symbol = sample["symbol"]
    qty = float(sample["filled_qty"])
    entry = float(sample["filled_avg_price"]) * 0.9  # pretend we bought 10% lower

    print(f"\n2. lookup helper finds fills for {symbol}")
    found = _filled_sells(client, [symbol])
    check("symbol present", symbol.upper() in found, True)
    check("has a fill price", bool(found[symbol.upper()][0].get("filled_avg_price")), True)

    print("\n3. a position that vanished gets settled at the real fill price")
    book = SleeveBook.load()
    check("test sleeve claims it", book.claim(TEST_SID, symbol, qty, entry, source="test"), True)
    # Journal the entry the way a runner would, so there is a row to settle.
    from core.trade_log import ensure_strategy, record_entry

    ensure_strategy(TEST_SID, name="settle selftest", enabled=False)
    record_entry(
        strategy_id=TEST_SID,
        strategy_name="settle selftest",
        symbol=symbol,
        qty=qty,
        price=entry,
        reason="selftest entry",
    )
    check("journal row opened", journal_rows()[0][4], "open")

    # A real entry predates its exit; the claim above was made just now, so
    # backdate it or the fill we are replaying looks like an older round-trip.
    _backdate(symbol, sample["filled_at"], minutes_before=30)

    # Broker reports no position: exactly what a fired stop-loss looks like.
    changes = book.reconcile({}, protected=set(), grace_sec=0, symbols=[symbol])
    check("reconcile saw the exit", [c.kind for c in changes], ["closed"])
    check("exit quantity carried", changes[0].qty_gone, qty)

    settled = settle_broker_exits(client, changes)
    check("one settlement produced", len(settled), 1)
    if settled:
        s = settled[0]
        check("settled at the broker fill price", round(s["exit_price"], 4),
              round(float(sample["filled_avg_price"]), 4))
        check("journal row closed", s["journal_rows"], 1)

    row = journal_rows()[0]
    sym, jqty, jentry, jexit, jstatus, jpnl = row
    print(
        f"    journal: {sym} qty={jqty:g} entry=${jentry:.2f} exit=${jexit:.2f} "
        f"status={jstatus} realized=${jpnl:,.2f}"
    )
    check("status closed", jstatus, "closed")
    check("exit price recorded", jexit is not None, True)
    check("realized pnl computed", round(jpnl, 2), round((jexit - jentry) * jqty, 2))

    print("\n4. an older fill in the same symbol cannot be borrowed as our exit")
    cleanup()
    book = SleeveBook.load()
    book.claim(TEST_SID, symbol, qty, entry, source="test")
    ensure_strategy(TEST_SID, name="settle selftest", enabled=False)
    record_entry(
        strategy_id=TEST_SID,
        strategy_name="settle selftest",
        symbol=symbol,
        qty=qty,
        price=entry,
        reason="selftest entry after the fill",
    )
    # Claimed *after* the only fill on record, so that fill belongs to an
    # earlier trade and must not be used to price this one.
    changes = book.reconcile({}, protected=set(), grace_sec=0, symbols=[symbol])
    check("no settlement from a pre-entry fill", settle_broker_exits(client, changes), [])
    check("journal left open for mark_stale", journal_rows()[0][4], "open")

    print("\n5. a partial exit splits the journal instead of closing it all")
    cleanup()
    from core.trade_log import close_journal

    ensure_strategy(TEST_SID, name="settle selftest", enabled=False)
    record_entry(
        strategy_id=TEST_SID,
        strategy_name="settle selftest",
        symbol=symbol,
        qty=80,
        price=100.0,
        reason="selftest partial",
    )
    close_journal(TEST_SID, symbol, 110.0, "trim", qty=30)
    rows = journal_rows()
    opened = [r for r in rows if r[4] == "open"]
    closed = [r for r in rows if r[4] == "closed"]
    check("one row still open", len(opened), 1)
    check("remaining qty on the open row", float(opened[0][1]), 50.0)
    check("one row settled", len(closed), 1)
    check("settled qty is what was sold", float(closed[0][1]), 30.0)
    check("partial pnl on sold shares only", round(float(closed[0][5]), 2), 300.0)

    # Selling the rest must close it out rather than split again.
    close_journal(TEST_SID, symbol, 120.0, "final", qty=50)
    rows = journal_rows()
    check("nothing left open", [r for r in rows if r[4] == "open"], [])
    check("total realized across both slices", round(sum(float(r[5]) for r in rows), 2), 1300.0)

    cleanup()
    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
