# -*- coding: utf-8 -*-
"""PEAD reality gate: non-overlapping event portfolio.

Rules:
  - Signal = Nasdaq EPS surprise (train sign lock on T+20 abn)
  - Rank events within each calendar month; long top quintile only
  - Entry T+1, hold exactly 20 sessions; skip if already in a position for that symbol
  - Max concurrent names N; equal weight among active
  - Costs: 10bps round-trip; optional 25bps stress
  - Benchmark: SPY total return over same active days

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_reality
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.event_alpha.pead_book import HOLD, MAX_NAMES, select_accepted_events

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
EVENTS = ROOT / "data" / "cache" / "nasdaq_earnings_events.parquet"
OUT = ROOT / "data" / "research" / "event_alpha_pead_reality.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

COSTS = (0.001, 0.0025)


def period_stats(daily: pd.Series, spy: pd.Series) -> dict:
    d = daily.dropna()
    if len(d) < 40:
        return {"n": int(len(d))}
    ann = float(d.mean() * 252)
    vol = float(d.std() * np.sqrt(252))
    sharpe = ann / vol if vol > 0 else None
    eq = (1 + d).cumprod()
    dd = float((eq / eq.cummax() - 1.0).min())
    # vs SPY on same days
    s = spy.reindex(d.index).fillna(0.0)
    ex = d - s
    ex_ann = float(ex.mean() * 252)
    ex_vol = float(ex.std() * np.sqrt(252))
    ir = ex_ann / ex_vol if ex_vol > 0 else None
    return {
        "n_days": int(len(d)),
        "ann": ann,
        "vol": vol,
        "sharpe": sharpe,
        "maxdd": dd,
        "hit": float((d > 0).mean()),
        "ex_ann_vs_spy": ex_ann,
        "ir_vs_spy": ir,
        "cum": float(eq.iloc[-1] - 1.0),
    }


def run_bt(close: pd.DataFrame, events: pd.DataFrame, cost_rt: float) -> dict:
    spy = close["SPY"].pct_change() if "SPY" in close.columns else close.pct_change().mean(axis=1)
    px_ret = close.pct_change()

    acc, sign, tr_ic = select_accepted_events(close, events, hold=HOLD, max_names=MAX_NAMES)
    print(f"  cost={cost_rt:.4f} accepted={len(acc)} sign={sign:+.0f}")

    n = len(close.index)
    daily = pd.Series(0.0, index=close.index)
    n_active = pd.Series(0.0, index=close.index)

    occ = {i: [] for i in range(n)}
    entry_cost_i = set()
    exit_cost_i = set()
    for _, r in acc.iterrows():
        ei, xi, sym = int(r["entry_i"]), int(r["exit_i"]), r["symbol"]
        for i in range(ei, xi):
            occ[i].append(sym)
        entry_cost_i.add((ei, sym))
        exit_cost_i.add((xi - 1, sym))  # charge on last held day

    for i in range(n):
        syms = occ[i]
        if not syms:
            continue
        w = 1.0 / len(syms)
        ret = 0.0
        for s in syms:
            r = px_ret[s].iloc[i]
            ret += w * (float(r) if pd.notna(r) else 0.0)
            if (i, s) in entry_cost_i:
                ret -= w * (cost_rt / 2.0)
            if (i, s) in exit_cost_i:
                ret -= w * (cost_rt / 2.0)
        daily.iloc[i] = ret
        n_active.iloc[i] = len(syms)

    out = {
        "n_accepted_events": int(len(acc)),
        "avg_names": float(n_active[n_active > 0].mean()) if (n_active > 0).any() else 0.0,
        "sign": sign,
        "train_ic": tr_ic,
        "periods": {
            "train": period_stats(daily[(daily.index >= "2021-07-01") & (daily.index <= "2022-12-31") & (n_active > 0)], spy),
            "valid": period_stats(daily[(daily.index >= "2023-01-01") & (daily.index <= "2023-12-31") & (n_active > 0)], spy),
            "oos": period_stats(daily[(daily.index >= "2024-01-01") & (daily.index <= "2025-12-31") & (n_active > 0)], spy),
            "holdout": period_stats(daily[(daily.index >= "2026-01-01") & (n_active > 0)], spy),
            "recent_2024plus": period_stats(daily[(daily.index >= "2024-01-01") & (n_active > 0)], spy),
        },
    }
    return out


def main() -> None:
    close = pd.read_parquet(CACHE)
    events = pd.read_parquet(EVENTS)
    events["event_date"] = pd.to_datetime(events["event_date"])
    results = {"costs": {}}
    for c in COSTS:
        print(f"Running cost_rt={c}")
        results["costs"][str(c)] = run_bt(close, events, c)
        oos = results["costs"][str(c)]["periods"]["oos"]
        print(f"  OOS: {oos}")

    base = results["costs"]["0.001"]["periods"]["oos"]
    stress = results["costs"]["0.0025"]["periods"]["oos"]
    results["reality_gate"] = {
        "pass": bool(
            (base.get("sharpe") or 0) > 0.5
            and (base.get("ex_ann_vs_spy") or 0) > 0.0
            and (stress.get("sharpe") or 0) > 0.0
            and (base.get("maxdd") or -1) > -0.25
        ),
        "checks": {
            "oos_sharpe_gt_0.5": (base.get("sharpe") or 0) > 0.5,
            "oos_excess_vs_spy_gt_0": (base.get("ex_ann_vs_spy") or 0) > 0.0,
            "stress_25bps_sharpe_gt_0": (stress.get("sharpe") or 0) > 0.0,
            "oos_maxdd_gt_-25pct": (base.get("maxdd") or -1) > -0.25,
        },
        "note": "Non-overlapping per-symbol, max 40 names, monthly top-quintile surprise, hold 20d",
    }
    results["recommendation"] = (
        "pead_reality_pass - keep as event candidate; still not LIVE without attribution"
        if results["reality_gate"]["pass"]
        else "pead_reality_fail - prior overlapping BT was optimistic"
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_B", {})["PEAD_reality"] = {
        "status": "PASS" if results["reality_gate"]["pass"] else "FAIL",
        "oos_10bps": base,
        "oos_25bps": stress,
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["updated_at"] = "2026-07-31"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
