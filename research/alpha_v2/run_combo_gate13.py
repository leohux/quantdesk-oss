# -*- coding: utf-8 -*-
"""Gate13: portfolio reality for Gate12-passing combo.

Combo (equal CS-rank):
  vol_adj_rev_5 (+), near_high_252 industry-neutral (+),
  earnings_yield (+), neg_leverage (+)

Rules:
  - Score on day t, enter at t+1, weekly Friday rebalance
  - Long top 10% eligible; max weight 5% / name
  - Costs: 10bps / 20bps / 40bps on turnover
  - Pass: OOS Sharpe>0.5, MaxDD>-30%, 2024&2025 both >0, stress 20bps Sharpe>0

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_combo_gate13
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.features.extended import build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.run_gate12_push import build_quality_value

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_combo_gate13.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def neutralize_wide(fr: pd.DataFrame, sector_map: pd.Series) -> pd.DataFrame:
    long = fr.stack(future_stack=True).rename("x").reset_index()
    long.columns = ["date", "symbol", "x"]
    long["x"] = industry_neutralize_score(long, "x", sector_map)
    out = long.pivot(index="date", columns="symbol", values="x")
    return out.reindex(index=fr.index, columns=fr.columns)


def build_score(close: pd.DataFrame, elig: pd.DataFrame) -> pd.DataFrame:
    fund = pd.read_parquet(FUND)
    ext = build_extended_features(close)
    qv = build_quality_value(close, fund)
    sm = load_sector_map()

    members = [
        (ext["vol_adj_rev_5"].reindex_like(close), False, 1.0),
        (ext["near_high_252"].reindex_like(close), True, 1.0),
        (qv["earnings_yield"].reindex_like(close), False, 1.0),
        (qv["neg_leverage"].reindex_like(close), False, 1.0),
    ]
    ranks = []
    for fr, neut, sign in members:
        x = fr.astype(float)
        if neut:
            x = neutralize_wide(x, sm)
        ranks.append((x * sign).rank(axis=1, pct=True))
    score = sum(ranks) / len(ranks)
    return score.where(elig.reindex_like(score).fillna(False))


def target_weights(score_row: pd.Series, top_frac: float, max_w: float) -> pd.Series:
    s = score_row.dropna()
    w = pd.Series(0.0, index=score_row.index)
    if len(s) < 50:
        return w
    n = max(1, int(len(s) * top_frac))
    picks = s.nlargest(n).index
    ew = min(1.0 / len(picks), max_w)
    w.loc[picks] = ew
    return w


def portfolio_backtest(
    close: pd.DataFrame,
    score: pd.DataFrame,
    *,
    top_frac: float = 0.10,
    max_w: float = 0.05,
    cost_rt: float = 0.001,
) -> dict:
    rets = close.pct_change()
    dates = close.index
    reb_days = [d for d in dates if int(d.dayofweek) == 4]
    if "SPY" in close.columns:
        spy = close["SPY"].pct_change()
    else:
        spy = rets.mean(axis=1)

    weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
    cost_series = pd.Series(0.0, index=dates)
    prev_w = pd.Series(0.0, index=close.columns)
    turnovers: list[float] = []

    for i, d in enumerate(reb_days):
        if d not in score.index:
            continue
        w = target_weights(score.loc[d], top_frac, max_w)
        if float(w.sum()) <= 0:
            continue
        to = float((w - prev_w).abs().sum())
        turnovers.append(to)
        start = int(dates.searchsorted(d)) + 1
        end = (
            int(dates.searchsorted(reb_days[i + 1])) + 1
            if i + 1 < len(reb_days)
            else len(dates)
        )
        end = min(end, len(dates))
        if start >= len(dates):
            continue
        for j in range(start, end):
            weights.iloc[j] = w.values
        cost_series.iloc[start] += to * (cost_rt / 2.0)
        prev_w = w

    port = (weights * rets).sum(axis=1) - cost_series

    def pack(mask) -> dict:
        r = port.loc[mask].dropna()
        if len(r) < 40:
            return {"n": int(len(r))}
        ann = float(r.mean() * 252)
        vol = float(r.std() * np.sqrt(252))
        sharpe = ann / vol if vol > 0 else None
        eq = (1 + r).cumprod()
        dd = float((eq / eq.cummax() - 1).min())
        years = {str(y): float((1 + g).prod() - 1) for y, g in r.groupby(r.index.year)}
        ex = r - spy.reindex(r.index).fillna(0)
        return {
            "n": int(len(r)),
            "ann": ann,
            "vol": vol,
            "sharpe": sharpe,
            "maxdd": dd,
            "hit": float((r > 0).mean()),
            "years": years,
            "ex_ann_vs_spy": float(ex.mean() * 252),
            "avg_turnover_reb": float(np.mean(turnovers)) if turnovers else None,
        }

    return {
        "train": pack((port.index >= "2021-07-01") & (port.index <= "2022-12-31")),
        "valid": pack((port.index >= "2023-01-01") & (port.index <= "2023-12-31")),
        "oos": pack((port.index >= "2024-01-01") & (port.index <= "2025-12-31")),
        "holdout": pack(port.index >= "2026-01-01"),
        "recent_2024plus": pack(port.index >= "2024-01-01"),
    }


def main() -> None:
    close = pd.read_parquet(CACHE)
    elig = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in elig.columns]
    close = close[cols]
    elig = elig[cols].reindex(close.index).fillna(False)

    print("Building score...", flush=True)
    score = build_score(close, elig)
    print("Score coverage", float(score.notna().mean().mean()), flush=True)

    results = {"costs": {}}
    for c in (0.001, 0.002, 0.004):
        print(f"BT cost_rt={c}", flush=True)
        results["costs"][str(c)] = portfolio_backtest(close, score, cost_rt=c)
        print("  OOS", results["costs"][str(c)]["oos"], flush=True)

    oos = results["costs"]["0.001"]["oos"]
    oos20 = results["costs"]["0.002"]["oos"]
    y = oos.get("years") or {}
    results["gate13"] = {
        "pass": bool(
            (oos.get("sharpe") or 0) > 0.5
            and (oos.get("maxdd") or -1) > -0.30
            and (y.get("2024") or 0) > 0
            and (y.get("2025") or 0) > 0
            and (oos20.get("sharpe") or 0) > 0
        ),
        "checks": {
            "oos_sharpe_gt_0.5": (oos.get("sharpe") or 0) > 0.5,
            "oos_maxdd_gt_-30pct": (oos.get("maxdd") or -1) > -0.30,
            "y2024_gt_0": (y.get("2024") or 0) > 0,
            "y2025_gt_0": (y.get("2025") or 0) > 0,
            "stress_20bps_sharpe_gt_0": (oos20.get("sharpe") or 0) > 0,
        },
    }
    results["recommendation"] = (
        "gate13_pass - run attribution Gate14"
        if results["gate13"]["pass"]
        else "gate13_fail - predictive RankIC did not translate to portfolio after costs"
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["gate13"], flush=True)
    print(results["recommendation"], flush=True)
    print(f"Saved {OUT}", flush=True)

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_A", {})["gate13_combo"] = {
        "status": "PASS" if results["gate13"]["pass"] else "FAIL",
        "oos": oos,
        "gate13": results["gate13"],
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["hard_gates"] = st.get("hard_gates", {})
    st["hard_gates"]["gate12_rankic"] = "PASS"
    st["hard_gates"]["gate13_reality"] = "PASS" if results["gate13"]["pass"] else "FAIL"
    st["live"] = "LOCKED"
    st.setdefault("system", {})
    st["system"]["alpha"] = (
        "CANDIDATE_PENDING_ATTRIBUTION" if results["gate13"]["pass"] else "NOT_FOUND"
    )
    st["system"]["live"] = "LOCKED"
    st["updated_at"] = "2026-07-31"
    st["phase"] = "phase12.3_gate12_gate13"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
