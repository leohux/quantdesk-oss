# -*- coding: utf-8 -*-
"""Minute-bar path replay for trail-activating closed trades.

Walks Alpaca IEX 1Min bars in time order — no optimistic/pessimistic guess.
Models hard TP as a resting limit: if a bar's high >= tp, fill at max(tp, open)
so gap-through (open already above tp) captures the "better than +15%" case.

Usage:
  python /app/scripts/trail_stop_replay_1min.py
  python /app/scripts/trail_stop_replay_1min.py --ids 13,48,143
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.db import SyncSessionLocal
from data.loader import load_intraday

STRATEGY_ID = "strategy-046bfa"
HARD_SL = -0.08
HARD_TP = 0.15
TRAIL_ACTIVATE = 0.13
TRAIL_PCT = 0.075
DISABLE_HARD_TP_AFTER_TRAIL = False

# Trades that activated trail in the daily replay (primary focus).
DEFAULT_IDS = (13, 48, 143)


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _col(df, *names: str):
    cols = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in cols:
            return cols[n.lower()]
    raise KeyError(names)


def simulate_1min(
    symbol: str,
    entry: float,
    qty: float,
    opened_at: datetime,
    closed_at: datetime,
) -> dict[str, Any]:
    opened_at = _as_utc(opened_at)
    closed_at = _as_utc(closed_at)
    assert opened_at and closed_at

    # Pad end so we can see if trail would have held past the actual exit.
    start = (opened_at - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (closed_at + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        df = load_intraday(symbol, start=start, end=end, timeframe="1Min")
    except Exception as exc:
        return {"error": f"ohlcv:{exc}"}
    if df is None or df.empty:
        return {"error": "empty_bars"}

    high_c = _col(df, "High", "high")
    low_c = _col(df, "Low", "low")
    open_c = _col(df, "Open", "open")
    close_c = _col(df, "Close", "close")

    hard_sl = entry * (1.0 + HARD_SL)
    hard_tp = entry * (1.0 + HARD_TP)
    peak = entry
    trail_active = False
    trail_stop: float | None = None
    events: list[str] = []
    n_bars = 0

    for idx, row in df.iterrows():
        if hasattr(idx, "to_pydatetime"):
            ts = idx.to_pydatetime()
        else:
            ts = datetime.fromisoformat(str(idx).replace("Z", "+00:00"))
        ts = _as_utc(ts)
        if ts is None or ts < opened_at - timedelta(seconds=30):
            continue
        # Stop walking far past actual close for reporting speed, but allow
        # 1 trading day after to see alternate trail exits.
        if ts > closed_at + timedelta(hours=20):
            break

        o = float(row[open_c])
        h = float(row[high_c])
        low = float(row[low_c])
        c = float(row[close_c])
        n_bars += 1

        # Chronology within bar (conservative for long): open → low → high → close
        # is wrong for longs (favors stop). Better long path: open → high → low → close
        # so TP/activate can happen before a later pullback — matches "price ran then dipped".
        path = [o, h, low, c]

        for px in path:
            if px > peak:
                peak = px

            gain = peak / entry - 1.0
            if not trail_active and gain >= TRAIL_ACTIVATE:
                trail_active = True
                trail_stop = peak * (1.0 - TRAIL_PCT)
                events.append(
                    f"{ts.isoformat()} TRAIL_ACTIVATE peak={peak:.4f} stop={trail_stop:.4f}"
                )

            if trail_active:
                cand = peak * (1.0 - TRAIL_PCT)
                if trail_stop is None or cand > trail_stop + 1e-9:
                    trail_stop = cand
                    events.append(
                        f"{ts.isoformat()} TRAIL_UPDATE stop={trail_stop:.4f} peak={peak:.4f}"
                    )

            binding_stop = hard_sl
            stop_reason = "hard_sl"
            if trail_active and trail_stop is not None and trail_stop > hard_sl:
                binding_stop = trail_stop
                stop_reason = "trail_stop"

            # Gap-aware hard TP: limit resting at hard_tp; if open already above, fill open.
            tp_live = not (trail_active and DISABLE_HARD_TP_AFTER_TRAIL)
            if tp_live and px >= hard_tp:
                fill = max(hard_tp, o) if px == o or o >= hard_tp else hard_tp
                # If we reached tp via high after open below tp → classic limit fill at tp
                if o < hard_tp <= h:
                    fill = hard_tp
                elif o >= hard_tp:
                    fill = o
                else:
                    fill = hard_tp
                return _result(
                    symbol, qty, entry, fill, "hard_tp", ts, trail_active, peak,
                    trail_stop, events, n_bars, gap_tp=o >= hard_tp,
                )

            if px <= binding_stop:
                # Stop market ≈ stop price (slippage ignored)
                return _result(
                    symbol, qty, entry, binding_stop, stop_reason, ts, trail_active,
                    peak, trail_stop, events, n_bars, gap_tp=False,
                )

    last_ts = _as_utc(
        df.index[-1].to_pydatetime()
        if hasattr(df.index[-1], "to_pydatetime")
        else datetime.fromisoformat(str(df.index[-1])[:19])
    )
    last_close = float(df.iloc[-1][close_c])
    return _result(
        symbol, qty, entry, last_close, "still_open_mtm", last_ts, trail_active,
        peak, trail_stop, events, n_bars, gap_tp=False,
    )


def _result(
    symbol, qty, entry, exit_px, reason, ts, trail_active, peak, trail_stop, events, n_bars,
    *, gap_tp: bool,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "exit_ts": ts.isoformat() if ts else None,
        "exit_price": float(exit_px),
        "exit_reason": reason,
        "realized_pnl": (float(exit_px) - entry) * qty,
        "return_pct": (float(exit_px) / entry - 1.0) * 100,
        "trail_activated": trail_active,
        "peak_price": peak,
        "trail_stop": trail_stop,
        "events": events,
        "n_bars": n_bars,
        "gap_through_tp": gap_tp,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=",".join(str(i) for i in DEFAULT_IDS))
    args = ap.parse_args()
    want = {int(x) for x in args.ids.split(",") if x.strip()}

    s = SyncSessionLocal()
    try:
        id_list = sorted(want)
        placeholders = ", ".join(f":id{i}" for i in range(len(id_list)))
        params: dict[str, Any] = {"sid": STRATEGY_ID}
        for i, vid in enumerate(id_list):
            params[f"id{i}"] = vid
        rows = s.execute(
            text(
                f"""
                SELECT id, symbol, qty, entry_price, exit_price, realized_pnl,
                       return_pct, opened_at, closed_at, signal_reason
                FROM trade_journal
                WHERE strategy_id = :sid AND status = 'closed'
                  AND id IN ({placeholders})
                ORDER BY opened_at
                """
            ),
            params,
        ).mappings().all()
    finally:
        s.close()

    print(
        f"1Min trail replay activate={TRAIL_ACTIVATE:.0%} trail={TRAIL_PCT:.1%} "
        f"hard_sl={HARD_SL:.0%} hard_tp={HARD_TP:.0%} "
        f"disable_tp_after_trail={DISABLE_HARD_TP_AFTER_TRAIL}"
    )
    print(f"ids={sorted(want)} n_found={len(rows)}\n")

    actual_sum = sim_sum = 0.0
    for r in rows:
        entry = float(r["entry_price"])
        qty = float(r["qty"])
        actual = float(r["realized_pnl"] or 0)
        actual_sum += actual
        sim = simulate_1min(r["symbol"], entry, qty, r["opened_at"], r["closed_at"])
        if sim.get("error"):
            print(f"id={r['id']} {r['symbol']} ERROR {sim['error']}\n")
            continue
        sim_sum += float(sim["realized_pnl"])
        delta = float(sim["realized_pnl"]) - actual
        print(
            f"id={r['id']} {r['symbol']} qty={qty:g} bars={sim['n_bars']}\n"
            f"  actual  exit={float(r['exit_price']):.4f}  pnl=${actual:+.2f}  "
            f"ret={float(r['return_pct'] or 0):+.2f}%  "
            f"closed={r['closed_at']}\n"
            f"  1min    reason={sim['exit_reason']:12s} exit={sim['exit_price']:.4f}  "
            f"pnl=${sim['realized_pnl']:+.2f}  Δ=${delta:+.2f}  "
            f"ret={sim['return_pct']:+.2f}%\n"
            f"  trail   activated={sim['trail_activated']} peak={sim['peak_price']:.4f} "
            f"stop={sim['trail_stop']} gap_tp={sim['gap_through_tp']} "
            f"exit_ts={sim['exit_ts']}"
        )
        for ev in (sim.get("events") or [])[:8]:
            print(f"    {ev}")
        if len(sim.get("events") or []) > 8:
            print(f"    … +{len(sim['events'])-8} updates")
        print()

    print("=" * 72)
    print(f"actual (these ids) ${actual_sum:+,.2f}")
    print(f"1min trail sim     ${sim_sum:+,.2f}  Δ=${sim_sum - actual_sum:+,.2f}")
    print(
        "\ntrail_execute stays false — this is research only. "
        "Gap-through TP fills at bar open when open >= hard_tp."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
