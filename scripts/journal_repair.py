"""
Rebuild trade_journal open rows from the sleeve ledger.
=======================================================
Until entries were only journalled on a real submission, every non-HOLD signal
inserted a row with status='open' — including dry runs and rejected orders, and
including one row per scan for a position that was opened once. The table ended
up with 58 open rows describing 8 actual positions.

This retires every open row and re-opens exactly one per ledger position, using
the broker's average entry price. Retired rows keep status='stale' rather than
'closed' because their true exit price is unknown and inventing one would
corrupt the P&L the journal now feeds.

Usage:
  python /app/scripts/journal_repair.py           # dry run
  python /app/scripts/journal_repair.py --apply
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from core.portfolio.sleeve import UNASSIGNED, SleeveBook, ensure_schema
from core.trade_log import insert


def _fetch(sql: str, params: dict | None = None) -> list[tuple]:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        return list(s.execute(text(sql), params or {}).fetchall())
    finally:
        s.close()


def _execute(sql: str, params: dict | None = None) -> int:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        result = s.execute(text(sql), params or {})
        s.commit()
        return int(result.rowcount or 0)
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild trade_journal from the sleeve ledger")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    ensure_schema()
    book = SleeveBook.load()
    held = {s: p for s, p in book.holdings().items() if p.strategy_id != UNASSIGNED}

    open_rows = _fetch(
        "SELECT strategy_id, symbol, COUNT(*) FROM trade_journal "
        "WHERE status = 'open' GROUP BY 1, 2 ORDER BY 2"
    )
    total_open = sum(int(r[2]) for r in open_rows)

    print(f"trade_journal has {total_open} open row(s) across {len(open_rows)} strategy/symbol pair(s)")
    for sid, symbol, n in open_rows:
        owned = symbol in held and held[symbol].strategy_id == sid
        note = "matches ledger" if owned else "no ledger position"
        print(f"  {symbol:6s} {int(n):3d} row(s)  {sid}  [{note}]")

    print(f"\nledger holds {len(held)} position(s); will re-open one row each:")
    for symbol, pos in sorted(held.items()):
        print(f"  {symbol:6s} {pos.qty:>7.0f} @ {pos.avg_price:8.2f}  {pos.strategy_id}")

    if not args.apply:
        print("\ndry run — re-run with --apply to rewrite")
        return

    retired = _execute(
        "UPDATE trade_journal SET status = 'stale', closed_at = NOW() WHERE status = 'open'"
    )
    print(f"\nretired {retired} row(s)")

    at = datetime.now(timezone.utc).isoformat()
    written = 0
    for symbol, pos in sorted(held.items()):
        ok = insert(
            "trade_journal",
            {
                "trade_id": str(uuid.uuid4())[:12],
                "strategy_id": pos.strategy_id,
                "strategy_name": pos.strategy_id,
                "symbol": symbol,
                "side": "buy",
                "signal_reason": "rebuilt from sleeve ledger",
                "entry_price": pos.avg_price,
                "qty": pos.qty,
                "status": "open",
                "opened_at": at,
                "created_at": at,
            },
        )
        written += 1 if ok else 0
    print(f"re-opened {written} row(s)")

    check = _fetch("SELECT status, COUNT(*) FROM trade_journal GROUP BY 1 ORDER BY 1")
    print("\ntrade_journal now:")
    for status, n in check:
        print(f"  {status:8s} {int(n)}")


if __name__ == "__main__":
    main()
