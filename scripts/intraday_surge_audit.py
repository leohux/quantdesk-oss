# -*- coding: utf-8 -*-
"""Audit paper trades for 美股科技股突破延续 (strategy-046bfa).

Former display name: 美股小盘早盘异动 (kept as strategies.json alias).

Reads Postgres trade_journal (+ open sleeve MTM via broker marks).

  python /app/scripts/intraday_surge_audit.py

Checks:
  - closed realized / open attributed / full-cycle total
  - hold-time buckets (calendar days from opened_at→closed_at)
  - single-name concentration (gross pos / gross neg), WARN 40% / HARD 50%
  - exit type inferred from signal_reason / return vs SL-TP bands
  - era split: early small/meme names vs current large-tech pool
  - false-breakout note: live filter arm date (params); pre-arm lots flagged

Does NOT claim statistical significance — sample is usually tiny.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.db import SyncSessionLocal
from execution.alpaca_client import AlpacaPaperClient

STRATEGY_ID = os.environ.get("INTRADAY_STRATEGY_ID", "strategy-046bfa")
CONC_WARN = float(os.environ.get("SURGE_CONC_WARN", "0.40"))
CONC_HARD = float(os.environ.get("SURGE_CONC_HARD", "0.50"))

# Names that defined the early "small/meme" identity (now mostly removed).
EARLY_NAMES = {"RIVN", "SOFI", "AMC", "UPST", "HOOD", "SMCI"}

# Approx when false-breakout filters were armed with real thresholds
# (min_vol_ratio=1.2, breakout_lookback=10, max_prior_ret=0.08).
FB_ARMED_UTC = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

# Live bracket defaults (for exit-type inference).
DEFAULT_SL = -0.08
DEFAULT_TP = 0.15


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _hold_days(opened: datetime | None, closed: datetime | None, stored: Any) -> float | None:
    if stored is not None:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    o, c = _as_utc(opened), _as_utc(closed)
    if o and c and c >= o:
        return (c - o).total_seconds() / 86400.0
    return None


def _hold_bucket(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 1:
        return "<1d"
    if days < 2:
        return "1-2d"
    if days < 4:
        return "2-4d"
    if days < 7:
        return "4-7d"
    return ">=7d"


def _infer_exit(row: dict[str, Any], sl: float, tp: float) -> str:
    reason = str(row.get("signal_reason") or "").lower()
    if "backfill" in reason and "stop" in reason:
        return "stop_loss"
    if "backfill" in reason and "limit" in reason:
        return "take_profit"
    if "broker bracket" in reason and "stop" in reason:
        return "stop_loss"
    ret = row.get("return_pct")
    if ret is None and row.get("entry_price") and row.get("exit_price"):
        ep, xp = float(row["entry_price"]), float(row["exit_price"])
        if ep > 0:
            ret = (xp / ep - 1.0) * 100.0
    if ret is not None:
        r = float(ret) / 100.0
        # loose bands — paper fills slip
        if r <= sl * 0.7:
            return "stop_loss"
        if r >= tp * 0.7:
            return "take_profit"
        if r < 0:
            return "stop_or_forced_loss"
        return "tp_or_manual_gain"
    return "unknown"


def _summ_dollar(xs: list[float]) -> str:
    if not xs:
        return "n=  0"
    wins = sum(1 for x in xs if x > 0)
    return (
        f"n={len(xs):3d}  win={wins/len(xs)*100:5.1f}%  "
        f"avg=${sum(xs)/len(xs):+8.2f}  sum=${sum(xs):+10.2f}"
    )


def load_journal() -> list[dict[str, Any]]:
    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, trade_id, symbol, qty, entry_price, exit_price,
                       realized_pnl, return_pct, status, holding_days,
                       opened_at, closed_at, signal_reason
                FROM trade_journal
                WHERE strategy_id = :sid
                ORDER BY COALESCE(opened_at, created_at)
                """
            ),
            {"sid": STRATEGY_ID},
        ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        s.close()


def open_mtm() -> tuple[float, list[tuple[str, float, float, float]]]:
    s = SyncSessionLocal()
    try:
        lots = s.execute(
            text(
                """
                SELECT symbol, qty, avg_price FROM strategy_positions
                WHERE strategy_id = :sid AND qty <> 0 ORDER BY symbol
                """
            ),
            {"sid": STRATEGY_ID},
        ).fetchall()
    finally:
        s.close()
    client = AlpacaPaperClient()
    pos = {p["symbol"]: p for p in client.positions()}
    out = []
    total = 0.0
    for sym, qty, avg in lots:
        qty, avg = float(qty), float(avg or 0)
        bp = pos.get(sym)
        if not bp:
            out.append((sym, qty, avg, float("nan")))
            continue
        cur = float(bp["current_price"])
        u = (cur - avg) * qty
        total += u
        out.append((sym, qty, avg, u))
    return total, out


def concentration(closed: list[dict[str, Any]]) -> None:
    print("\n== single-name concentration (discipline, two-sided) ==")
    by_sym: dict[str, float] = defaultdict(float)
    for r in closed:
        by_sym[str(r["symbol"])] += float(r["realized_pnl"])

    pos_all = sum(max(0.0, v) for v in by_sym.values())
    neg_all = sum(min(0.0, v) for v in by_sym.values())
    neg_mag = abs(neg_all)

    print(f"  closed_symbols={len(by_sym)}  gross_pos=${pos_all:+,.2f}  gross_neg=${neg_all:+,.2f}")
    ranked = sorted(by_sym.items(), key=lambda kv: -abs(kv[1]))
    for sym, pnl in ranked:
        pos_share = (max(0.0, pnl) / pos_all) if pos_all > 1e-9 and pnl > 0 else 0.0
        neg_share = (abs(min(0.0, pnl)) / neg_mag) if neg_mag > 1e-9 and pnl < 0 else 0.0
        flag = ""
        hot = max(pos_share, neg_share)
        if hot >= CONC_HARD:
            flag = "  HARD FLAG"
        elif hot >= CONC_WARN:
            flag = "  WARN"
        print(
            f"  {sym:6s}  pnl=${pnl:+10.2f}  "
            f"pos_share={pos_share*100:5.1f}%  neg_share={neg_share*100:5.1f}%{flag}"
        )

    if not by_sym:
        print("  (no closed trades)")
        return

    top_pos_sym, top_pos = max(by_sym.items(), key=lambda kv: max(0.0, kv[1]))
    top_neg_sym, top_neg = min(by_sym.items(), key=lambda kv: kv[1])
    pos_share = (max(0.0, top_pos) / pos_all) if pos_all > 1e-9 else 0.0
    neg_share = (abs(min(0.0, top_neg)) / neg_mag) if neg_mag > 1e-9 else 0.0
    hot = max(pos_share, neg_share)
    side = "pos" if pos_share >= neg_share else "neg"
    who = top_pos_sym if side == "pos" else top_neg_sym
    if hot >= CONC_HARD:
        print(
            f"  HARD FLAG: top {side} name {who} contributes {hot*100:.1f}% "
            f">= {CONC_HARD*100:.0f}% of gross {'gains' if side=='pos' else 'losses'}"
        )
    elif hot >= CONC_WARN:
        print(
            f"  WARN: top {side} name {who} contributes {hot*100:.1f}% "
            f">= {CONC_WARN*100:.0f}% — watch concentration"
        )
    else:
        print(f"  OK: max single-name contrib {hot*100:.1f}% < {CONC_WARN*100:.0f}% warn")


def main() -> int:
    from config.store import get_strategy

    st = get_strategy(STRATEGY_ID) or {}
    params = st.get("params") or {}
    sl = float(params.get("stop_loss", DEFAULT_SL))
    tp = float(params.get("take_profit", DEFAULT_TP))
    fb_on = bool(params.get("filter_false_breakout", True))

    rows = load_journal()
    closed = [
        r
        for r in rows
        if r.get("status") == "closed" and r.get("realized_pnl") is not None
    ]
    open_rows = [r for r in rows if r.get("status") == "open"]
    superseded = [r for r in rows if r.get("status") == "superseded"]
    stale = [r for r in rows if r.get("status") == "stale"]

    print(f"strategy={st.get('name')} id={STRATEGY_ID}")
    print(
        f"enabled={st.get('enabled')}  stocknum={params.get('stocknum')}  "
        f"surge=[{params.get('buy_surge')},{params.get('buy_cap')})  "
        f"SL/TP={sl}/{tp}  fb_filter={fb_on}"
    )
    print(
        f"journal: closed={len(closed)} open={len(open_rows)} "
        f"superseded={len(superseded)} stale={len(stale)}"
    )
    if stale:
        print(f"  WARNING: {len(stale)} stale rows still lack PnL — re-run backfill")

    realized = sum(float(r["realized_pnl"]) for r in closed)
    wins = sum(1 for r in closed if float(r["realized_pnl"]) > 0)
    losses = sum(1 for r in closed if float(r["realized_pnl"]) < 0)
    attr, lots = open_mtm()

    print("\n== full-cycle PnL ==")
    print(
        f"  closed realized=${realized:+,.2f}  "
        f"wins={wins} losses={losses} flat={len(closed)-wins-losses}"
    )
    print(f"  open attributed upnl=${attr:+,.2f}")
    for sym, qty, avg, u in lots:
        print(f"    {sym:6s} {qty:g}@{avg:.4f}  upnl=${u:+,.2f}")
    print(f"  TOTAL ≈ ${realized + attr:+,.2f}")
    print(f"  note: n_closed={len(closed)} is too small for edge claims")

    # hold buckets
    print("\n== hold-time buckets (calendar days) ==")
    by_hold: dict[str, list[float]] = defaultdict(list)
    holds: list[float] = []
    for r in closed:
        d = _hold_days(r.get("opened_at"), r.get("closed_at"), r.get("holding_days"))
        by_hold[_hold_bucket(d)].append(float(r["realized_pnl"]))
        if d is not None:
            holds.append(d)
    for b in ["<1d", "1-2d", "2-4d", "4-7d", ">=7d", "unknown"]:
        xs = by_hold.get(b) or []
        if xs:
            print(f"  {b:8s}  {_summ_dollar(xs)}")
        else:
            print(f"  {b:8s}  (no samples)")
    if holds:
        holds_s = sorted(holds)
        med = holds_s[len(holds_s) // 2]
        print(
            f"  distribution: min={min(holds):.1f}d  median={med:.1f}d  "
            f"max={max(holds):.1f}d  mean={sum(holds)/len(holds):.1f}d"
        )
        multi = sum(1 for h in holds if h >= 2)
        print(
            f"  >=2d holds: {multi}/{len(holds)} "
            f"({multi/len(holds)*100:.0f}%) — vs 'intraday' naming"
        )

    # exit type
    print("\n== exit type (inferred) ==")
    by_exit: dict[str, list[float]] = defaultdict(list)
    for r in closed:
        by_exit[_infer_exit(r, sl, tp)].append(float(r["realized_pnl"]))
    for k, xs in sorted(by_exit.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:22s}  {_summ_dollar(xs)}")

    # era / identity drift
    print("\n== identity / era split ==")
    early = [r for r in closed if str(r["symbol"]).upper() in EARLY_NAMES]
    late = [r for r in closed if str(r["symbol"]).upper() not in EARLY_NAMES]
    print(f"  early_pool (RIVN/SOFI/AMC/…)  {_summ_dollar([float(r['realized_pnl']) for r in early])}")
    print(f"  large_tech / other           {_summ_dollar([float(r['realized_pnl']) for r in late])}")

    pre_fb = []
    post_fb = []
    for r in closed:
        o = _as_utc(r.get("opened_at"))
        if o and o < FB_ARMED_UTC:
            pre_fb.append(float(r["realized_pnl"]))
        else:
            post_fb.append(float(r["realized_pnl"]))
    print(f"  opened before FB arm (~{FB_ARMED_UTC.date()})  {_summ_dollar(pre_fb)}")
    print(f"  opened after FB arm                {_summ_dollar(post_fb)}")
    print(
        "  note: FB hit-rate A/B needs skip logs with reasons; "
        "runner log mining not wired here yet"
    )

    concentration(closed)

    print("\n== sample-size gate ==")
    if len(closed) < 20:
        print(
            f"  HARD: n_closed={len(closed)} < 20 — "
            f"do not promote on win-rate / expectancy yet"
        )
    elif len(closed) < 50:
        print(f"  WARN: n_closed={len(closed)} < 50 — treat metrics as provisional")
    else:
        print(f"  OK: n_closed={len(closed)} — enough for a first cut (still not research IS/OOS)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
