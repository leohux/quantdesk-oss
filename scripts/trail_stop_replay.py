# -*- coding: utf-8 -*-
"""Replay profit-protecting trailing stop on strategy-046bfa closed trades.

Compares actual journal exits vs a daily-OHLC simulation with:
  hard SL -8% / hard TP +15%  (baseline path)
  + trail activate +13% / trail 7.5% from peak
  DISABLE_HARD_TP_AFTER_TRAIL = False  (TP still live after trail)

Intraday path ambiguity on a day that touches both stop and TP:
  pessimistic = stop first; optimistic = TP first. We report both.

Usage:
  python /app/scripts/trail_stop_replay.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.db import SyncSessionLocal
from data.loader import load_ohlcv

STRATEGY_ID = "strategy-046bfa"
HARD_SL = -0.08
HARD_TP = 0.15
TRAIL_ACTIVATE = 0.13
TRAIL_PCT = 0.075
DISABLE_HARD_TP_AFTER_TRAIL = False


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def simulate(
    symbol: str,
    entry: float,
    qty: float,
    opened_at: datetime,
    closed_at: datetime,
    *,
    intraday_priority: str = "pessimistic",
) -> dict[str, Any]:
    """Walk daily bars from entry date through a buffer after actual close."""
    opened_at = _as_utc(opened_at)
    closed_at = _as_utc(closed_at)
    assert opened_at and closed_at
    start = (opened_at - timedelta(days=5)).strftime("%Y-%m-%d")
    end = (closed_at + timedelta(days=10)).strftime("%Y-%m-%d")
    df = load_ohlcv(symbol, start=start)
    if df is None or df.empty:
        return {"error": "no_ohlcv"}

    hard_sl = entry * (1.0 + HARD_SL)
    hard_tp = entry * (1.0 + HARD_TP)
    peak = entry
    trail_active = False
    trail_stop: float | None = None
    events: list[str] = []

    entry_day = opened_at.date()
    # Allow sim to run past actual close so we see if trail would hold longer.
    last_day = (closed_at + timedelta(days=5)).date()

    for idx in df.index:
        if hasattr(idx, "date"):
            d = idx.date()
        else:
            d = datetime.fromisoformat(str(idx)[:10]).date()
        if d < entry_day:
            continue
        if d > last_day:
            break

        high = float(df.loc[idx, "High"] if "High" in df.columns else df.loc[idx, "high"])
        low = float(df.loc[idx, "Low"] if "Low" in df.columns else df.loc[idx, "low"])
        close = float(df.loc[idx, "Close"] if "Close" in df.columns else df.loc[idx, "close"])

        # Update peak from session high
        if high > peak:
            peak = high

        gain_hi = peak / entry - 1.0
        if not trail_active and gain_hi >= TRAIL_ACTIVATE:
            trail_active = True
            trail_stop = peak * (1.0 - TRAIL_PCT)
            events.append(f"{d} TRAIL_ACTIVATE peak={peak:.4f} stop={trail_stop:.4f}")

        if trail_active:
            cand = peak * (1.0 - TRAIL_PCT)
            if trail_stop is None or cand > trail_stop:
                trail_stop = cand
                events.append(f"{d} TRAIL_UPDATE stop={trail_stop:.4f} peak={peak:.4f}")

        hit_sl = low <= hard_sl
        hit_tp = high >= hard_tp and not (
            trail_active and DISABLE_HARD_TP_AFTER_TRAIL
        )
        hit_trail = trail_active and trail_stop is not None and low <= trail_stop

        exit_px = None
        reason = None
        if intraday_priority == "pessimistic":
            # stops before targets
            if hit_sl and (not trail_active or (trail_stop is not None and hard_sl <= trail_stop)):
                # initial hard SL still binding until trail raises stop above it
                if not trail_active or (trail_stop is not None and hard_sl >= trail_stop - 1e-9):
                    if hit_sl and (not trail_active or hard_sl >= (trail_stop or 0)):
                        pass
            # Order: hard_sl (if still the binding stop), trail, then tp
            binding_stop = hard_sl
            stop_reason = "hard_sl"
            if trail_active and trail_stop is not None and trail_stop > hard_sl:
                binding_stop = trail_stop
                stop_reason = "trail_stop"
            if low <= binding_stop:
                exit_px = binding_stop
                reason = stop_reason
            elif hit_tp:
                exit_px = hard_tp
                reason = "hard_tp"
        else:
            # optimistic: TP before stops
            if hit_tp:
                exit_px = hard_tp
                reason = "hard_tp"
            else:
                binding_stop = hard_sl
                stop_reason = "hard_sl"
                if trail_active and trail_stop is not None and trail_stop > hard_sl:
                    binding_stop = trail_stop
                    stop_reason = "trail_stop"
                if low <= binding_stop:
                    exit_px = binding_stop
                    reason = stop_reason

        if exit_px is not None:
            pnl = (exit_px - entry) * qty
            ret = exit_px / entry - 1.0
            return {
                "symbol": symbol,
                "exit_date": str(d),
                "exit_price": exit_px,
                "exit_reason": reason,
                "realized_pnl": pnl,
                "return_pct": ret * 100,
                "trail_activated": trail_active,
                "peak_price": peak,
                "trail_stop": trail_stop,
                "events": events,
                "priority": intraday_priority,
                "last_close_seen": close,
            }

    # Still open at end of window — mark at last close
    last = df.iloc[-1]
    close = float(last["Close"] if "Close" in df.columns else last["close"])
    return {
        "symbol": symbol,
        "exit_date": None,
        "exit_price": close,
        "exit_reason": "still_open_mtm",
        "realized_pnl": (close - entry) * qty,
        "return_pct": (close / entry - 1.0) * 100,
        "trail_activated": trail_active,
        "peak_price": peak,
        "trail_stop": trail_stop,
        "events": events,
        "priority": intraday_priority,
    }


def main() -> int:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, symbol, qty, entry_price, exit_price, realized_pnl,
                       return_pct, opened_at, closed_at
                FROM trade_journal
                WHERE strategy_id = :sid AND status = 'closed'
                ORDER BY opened_at
                """
            ),
            {"sid": STRATEGY_ID},
        ).mappings().all()
    finally:
        s.close()

    print(
        f"trail replay activate={TRAIL_ACTIVATE:.0%} trail={TRAIL_PCT:.1%} "
        f"hard_sl={HARD_SL:.0%} hard_tp={HARD_TP:.0%} "
        f"disable_tp_after_trail={DISABLE_HARD_TP_AFTER_TRAIL}"
    )
    print(f"closed trades n={len(rows)}\n")

    actual_sum = 0.0
    sim_pess_sum = 0.0
    sim_opt_sum = 0.0

    for r in rows:
        entry = float(r["entry_price"])
        qty = float(r["qty"])
        actual = float(r["realized_pnl"] or 0)
        actual_sum += actual
        pess = simulate(
            r["symbol"], entry, qty, r["opened_at"], r["closed_at"],
            intraday_priority="pessimistic",
        )
        opt = simulate(
            r["symbol"], entry, qty, r["opened_at"], r["closed_at"],
            intraday_priority="optimistic",
        )
        if pess.get("error"):
            print(f"id={r['id']} {r['symbol']} ERROR {pess['error']}")
            continue
        sim_pess_sum += float(pess["realized_pnl"])
        sim_opt_sum += float(opt["realized_pnl"])
        delta_p = float(pess["realized_pnl"]) - actual
        print(
            f"id={r['id']:<4} {r['symbol']:5s} qty={qty:g}\n"
            f"  actual   exit={float(r['exit_price']):.4f}  pnl=${actual:+.2f}  "
            f"ret={float(r['return_pct'] or 0):+.2f}%\n"
            f"  trail_pess reason={pess['exit_reason']:16s} exit={pess['exit_price']:.4f}  "
            f"pnl=${pess['realized_pnl']:+.2f}  Δ=${delta_p:+.2f}  "
            f"activated={pess['trail_activated']} peak={pess['peak_price']:.4f}\n"
            f"  trail_opt  reason={opt['exit_reason']:16s} exit={opt['exit_price']:.4f}  "
            f"pnl=${opt['realized_pnl']:+.2f}  activated={opt['trail_activated']}"
        )
        for ev in (pess.get("events") or [])[:6]:
            print(f"    {ev}")
        if len(pess.get("events") or []) > 6:
            print(f"    … +{len(pess['events'])-6} more events")
        print()

    print("=" * 72)
    print(f"actual total     ${actual_sum:+,.2f}")
    print(f"trail pess total ${sim_pess_sum:+,.2f}  Δ=${sim_pess_sum-actual_sum:+,.2f}")
    print(f"trail opt  total ${sim_opt_sum:+,.2f}  Δ=${sim_opt_sum-actual_sum:+,.2f}")
    print(
        "\nNote: daily-bar path is an approximation of the live 90s scanner + "
        "broker stops; use for directional impact, not exact fill prices."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
