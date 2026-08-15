# -*- coding: utf-8 -*-
"""Clean phase6 journal pollution + backfill stale PnL (MiMo / ATRBreak / Surge-NVDA).

Root cause (two layers, same era — pre journal_repair 2026-08-01):
  1) SIGNAL NOISE: every non-HOLD signal was inserted into trade_journal.
     Exit signals became side='sell' status='closed' with NULL exit/PnL and
     reason like 'entry=False, exit=True, pos=True'. These are not lots.
  2) STALE BUYS: buy signal rows left open until journal_repair / mark_stale
     retired them without an exit price (same class as strategy-046bfa stale).

MiMo today has ONLY (1) — zero buy rows — so we also reconstruct the two
broker-confirmed round-trips (AAPL×56, MSFT×36 on 2026-07-28→29).

Usage:
  python /app/scripts/backfill_phase6_journal_pnl.py
  python /app/scripts/backfill_phase6_journal_pnl.py --apply
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.db import SyncSessionLocal
from core.trade_log import insert
from execution.alpaca_client import AlpacaPaperClient

STRATEGIES = (
    "mimo-mean-reversion-rsi-extreme-fac1bf",
    "cursor-hybrid-classic-atrbreak-aapl-m15-061001-d4e35-44d4b4",
    "cursor-surge-nvda-052828-63859c-82d552",
)

# Broker-confirmed MiMo round-trips (orders.id matches broker fills).
MIMO_RECONSTRUCT = (
    {
        "strategy_id": "mimo-mean-reversion-rsi-extreme-fac1bf",
        "strategy_name": "MiMo-Mean-Reversion RSI Extreme",
        "symbol": "AAPL",
        "qty": 56.0,
        "entry_price": 339.762858,
        "exit_price": 339.62875,
        "opened_at": "2026-07-28T19:50:25.265507+00:00",
        "closed_at": "2026-07-29T19:50:17.240327+00:00",
        "entry_order": "641c98b4",
        "exit_order": "7b91d7ed-8010-4450-bff8-1fb530ccddfd",
        "note": "reconstructed from broker fills; mimo sell order id matches",
    },
    {
        "strategy_id": "mimo-mean-reversion-rsi-extreme-fac1bf",
        "strategy_name": "MiMo-Mean-Reversion RSI Extreme",
        "symbol": "MSFT",
        "qty": 36.0,
        "entry_price": 399.058889,
        "exit_price": 392.189445,
        "opened_at": "2026-07-28T13:37:32.389333+00:00",
        "closed_at": "2026-07-29T19:50:23.565667+00:00",
        "entry_order": "5707dc6e",
        "exit_order": "711ca61b-fe00-4be4-aa73-7c84ae79591d",
        "note": "reconstructed from broker fills; mimo sell order id matches",
    },
)


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
        return client.orders(status="all", limit=200, symbols=[symbol]) or []
    except TypeError:
        return [
            o
            for o in (client.orders(status="all", limit=500) or [])
            if str(o.get("symbol", "")).upper() == symbol
        ]


def _match_exit(orders, *, qty: float, opened_at) -> dict[str, Any] | None:
    open_utc = _as_utc(opened_at)
    if open_utc is None:
        return None
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


def _match_entry(orders, *, qty: float, opened_at) -> dict[str, Any] | None:
    open_utc = _as_utc(opened_at)
    if open_utc is None:
        return None
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


def plan_mark_phantoms() -> list[dict[str, Any]]:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, strategy_id, symbol, qty, entry_price, opened_at, signal_reason
                FROM trade_journal
                WHERE strategy_id = ANY(:sids)
                  AND side = 'sell'
                  AND status = 'closed'
                  AND exit_price IS NULL
                  AND realized_pnl IS NULL
                ORDER BY strategy_id, id
                """
            ),
            {"sids": list(STRATEGIES)},
        ).mappings().all()
    finally:
        s.close()
    return [
        {
            "action": "signal_noise",
            "id": int(r["id"]),
            "strategy_id": r["strategy_id"],
            "symbol": str(r["symbol"]).upper(),
            "qty": float(r["qty"] or 0),
            "note": "legacy signal log (side=sell closed without fill/PnL)",
        }
        for r in rows
    ]


def _reserved_sell_ids() -> set[str]:
    """Sell order ids already booked on closed rows or claimed by MiMo reconstruct."""
    reserved = {str(lot["exit_order"]) for lot in MIMO_RECONSTRUCT}
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT signal_reason FROM trade_journal
                WHERE status = 'closed'
                  AND exit_price IS NOT NULL
                  AND realized_pnl IS NOT NULL
                  AND signal_reason IS NOT NULL
                """
            )
        ).fetchall()
    finally:
        s.close()
    for (reason,) in rows:
        text_r = str(reason or "")
        if "order=" in text_r:
            oid = text_r.rsplit("order=", 1)[-1].strip().split()[0]
            if oid:
                reserved.add(oid)
    return reserved


def _booked_lot_keys() -> set[tuple[str, float, float]]:
    """(symbol, qty, exit_px) already on a closed booked row — avoid double-count."""
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT symbol, qty, exit_price FROM trade_journal
                WHERE status = 'closed'
                  AND exit_price IS NOT NULL
                  AND realized_pnl IS NOT NULL
                  AND side = 'buy'
                """
            )
        ).mappings().all()
    finally:
        s.close()
    out: set[tuple[str, float, float]] = set()
    for r in rows:
        out.add(
            (
                str(r["symbol"]).upper(),
                round(float(r["qty"] or 0), 4),
                round(float(r["exit_price"]), 4),
            )
        )
    # Reserve MiMo reconstruct lots too
    for lot in MIMO_RECONSTRUCT:
        out.add(
            (
                lot["symbol"],
                round(float(lot["qty"]), 4),
                round(float(lot["exit_price"]), 4),
            )
        )
    return out


def plan_stale_backfill(client: AlpacaPaperClient) -> list[dict[str, Any]]:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, strategy_id, symbol, qty, entry_price, opened_at, signal_reason
                FROM trade_journal
                WHERE strategy_id = ANY(:sids)
                  AND status = 'stale'
                  AND side = 'buy'
                ORDER BY strategy_id, opened_at NULLS LAST, id
                """
            ),
            {"sids": list(STRATEGIES)},
        ).mappings().all()
    finally:
        s.close()

    plans: list[dict[str, Any]] = []
    cache: dict[str, list] = {}
    used_sell_ids: set[str] = _reserved_sell_ids()
    booked_lots = _booked_lot_keys()

    for r in rows:
        sid = r["strategy_id"]
        symbol = str(r["symbol"]).upper()
        qty = float(r["qty"] or 0)
        opened_at = r["opened_at"]
        journal_entry = float(r["entry_price"]) if r["entry_price"] == r["entry_price"] else 0.0

        if qty <= 0 or journal_entry != journal_entry:
            plans.append(
                {
                    "action": "supersede_stale",
                    "id": int(r["id"]),
                    "strategy_id": sid,
                    "symbol": symbol,
                    "qty": qty,
                    "note": "invalid qty/entry — cannot backfill",
                }
            )
            continue

        if symbol not in cache:
            cache[symbol] = _orders_for(client, symbol)
        orders = cache[symbol]
        sell = _match_exit(orders, qty=qty, opened_at=opened_at)
        if sell is None:
            # Repeated signal-scan buys for lots that later migrated / never uniquely
            # matched — keep out of PnL rather than inventing an exit.
            plans.append(
                {
                    "action": "supersede_stale",
                    "id": int(r["id"]),
                    "strategy_id": sid,
                    "symbol": symbol,
                    "qty": qty,
                    "journal_entry": journal_entry,
                    "note": "no unique broker sell match (signal-scan duplicate / migrated lot)",
                }
            )
            continue

        sell_id = str(sell.get("id") or "")
        exit_px = float(sell["filled_avg_price"])
        lot_key = (symbol, round(qty, 4), round(exit_px, 4))
        if (sell_id and sell_id in used_sell_ids) or lot_key in booked_lots:
            plans.append(
                {
                    "action": "supersede_stale",
                    "id": int(r["id"]),
                    "strategy_id": sid,
                    "symbol": symbol,
                    "qty": qty,
                    "note": (
                        f"sell/lot already booked elsewhere "
                        f"(order={sell_id[:8] if sell_id else '?'} exit={exit_px:.4f})"
                    ),
                }
            )
            continue

        buy = _match_entry(orders, qty=qty, opened_at=opened_at)
        entry_px = float(buy["filled_avg_price"]) if buy else journal_entry
        exit_at = _parse(sell.get("filled_at") or sell.get("submitted_at"))
        open_utc = _as_utc(opened_at)
        hold_days = 0
        if open_utc and exit_at:
            hold_days = max(0, int((exit_at - open_utc).total_seconds()) // 86400)
        pnl = (exit_px - entry_px) * qty
        ret = (exit_px / entry_px - 1.0) * 100 if entry_px > 0 else None
        if sell_id:
            used_sell_ids.add(sell_id)
        booked_lots.add(lot_key)
        plans.append(
            {
                "action": "close_stale",
                "id": int(r["id"]),
                "strategy_id": sid,
                "symbol": symbol,
                "qty": qty,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "exit_at": exit_at,
                "holding_days": hold_days,
                "realized_pnl": pnl,
                "return_pct": ret,
                "exit_order_id": sell_id,
                "note": f"broker fill type={sell.get('type')}",
            }
        )
    return plans


def plan_mimo_reconstruct() -> list[dict[str, Any]]:
    s = SyncSessionLocal()
    try:
        existing = s.execute(
            text(
                """
                SELECT COUNT(*) FROM trade_journal
                WHERE strategy_id = :sid
                  AND side = 'buy'
                  AND status = 'closed'
                  AND exit_price IS NOT NULL
                  AND signal_reason LIKE '%reconstructed from broker fills%'
                """
            ),
            {"sid": "mimo-mean-reversion-rsi-extreme-fac1bf"},
        ).scalar()
    finally:
        s.close()
    if existing and int(existing) > 0:
        return [{"action": "skip_reconstruct", "note": f"already have {existing} reconstructed row(s)"}]

    out = []
    for lot in MIMO_RECONSTRUCT:
        entry = float(lot["entry_price"])
        exit_ = float(lot["exit_price"])
        qty = float(lot["qty"])
        pnl = (exit_ - entry) * qty
        ret = (exit_ / entry - 1.0) * 100
        opened = _parse(lot["opened_at"])
        closed = _parse(lot["closed_at"])
        hold = 0
        if opened and closed:
            hold = max(0, int((closed - opened).total_seconds()) // 86400)
        out.append(
            {
                "action": "insert_reconstructed",
                **lot,
                "realized_pnl": pnl,
                "return_pct": ret,
                "holding_days": hold,
            }
        )
    return out


def apply_plans(plans: list[dict[str, Any]]) -> None:
    s = SyncSessionLocal()
    try:
        for p in plans:
            act = p["action"]
            if act == "signal_noise":
                s.execute(
                    text(
                        """
                        UPDATE trade_journal
                        SET status = 'signal_noise',
                            closed_at = COALESCE(closed_at, NOW()),
                            signal_reason = COALESCE(signal_reason, '') ||
                                ' | classified: signal_noise (not a fill; excluded from PnL)'
                        WHERE id = :id AND side = 'sell' AND status = 'closed'
                          AND exit_price IS NULL
                        """
                    ),
                    {"id": p["id"]},
                )
            elif act == "supersede_stale":
                s.execute(
                    text(
                        """
                        UPDATE trade_journal
                        SET status = 'superseded',
                            signal_reason = COALESCE(signal_reason, '') ||
                                ' | superseded: ' || :note
                        WHERE id = :id AND status = 'stale'
                        """
                    ),
                    {"id": p["id"], "note": p["note"][:200]},
                )
            elif act == "close_stale":
                s.execute(
                    text(
                        """
                        UPDATE trade_journal
                        SET status = 'closed',
                            side = 'buy',
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
                        "oid": str(p.get("exit_order_id") or "")[:36],
                    },
                )
            elif act == "insert_reconstructed":
                # insert() opens its own session — commit current first
                s.commit()
                insert(
                    "trade_journal",
                    {
                        "trade_id": str(uuid.uuid4())[:12],
                        "strategy_id": p["strategy_id"],
                        "strategy_name": p["strategy_name"],
                        "symbol": p["symbol"],
                        "side": "buy",
                        "signal_reason": f"reconstructed from broker fills | {p['note']}",
                        "entry_price": float(p["entry_price"]),
                        "exit_price": float(p["exit_price"]),
                        "qty": float(p["qty"]),
                        "status": "closed",
                        "opened_at": p["opened_at"],
                        "closed_at": p["closed_at"],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "realized_pnl": float(p["realized_pnl"]),
                        "return_pct": float(p["return_pct"]),
                        "holding_days": int(p["holding_days"]),
                    },
                )
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def summarize(plans: list[dict[str, Any]]) -> None:
    by = {}
    for p in plans:
        by.setdefault(p["action"], []).append(p)
    for act, items in sorted(by.items()):
        print(f"\n=== {act} ({len(items)}) ===")
        for p in items[:40]:
            if act == "close_stale":
                print(
                    f"  id={p['id']} {p['strategy_id'][:28]:28s} {p['symbol']:5s} "
                    f"qty={p['qty']:.0f} pnl=${p['realized_pnl']:+.2f}  {p['note']}"
                )
            elif act == "insert_reconstructed":
                print(
                    f"  NEW {p['symbol']:5s} qty={p['qty']:.0f} "
                    f"{p['entry_price']:.4f}->{p['exit_price']:.4f} "
                    f"pnl=${p['realized_pnl']:+.2f}"
                )
            else:
                print(
                    f"  id={p.get('id','-')} {str(p.get('strategy_id',''))[:28]:28s} "
                    f"{p.get('symbol','?'):5s} qty={p.get('qty',0):.0f}  {p.get('note','')}"
                )
        if len(items) > 40:
            print(f"  ... +{len(items)-40} more")


def post_report() -> None:
    s = SyncSessionLocal()
    try:
        print("\n=== POST STATUS ===")
        for sid in STRATEGIES:
            rows = s.execute(
                text(
                    """
                    SELECT status, side, COUNT(*) n,
                           COUNT(*) FILTER (WHERE realized_pnl IS NOT NULL) with_pnl,
                           COALESCE(SUM(realized_pnl) FILTER (
                               WHERE status='closed' AND exit_price IS NOT NULL
                           ), 0) booked
                    FROM trade_journal WHERE strategy_id=:sid
                    GROUP BY 1,2 ORDER BY 1,2
                    """
                ),
                {"sid": sid},
            ).mappings().all()
            print(f"\n{sid}")
            for r in rows:
                print(dict(r))
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client = AlpacaPaperClient()
    plans: list[dict[str, Any]] = []
    plans += plan_mark_phantoms()
    plans += plan_stale_backfill(client)
    plans += plan_mimo_reconstruct()
    summarize(plans)

    n_noise = sum(1 for p in plans if p["action"] == "signal_noise")
    n_close = sum(1 for p in plans if p["action"] == "close_stale")
    n_recon = sum(1 for p in plans if p["action"] == "insert_reconstructed")
    pnl_close = sum(float(p["realized_pnl"]) for p in plans if p["action"] == "close_stale")
    pnl_recon = sum(float(p["realized_pnl"]) for p in plans if p["action"] == "insert_reconstructed")
    print(
        f"\nTOTAL signal_noise={n_noise} close_stale={n_close} "
        f"reconstruct={n_recon} stale_pnl=${pnl_close:+.2f} mimo_pnl=${pnl_recon:+.2f}"
    )

    if not args.apply:
        print("\ndry run — re-run with --apply to write")
        return 0

    apply_plans(plans)
    print("\nAPPLIED")
    post_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
