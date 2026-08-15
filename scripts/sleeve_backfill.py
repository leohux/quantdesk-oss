"""
Seed the strategy_positions ledger from existing broker positions.
==================================================================
One-off (and re-runnable) import. For every position the broker reports, work
out which strategy opened it and write the ownership row.

Evidence is ranked by reliability:
  1. `transactions` — real order flow written at submit time, carries strategy_id
  2. `trade_journal` — open rows, noisier (duplicates, never-closed rows)
  3. give up and mark the position unassigned, which freezes it from strategies

Usage:
  python /app/scripts/sleeve_backfill.py            # dry run, prints the plan
  python /app/scripts/sleeve_backfill.py --apply
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

from core.portfolio.sleeve import UNASSIGNED, SleeveBook, ensure_schema
from execution.alpaca_client import AlpacaPaperClient


def _rows(sql: str) -> list[tuple]:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        return list(s.execute(text(sql)).fetchall())
    finally:
        s.close()


def transaction_candidates() -> dict[str, list[tuple[str, float]]]:
    """{symbol: [(strategy_id, net_qty), ...]} ordered most-recent first."""
    rows = _rows(
        """
        SELECT symbol, strategy_id,
               SUM(CASE WHEN side = 'buy' THEN qty ELSE -qty END) AS net_qty,
               MAX(created_at) AS last_at
        FROM transactions
        WHERE strategy_id IS NOT NULL
        GROUP BY 1, 2
        HAVING SUM(CASE WHEN side = 'buy' THEN qty ELSE -qty END) > 0
        ORDER BY symbol, last_at DESC
        """
    )
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol, sid, net_qty, _ in rows:
        out.setdefault(str(symbol), []).append((str(sid), float(net_qty)))
    return out


def journal_candidates() -> dict[str, list[tuple[str, float]]]:
    rows = _rows(
        """
        SELECT symbol, strategy_id, qty, opened_at
        FROM trade_journal
        WHERE status = 'open' AND side = 'buy' AND qty > 0
        ORDER BY symbol, opened_at DESC
        """
    )
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol, sid, qty, _ in rows:
        out.setdefault(str(symbol), []).append((str(sid), float(qty)))
    return out


def _pick(cands: list[tuple[str, float]], qty: float) -> str | None:
    """Prefer an exact quantity match, else the most recent candidate."""
    if not cands:
        return None
    for sid, q in cands:
        if abs(q - qty) < 1e-9:
            return sid
    return cands[0][0]


def attribute(symbol: str, qty: float, tx, tj) -> tuple[str, str]:
    """Return (strategy_id, evidence)."""
    sid = _pick(tx.get(symbol, []), qty)
    if sid:
        return sid, "transactions"
    sid = _pick(tj.get(symbol, []), qty)
    if sid:
        return sid, "trade_journal"
    return UNASSIGNED, "none"


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed strategy_positions from broker state")
    ap.add_argument("--apply", action="store_true", help="write rows (default: dry run)")
    args = ap.parse_args()

    ensure_schema()

    positions = AlpacaPaperClient().positions()
    tx, tj = transaction_candidates(), journal_candidates()
    book = SleeveBook.load()

    print(f"{'SYMBOL':7s} {'QTY':>7s} {'AVG':>9s}  {'OWNER':<60s} EVIDENCE")
    print("-" * 110)

    plan: list[tuple[str, str, float, float]] = []
    for p in sorted(positions, key=lambda x: x["symbol"]):
        symbol = str(p["symbol"])
        qty = float(p.get("qty") or 0)
        avg = float(p.get("avg_entry_price") or 0)
        if qty <= 0:
            continue

        existing = book.owner_of(symbol)
        if existing and existing != UNASSIGNED:
            print(f"{symbol:7s} {qty:7.0f} {avg:9.2f}  {existing:<60s} already-owned")
            continue

        sid, evidence = attribute(symbol, qty, tx, tj)
        print(f"{symbol:7s} {qty:7.0f} {avg:9.2f}  {sid:<60s} {evidence}")
        plan.append((sid, symbol, qty, avg))

    unassigned = [s for sid, s, _, _ in plan if sid == UNASSIGNED]
    print("-" * 110)
    print(f"{len(plan)} position(s) to assign, {len(unassigned)} unattributable")
    if unassigned:
        print(f"  frozen (no strategy may trade these): {', '.join(unassigned)}")

    if not args.apply:
        print("\ndry run — re-run with --apply to write the ledger")
        return

    for sid, symbol, qty, avg in plan:
        # An orphan row from an earlier reconcile blocks the real owner's claim.
        book.release(UNASSIGNED, symbol)
        ok = book.claim(sid, symbol, qty, avg, source="backfill")
        print(f"  {'OK  ' if ok else 'FAIL'} {symbol:7s} -> {sid}")

    print(f"\nledger now holds {len(SleeveBook.load().holdings())} position(s)")


if __name__ == "__main__":
    main()
