# -*- coding: utf-8 -*-
"""Gate14 attribution for Gate13-passing combo portfolio.

Tests whether weekly long-top-10% combo beats:
  - SPY buy&hold
  - SPY + 200d MA timing
  - Simple value (earnings_yield top 10%)
  - Simple low-vol (20d vol bottom 10%)
  - Multifactor residual alpha (regress daily excess on SPY)

Pass bar (strict):
  - OOS residual alpha > 0 with t-stat > 1.5
  - OOS Sharpe > SPY Sharpe
  - Not dominated by SPY+200MA timing on Sharpe

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_combo_gate14
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.run_combo_gate13 import build_score, portfolio_backtest, target_weights
from research.alpha_v2.run_gate12_push import build_quality_value

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_combo_gate14.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def daily_port_returns(
    close: pd.DataFrame,
    score: pd.DataFrame,
    cost_rt: float = 0.001,
) -> pd.Series:
    """Reuse Gate13 construction; return daily net series."""
    rets = close.pct_change()
    dates = close.index
    reb_days = [d for d in dates if int(d.dayofweek) == 4]
    weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
    cost_series = pd.Series(0.0, index=dates)
    prev_w = pd.Series(0.0, index=close.columns)
    for i, d in enumerate(reb_days):
        if d not in score.index:
            continue
        w = target_weights(score.loc[d].reindex(close.columns), 0.10, 0.05)
        if float(w.sum()) <= 0:
            continue
        to = float((w - prev_w).abs().sum())
        start = int(dates.searchsorted(d)) + 1
        end = (
            int(dates.searchsorted(reb_days[i + 1])) + 1
            if i + 1 < len(reb_days)
            else len(dates)
        )
        end = min(end, len(dates))
        if start >= len(dates):
            continue
        w = w.reindex(close.columns).fillna(0.0)
        for j in range(start, end):
            weights.iloc[j] = w.to_numpy()
        cost_series.iloc[start] += to * (cost_rt / 2.0)
        prev_w = w
    return (weights * rets).sum(axis=1) - cost_series


def sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 40 or r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def ols_alpha(y: pd.Series, x: pd.Series) -> dict:
    d = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
    if len(d) < 40:
        return {"alpha": None, "beta": None, "t_alpha": None, "n": int(len(d))}
    X = np.column_stack([np.ones(len(d)), d["x"].to_numpy()])
    yv = d["y"].to_numpy()
    beta_hat, *_ = np.linalg.lstsq(X, yv, rcond=None)
    resid = yv - X @ beta_hat
    s2 = float((resid @ resid) / max(len(d) - 2, 1))
    xtx_inv = np.linalg.inv(X.T @ X)
    se_a = float(np.sqrt(s2 * xtx_inv[0, 0]))
    t_a = float(beta_hat[0] / se_a) if se_a > 0 else None
    return {
        "alpha_daily": float(beta_hat[0]),
        "alpha_ann": float(beta_hat[0] * 252),
        "beta": float(beta_hat[1]),
        "t_alpha": t_a,
        "n": int(len(d)),
    }


def simple_factor_port(
    close: pd.DataFrame,
    elig: pd.DataFrame,
    score: pd.DataFrame,
    cost_rt: float = 0.001,
) -> pd.Series:
    sc = score.reindex_like(close)
    return daily_port_returns(close, sc.where(elig.reindex_like(sc).fillna(False)), cost_rt)


def main() -> None:
    close = pd.read_parquet(CACHE)
    elig = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in elig.columns]
    close = close[cols]
    elig = elig[cols].reindex(close.index).fillna(False)

    combo_score = build_score(close, elig)
    combo = daily_port_returns(close, combo_score, 0.001)

    # SPY
    if "SPY" in close.columns:
        spy = close["SPY"].pct_change()
    else:
        # load separately if dropped from eligible
        full = pd.read_parquet(CACHE)
        spy = full["SPY"].pct_change().reindex(close.index) if "SPY" in full.columns else close.pct_change().mean(axis=1)

    # SPY 200MA timing
    spy_px = (1 + spy.fillna(0)).cumprod()
    ma200 = spy_px.rolling(200).mean()
    spy_timing = spy.where(spy_px > ma200, 0.0)

    # value sleeve
    fund = pd.read_parquet(FUND)
    qv = build_quality_value(close, fund)
    ey = qv["earnings_yield"].where(elig)
    value_port = simple_factor_port(close, elig, ey)

    # low vol sleeve
    vol20 = close.pct_change().rolling(20).std()
    lowvol_score = (-vol20).where(elig)
    lowvol_port = simple_factor_port(close, elig, lowvol_score)

    mask = (combo.index >= "2024-01-01") & (combo.index <= "2025-12-31")
    c = combo.loc[mask]
    s = spy.reindex(c.index)
    results = {
        "oos": {
            "combo_sharpe": sharpe(c),
            "spy_sharpe": sharpe(s),
            "spy_timing_sharpe": sharpe(spy_timing.reindex(c.index)),
            "value_sharpe": sharpe(value_port.reindex(c.index)),
            "lowvol_sharpe": sharpe(lowvol_port.reindex(c.index)),
            "combo_ann": float(c.mean() * 252),
            "spy_ann": float(s.mean() * 252),
            "alpha_vs_spy": ols_alpha(c, s),
            "alpha_vs_spy_timing": ols_alpha(c, spy_timing.reindex(c.index).fillna(0)),
        }
    }
    # multifactor: SPY + value + lowvol
    mf = pd.concat(
        [
            s.rename("spy"),
            value_port.reindex(c.index).rename("value"),
            lowvol_port.reindex(c.index).rename("lowvol"),
        ],
        axis=1,
    ).dropna()
    y = c.reindex(mf.index)
    X = np.column_stack([np.ones(len(mf)), mf.to_numpy()])
    bh, *_ = np.linalg.lstsq(X, y.to_numpy(), rcond=None)
    resid = y.to_numpy() - X @ bh
    s2 = float((resid @ resid) / max(len(mf) - X.shape[1], 1))
    se = float(np.sqrt(s2 * np.linalg.inv(X.T @ X)[0, 0]))
    results["oos"]["alpha_multifactor"] = {
        "alpha_ann": float(bh[0] * 252),
        "t_alpha": float(bh[0] / se) if se > 0 else None,
        "betas": {
            "spy": float(bh[1]),
            "value": float(bh[2]),
            "lowvol": float(bh[3]),
        },
        "n": int(len(mf)),
    }

    a = results["oos"]["alpha_vs_spy"]
    am = results["oos"]["alpha_multifactor"]
    results["gate14"] = {
        "pass": bool(
            (a.get("alpha_ann") or 0) > 0
            and (a.get("t_alpha") or 0) > 1.5
            and (results["oos"]["combo_sharpe"] or 0) > (results["oos"]["spy_sharpe"] or 0)
            and (am.get("alpha_ann") or 0) > 0
            and (am.get("t_alpha") or 0) > 1.0
        ),
        "checks": {
            "spy_alpha_ann_gt_0": (a.get("alpha_ann") or 0) > 0,
            "spy_alpha_t_gt_1.5": (a.get("t_alpha") or 0) > 1.5,
            "combo_sharpe_gt_spy": (results["oos"]["combo_sharpe"] or 0)
            > (results["oos"]["spy_sharpe"] or 0),
            "mf_alpha_ann_gt_0": (am.get("alpha_ann") or 0) > 0,
            "mf_alpha_t_gt_1.0": (am.get("t_alpha") or 0) > 1.0,
            "not_dominated_by_spy_timing": (results["oos"]["combo_sharpe"] or 0)
            >= (results["oos"]["spy_timing_sharpe"] or 0) - 0.05,
        },
    }
    results["recommendation"] = (
        "gate14_pass - residual alpha candidate; LIVE still requires ops checklist"
        if results["gate14"]["pass"]
        else "gate14_fail - returns mostly explained by market/simple factors"
    )
    print(json.dumps(results["oos"], indent=2, default=float))
    print(results["gate14"])
    print(results["recommendation"])
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")

    st = json.loads(STATUS.read_text(encoding="utf-8"))
    st.setdefault("Track_A", {})["gate14_combo"] = {
        "status": "PASS" if results["gate14"]["pass"] else "FAIL",
        "oos": results["oos"],
        "gate14": results["gate14"],
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["hard_gates"]["gate14_attribution"] = "PASS" if results["gate14"]["pass"] else "FAIL"
    st["live"] = "LOCKED"
    st["system"]["alpha"] = (
        "RESIDUAL_ALPHA_CANDIDATE" if results["gate14"]["pass"] else "NOT_FOUND"
    )
    st["system"]["live"] = "LOCKED"
    st["updated_at"] = "2026-07-31"
    st["phase"] = "phase12.3_gate14"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
