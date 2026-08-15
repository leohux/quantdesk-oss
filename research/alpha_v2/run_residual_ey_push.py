# -*- coding: utf-8 -*-
"""Push the residual-EY Gate12a candidate through cost + SPY-timing residual.

Candidate: industry-neutral near_high_252 residualized vs earnings_yield.
Gate12-A on 2024+ was a pass only because 2026 holdout inflated RankIC;
this still runs the book so we can kill or keep it with Gate13/14 numbers.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_residual_ey_push
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
from research.alpha_v2.labels.forward_return import align_xy, forward_return
from research.alpha_v2.run_combo_gate14 import daily_port_returns, ols_alpha, sharpe
from research.alpha_v2.run_gate12_push import build_quality_value
from research.alpha_v2.run_residual_ey_screen import cs_residual

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_residual_ey_push.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"
OOS = ("2024-01-01", "2025-12-31")


def spy_timing(close: pd.DataFrame) -> pd.Series:
    full = pd.read_parquet(CACHE)
    spy = full["SPY"] if "SPY" in full.columns else close.mean(axis=1)
    r = spy.pct_change()
    ma = spy.rolling(200).mean()
    on = (spy > ma).astype(float).reindex(r.index).fillna(0.0)
    return r * on


def main() -> None:
    close = pd.read_parquet(CACHE)
    elig = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in elig.columns]
    close = close[cols]
    elig = elig[cols].reindex(close.index).fillna(False)
    fund = pd.read_parquet(FUND)
    px = build_extended_features(close)
    qv = build_quality_value(close, fund)
    feats = {
        "near_high_252": px["near_high_252"],
        "earnings_yield": qv["earnings_yield"],
    }
    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, elig)
    panel["date"] = pd.to_datetime(panel["date"])
    sector_map = load_sector_map()

    tmp = panel[["date", "symbol"]].copy()
    tmp["near_high_252"] = panel["near_high_252"].astype(float)
    y = industry_neutralize_score(tmp, "near_high_252", sector_map)
    tmp2 = panel[["date", "symbol"]].copy()
    tmp2["earnings_yield"] = panel["earnings_yield"].astype(float)
    x = industry_neutralize_score(tmp2, "earnings_yield", sector_map)
    work = panel[["date", "symbol"]].copy()
    work["_y"] = y
    work["_x"] = x
    work["score"] = cs_residual(work, "_y", "_x")
    wide = work.pivot(index="date", columns="symbol", values="score")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.reindex(close.index).where(elig)

    port = daily_port_returns(close, wide, 0.001)
    timing = spy_timing(close).reindex(port.index)
    oos = (port.index >= OOS[0]) & (port.index <= OOS[1])
    y = port.loc[oos]
    x = timing.loc[oos]
    fit = ols_alpha(y, x)
    resid = (y - fit["beta"] * x - fit["alpha_daily"]).dropna() if fit["beta"] is not None else y
    # ols_alpha alpha is intercept; residual series for sharpe:
    d = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
    if len(d) >= 40:
        X = np.column_stack([np.ones(len(d)), d["x"].to_numpy()])
        bh, *_ = np.linalg.lstsq(X, d["y"].to_numpy(), rcond=None)
        resid = pd.Series(d["y"].to_numpy() - X @ bh, index=d.index)
    out = {
        "factor": "near_high_252__indneut_resid_ey",
        "cost_rt": 0.001,
        "oos": {
            "sharpe": sharpe(y),
            "ann": float(y.mean() * 252) if len(y) else None,
            "n": int(y.dropna().shape[0]),
        },
        "spy_timing_sharpe": sharpe(x),
        "vs_spy_timing": fit,
        "resid_sharpe": sharpe(resid),
        "resid_ann": float(resid.mean() * 252) if len(resid) else None,
        "note": (
            "Gate12a 2024+ RankIC was 0.021 only with 2026 holdout; "
            "valid 2023 RankIC=-0.026, OOS 2024-25 RankIC=0.013."
        ),
    }
    rs = out["resid_sharpe"]
    t = fit.get("t_alpha")
    out["verdict"] = (
        "KEEP_DIGGING"
        if (rs is not None and rs == rs and rs >= 0.5 and t is not None and abs(t) >= 1.5)
        else "ARCHIVE_DEAD"
    )
    print(json.dumps({k: out[k] for k in ("oos", "spy_timing_sharpe", "vs_spy_timing", "resid_sharpe", "resid_ann", "verdict")}, indent=2, default=float))
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_A", {}).setdefault("residual_ey_screen", {})["push"] = {
        "verdict": out["verdict"],
        "resid_sharpe": out["resid_sharpe"],
        "t_alpha": t,
        "artifact": str(OUT).replace("\\", "/"),
    }
    st["live"] = "LOCKED"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
