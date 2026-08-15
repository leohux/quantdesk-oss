"""
Backfill realized P&L for strategy-046bfa stale journal rows.
=============================================================
Matches each stale buy to the broker sell that closed that lot (qty + time
window after open). CRM/CRWD stale rows from journal_repair (2026-08-01) are
marked superseded — the live lot was rebooked and already settled as closed.

Usage:
  python /app/scripts/backfill_stale_surge_pnl.py           # dry run
  python /app/scripts/backfill_stale_surge_pnl.py --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.db import SyncSessionLocal
from execution.alpaca_client import AlpacaPaperClient

STRATEGY_ID = "strategy-046bfa"

# journal_repair at 2026-08-01 06:33 retired open rows and re-opened ledger lots.
# These two stale rows are the pre-repair copies of CRM/CRWD that continued.
SUPERSEDED_IDS = {67, 69}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _orders_for(client: AlpacaPaperClient, symbol: str) -> list[dict[str, Any]]:
    try:
        orders = client.orders(status="all", limit=200, symbols=[symbol]) or []
    except TypeError:
        orders = [
            o
            for o in (client.orders(status="all", limit=500) or [])
            if str(o.get("symbol", "")).upper() == symbol
        ]
    return orders


def _match_exit(
    orders: list[dict[str, Any]],
    *,
    qty: float,
    opened_at: datetime,
) -> dict[str, Any] | None:
    """First filled sell after open with matching qty (exact share lot)."""
    open_utc = _as_utc(opened_at)
    assert open_utc is not None
    sells = []
    for o in orders:
        if str(o.get("side", "")).lower() != "sell":
            continue
        if str(o.get("status", "")).lower() != "filled":
            continue
        filled_qty = float(o.get("filled_qty") or 0)
        px = o.get("filled_avg_price")
        filled_at = _parse(o.get("filled_at") or o.get("submitted_at"))
        if filled_qty <= 0 or px in (None, 0) or filled_at is None:
            continue
        if filled_at < open_utc:
            continue
        if abs(filled_qty - qty) > 1e-6:
            continue
        sells.append((filled_at, o))
    if not sells:
        return None
    sells.sort(key=lambda x: x[0])
    return sells[0][1]


def _match_entry(
    orders: list[dict[str, Any]],
    *,
    qty: float,
    opened_at: datetime,
) -> dict[str, Any] | None:
    open_utc = _as_utc(opened_at)
    assert open_utc is not None
    # Allow a few minutes of clock skew around journal vs broker submit.
    window_start = open_utc.timestamp() - 120
    window_end = open_utc.timestamp() + 600
    buys = []
    for o in orders:
        if str(o.get("side", "")).lower() != "buy":
            continue
        if str(o.get("status", "")).lower() != "filled":
            continue
        filled_qty = float(o.get("filled_qty") or 0)
        px = o.get("filled_avg_price")
        submitted = _parse(o.get("submitted_at") or o.get("filled_at"))
        if filled_qty <= 0 or px in (None, 0) or submitted is None:
            continue
        if abs(filled_qty - qty) > 1e-6:
            continue
        ts = submitted.timestamp()
        if window_start <= ts <= window_end:
            buys.append((abs(ts - open_utc.timestamp()), o))
    if not buys:
        return None
    buys.sort(key=lambda x: x[0])
    return buys[0][1]


def plan_backfill(client: AlpacaPaperClient) -> list[dict[str, Any]]:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, trade_id, symbol, qty, entry_price, opened_at, closed_at,
                       signal_reason, status
                FROM trade_journal
                WHERE strategy_id = :sid AND status = 'stale'
                ORDER BY opened_at NULLS LAST
                """
            ),
            {"sid": STRATEGY_ID},
        ).mappings().all()
    finally:
        s.close()

    plans: list[dict[str, Any]] = []
    cache: dict[str, list[dict[str, Any]]] = {}

    for r in rows:
        row_id = int(r["id"])
        symbol = str(r["symbol"]).upper()
        qty = float(r["qty"])
        opened_at = r["opened_at"]
        journal_entry = float(r["entry_price"] or 0)

        if row_id in SUPERSEDED_IDS:
            plans.append(
                {
                    "action": "supersede",
                    "id": row_id,
                    "trade_id": r["trade_id"],
                    "symbol": symbol,
                    "qty": qty,
                    "journal_entry": journal_entry,
                    "note": "journal_repair rebooked continuing lot; PnL lives on later closed/open rows",
                }
            )
            continue

        if symbol not in cache:
            cache[symbol] = _orders_for(client, symbol)
        orders = cache[symbol]
        buy = _match_entry(orders, qty=qty, opened_at=opened_at)
        sell = _match_exit(orders, qty=qty, opened_at=opened_at)

        if sell is None:
            plans.append(
                {
                    "action": "unmatched",
                    "id": row_id,
                    "trade_id": r["trade_id"],
                    "symbol": symbol,
                    "qty": qty,
                    "journal_entry": journal_entry,
                    "note": "no filled sell with matching qty after open",
                }
            )
            continue

        entry_px = float(buy["filled_avg_price"]) if buy else journal_entry
        exit_px = float(sell["filled_avg_price"])
        exit_at = _parse(sell.get("filled_at") or sell.get("submitted_at"))
        open_utc = _as_utc(opened_at)
        hold_days = 0
        if open_utc and exit_at:
            hold_days = max(0, int((exit_at - open_utc).total_seconds()) // 86400)
        pnl = (exit_px - entry_px) * qty
        ret = (exit_px / entry_px - 1.0) * 100 if entry_px > 0 else None
        otype = str(sell.get("type") or "")
        plans.append(
            {
                "action": "close",
                "id": row_id,
                "trade_id": r["trade_id"],
                "symbol": symbol,
                "qty": qty,
                "journal_entry": journal_entry,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "exit_at": exit_at,
                "holding_days": hold_days,
                "realized_pnl": pnl,
                "return_pct": ret,
                "exit_order_id": sell.get("id"),
                "exit_type": otype,
                "entry_order_id": (buy or {}).get("id"),
                "note": f"broker {otype or 'sell'} fill",
            }
        )
    return plans


def apply_plan(plans: list[dict[str, Any]]) -> None:
    s = SyncSessionLocal()
    try:
        for p in plans:
            if p["action"] == "supersede":
                s.execute(
                    text(
                        """
                        UPDATE trade_journal
                        SET status = 'superseded',
                            signal_reason = COALESCE(signal_reason, '') ||
                                ' | superseded: journal_repair rebook (no double-count PnL)'
                        WHERE id = :id AND status = 'stale'
                        """
                    ),
                    {"id": p["id"]},
                )
            elif p["action"] == "close":
                s.execute(
                    text(
                        """
                        UPDATE trade_journal
                        SET status = 'closed',
                            entry_price = :entry,
                            exit_price = :exit,
                            closed_at = :closed_at,
                            realized_pnl = :pnl,
                            return_pct = :ret,
                            holding_days = :hold,
                            signal_reason = COALESCE(signal_reason, '') ||
                                ' | backfill: ' || :note || ' order=' || :oid
                        WHERE id = :id AND status = 'stale'
                        """
                    ),
                    {
                        "id": p["id"],
                        "entry": p["entry_price"],
                        "exit": p["exit_price"],
                        "closed_at": p["exit_at"],
                        "pnl": p["realized_pnl"],
                        "ret": p["return_pct"],
                        "hold": p["holding_days"],
                        "note": p["note"],
                        "oid": str(p.get("exit_order_id") or ""),
                    },
                )
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def summarize(plans: list[dict[str, Any]]) -> None:
    print(f"{'ACT':10s} {'ID':>4s} {'SYM':6s} {'QTY':>8s} {'ENTRY':>10s} {'EXIT':>10s} {'PNL':>12s}  NOTE")
    print("-" * 100)
    closed_pnl = 0.0
    n_close = n_super = n_miss = 0
    for p in plans:
        act = p["action"]
        if act == "close":
            n_close += 1
            closed_pnl += float(p["realized_pnl"])
            print(
                f"{act:10s} {p['id']:4d} {p['symbol']:6s} {p['qty']:8.0f} "
                f"{p['entry_price']:10.4f} {p['exit_price']:10.4f} "
                f"{p['realized_pnl']:+12.2f}  {p['note']} ({p.get('exit_type')})"
            )
        elif act == "supersede":
            n_super += 1
            print(
                f"{act:10s} {p['id']:4d} {p['symbol']:6s} {p['qty']:8.0f} "
                f"{p['journal_entry']:10.4f} {'—':>10s} {'(exclude)':>12s}  {p['note']}"
            )
        else:
            n_miss += 1
            print(
                f"{act:10s} {p['id']:4d} {p['symbol']:6s} {p['qty']:8.0f} "
                f"{p['journal_entry']:10.4f} {'—':>10s} {'—':>12s}  {p['note']}"
            )
    print("-" * 100)
    print(f"close={n_close} supersede={n_super} unmatched={n_miss}")
    print(f"stale-lot realized (to book) = ${closed_pnl:+,.2f}")


def full_cycle_report() -> None:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT status, symbol, qty, entry_price, exit_price, realized_pnl,
                       return_pct, holding_days, opened_at, closed_at
                FROM trade_journal
                WHERE strategy_id = :sid
                ORDER BY COALESCE(opened_at, created_at)
                """
            ),
            {"sid": STRATEGY_ID},
        ).mappings().all()
    finally:
        s.close()

    print("\n=== FULL JOURNAL AFTER BACKFILL ===")
    realized = 0.0
    wins = losses = 0
    for r in rows:
        rp = r["realized_pnl"]
        print(
            f"{r['status']:10s} {r['symbol']:6s} qty={r['qty']} "
            f"entry={r['entry_price']} exit={r['exit_price']} "
            f"rpnl={rp} hold={r['holding_days']}d"
        )
        if r["status"] == "closed" and rp is not None:
            realized += float(rp)
            if float(rp) >= 0:
                wins += 1
            else:
                losses += 1

    from config.store import get_strategy

    client = AlpacaPaperClient()
    pos = {p["symbol"]: p for p in client.positions()}
    attr = 0.0
    print("\n=== OPEN SLEEVE MTM ===")
    s2 = SyncSessionLocal()
    try:
        open_lots = s2.execute(
            text(
                """
                SELECT symbol, qty, avg_price FROM strategy_positions
                WHERE strategy_id = :sid AND qty <> 0 ORDER BY symbol
                """
            ),
            {"sid": STRATEGY_ID},
        ).fetchall()
    finally:
        s2.close()

    for sym, qty, avg in open_lots:
        qty = float(qty)
        avg = float(avg or 0)
        bp = pos.get(sym)
        if not bp:
            print(f"{sym}: missing at broker qty={qty}")
            continue
        cur = float(bp["current_price"])
        u = (cur - avg) * qty
        attr += u
        print(f"{sym}: {qty:g}@{avg:.4f} px={cur:.4f} upnl={u:+.2f}")

    print(
        f"\nclosed realized=${realized:+,.2f}  wins={wins} losses={losses}  "
        f"open_attr_upnl=${attr:+,.2f}  total≈${realized + attr:+,.2f}"
    )
    st = get_strategy(STRATEGY_ID)
    print(f"strategy={st.get('name')} enabled={st.get('enabled')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill stale surge journal PnL from broker fills")
    ap.add_argument("--apply", action="store_true", help="write DB updates (default: dry run)")
    ap.add_argument("--report", action="store_true", help="print full-cycle summary after plan/apply")
    args = ap.parse_args()

    client = AlpacaPaperClient()
    plans = plan_backfill(client)
    summarize(plans)

    if not args.apply:
        print("\ndry run — re-run with --apply to write")
    else:
        apply_plan(plans)
        print("\napplied.")

    if args.apply or args.report:
        full_cycle_report()


if __name__ == "__main__":
    main()
