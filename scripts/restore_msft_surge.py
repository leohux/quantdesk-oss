"""
Restore the MSFT lot force-sold by MiMo into Paper holdings.

1. Buy 36 MSFT at the broker (real paper position)
2. Sleeve-claim for 美股科技股突破延续 @ original cost 399.04 (coexist with ATRBreak)
3. Re-open trade_journal for that strategy

ATRBreak's 52 @ ~464 is left untouched.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid

sys.path.insert(0, "/app")

STRATEGY_ID = "strategy-046bfa"
STRATEGY_NAME = "美股科技股突破延续"
SYMBOL = "MSFT"
QTY = 36.0
ENTRY = 399.04  # original cost for strategy attribution / future exit decisions
SOURCE_TRADE_ID = "e428c9e5-c68"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-buy", action="store_true", help="ledger/journal only")
    args = ap.parse_args()

    from config.store import list_strategies
    from core.portfolio.sleeve import SleeveBook, ensure_schema
    from execution.alpaca_client import AlpacaPaperClient
    from sqlalchemy import text
    from core.db import SyncSessionLocal

    ensure_schema()
    client = AlpacaPaperClient()
    positions = {str(p["symbol"]).upper(): p for p in client.positions()}
    msft = positions.get(SYMBOL)
    broker_qty = float(msft["qty"]) if msft else 0.0

    book = SleeveBook.load()
    surge_qty = book.qty_of(STRATEGY_ID, SYMBOL)
    atr_lots = [
        p for p in book.owners_of(SYMBOL)
        if "atrbreak" in p.strategy_id.lower() or "hybrid" in p.strategy_id.lower()
    ]

    print("=== restore MSFT surge lot ===")
    print(f"  broker MSFT qty now: {broker_qty}")
    print(f"  surge sleeve qty:    {surge_qty}")
    print(f"  other lots:          {[(p.strategy_id, p.qty, p.avg_price) for p in book.owners_of(SYMBOL)]}")
    print(f"  will buy:            {QTY} @ market (ledger cost {ENTRY})")

    names = {s["id"]: s.get("name") for s in list_strategies()}
    print(f"  strategy: {STRATEGY_ID} ({names.get(STRATEGY_ID) or STRATEGY_NAME})")

    if not args.apply:
        print("\nDRY-RUN — pass --apply to execute")
        return 0

    # Close any leftover virtual reconstruction row (from aborted design).
    try:
        s = SyncSessionLocal()
        try:
            s.execute(text("DROP TABLE IF EXISTS strategy_virtual_positions"))
            s.commit()
            print("  dropped strategy_virtual_positions (if any)")
        finally:
            s.close()
    except Exception as exc:
        print(f"  virtual cleanup skipped: {exc}")

    fill_px = ENTRY
    filled_ok = args.skip_buy
    if not args.skip_buy:
        if surge_qty >= QTY - 1e-9 and broker_qty >= 52 + QTY - 1e-9:
            print("  buy skipped — surge lot already present at broker+sleeve")
            filled_ok = True
        else:
            # Reuse an already-working buy if we submitted earlier while closed.
            oid = None
            try:
                for o in client.orders(status="open", limit=100) or []:
                    if (
                        str(o.get("symbol") or "").upper() == SYMBOL
                        and str(o.get("side") or "").lower() == "buy"
                        and float(o.get("qty") or 0) >= QTY - 1e-9
                    ):
                        oid = o.get("id")
                        print(f"  reusing open buy order id={oid} status={o.get('status')}")
                        break
            except Exception as exc:
                print(f"  open-order scan skipped: {exc}")

            if not oid:
                print(f"  submitting market BUY {QTY} {SYMBOL} ...")
                order = client.market_order(SYMBOL, QTY, "buy")
                oid = order.get("id")
                print(f"  order id={oid} status={order.get('status')}")

            # Wait for fill — do NOT claim sleeve until broker has the shares,
            # or reconcile will immediately shrink the coexist lot.
            for _ in range(45):
                time.sleep(1)
                try:
                    o = client.get_order(str(oid))
                except Exception:
                    break
                st = str(o.get("status") or "")
                fq = float(o.get("filled_qty") or 0)
                if st == "filled" or fq >= QTY - 1e-9:
                    fill_px = float(o.get("filled_avg_price") or fill_px)
                    print(f"  filled @ {fill_px}")
                    filled_ok = True
                    break
                if st in ("canceled", "expired", "rejected"):
                    print(f"  order {st}: {o}")
                    return 1
            if not filled_ok:
                print(
                    "  BUY not filled (market closed?). Order left working.\n"
                    "  Re-run after the open:\n"
                    "    python /app/scripts/restore_msft_surge.py --apply\n"
                    "  (script reuses the open buy, then claims sleeve @ 399.04)"
                )
                return 2

    if args.skip_buy:
        # Require broker evidence before claiming.
        positions = {str(p["symbol"]).upper(): p for p in client.positions()}
        broker_qty = float((positions.get(SYMBOL) or {}).get("qty") or 0)
        if broker_qty + 1e-9 < 52 + QTY:
            print(
                f"  FAIL: broker MSFT qty={broker_qty}, need >= {52 + QTY}. "
                "Wait for the buy to fill."
            )
            return 2
        filled_ok = True

    if not filled_ok:
        print("  FAIL: no fill — not claiming sleeve")
        return 2

    # Sleeve: strategy attribution uses ORIGINAL entry 399.04 so P&L matches intent.
    book = SleeveBook.load()
    ok = book.claim(
        STRATEGY_ID,
        SYMBOL,
        QTY,
        ENTRY,
        source="restore_collision",
        coexist=True,
    )
    if not ok:
        print("  FAIL: sleeve claim rejected")
        return 1
    print(f"  sleeve claimed {STRATEGY_ID} {SYMBOL} x{QTY} @ {ENTRY} (coexist)")

    # Keep ATRBreak lot intact — verify still there.
    book = SleeveBook.load()
    print("  sleeves now:", [(p.strategy_id, p.qty, p.avg_price) for p in book.owners_of(SYMBOL)])

    # Journal: open (or re-open) attribution row at original cost.
    s = SyncSessionLocal()
    try:
        existing = s.execute(
            text(
                """
                SELECT id, status FROM trade_journal
                WHERE strategy_id = :sid AND symbol = :sym
                  AND ABS(COALESCE(entry_price,0) - :entry) < 0.05
                  AND qty = :qty
                ORDER BY opened_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"sid": STRATEGY_ID, "sym": SYMBOL, "entry": ENTRY, "qty": QTY},
        ).mappings().fetchone()

        if existing and existing["status"] == "open":
            print(f"  journal already open id={existing['id']}")
        elif existing:
            s.execute(
                text(
                    """
                    UPDATE trade_journal SET
                        status = 'open',
                        side = 'buy',
                        exit_price = NULL,
                        realized_pnl = NULL,
                        return_pct = NULL,
                        closed_at = NULL,
                        entry_price = :entry,
                        qty = :qty,
                        signal_reason = 'restored after mimo force-exit; original surge entry'
                    WHERE id = :id
                    """
                ),
                {"id": existing["id"], "entry": ENTRY, "qty": QTY},
            )
            s.commit()
            print(f"  journal reopened id={existing['id']}")
        else:
            tid = SOURCE_TRADE_ID if SOURCE_TRADE_ID else str(uuid.uuid4())[:12]
            s.execute(
                text(
                    """
                    INSERT INTO trade_journal
                        (trade_id, strategy_id, strategy_name, symbol, side, status,
                         qty, entry_price, signal_reason, opened_at, created_at)
                    VALUES
                        (:tid, :sid, :sname, :sym, 'buy', 'open',
                         :qty, :entry, :reason, NOW(), NOW())
                    """
                ),
                {
                    "tid": tid,
                    "sid": STRATEGY_ID,
                    "sname": STRATEGY_NAME,
                    "sym": SYMBOL,
                    "qty": QTY,
                    "entry": ENTRY,
                    "reason": "restored after mimo force-exit; original surge entry",
                },
            )
            s.commit()
            print(f"  journal inserted trade_id={tid}")
    finally:
        s.close()

    positions = {str(p["symbol"]).upper(): p for p in client.positions()}
    msft = positions.get(SYMBOL)
    print(
        f"  broker now: qty={msft.get('qty') if msft else 0} "
        f"avg={msft.get('avg_entry_price') if msft else None} "
        f"mark={msft.get('current_price') if msft else None}"
    )
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
