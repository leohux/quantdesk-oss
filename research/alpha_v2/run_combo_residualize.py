# -*- coding: utf-8 -*-
"""CS combo residualization vs SPY+200MA timing and earnings_yield sleeve.

Gate14 already showed combo is dominated by those two. This script asks the
archive question directly:

  R_combo = a + b1 * R_spy_timing + b2 * R_earnings_yield + e

If residual Sharpe (of e) is near zero / insignificant, archive (death).
If residual still has economic Sharpe, KEEP_DIGGING.

Death bar (OOS 2024-2025, 10bps book):
  - residual Sharpe < 0.3  OR  |t_alpha| < 1.0  OR  residual ann ~ 0
  => ARCHIVE_DEAD
  - residual Sharpe >= 0.5 and t_alpha >= 1.5
  => KEEP_DIGGING
  - else MARGINAL_ARCHIVE (lean archive)

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_combo_residualize
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.run_combo_gate13 import build_score
from research.alpha_v2.run_combo_gate14 import daily_port_returns, ols_alpha, sharpe, simple_factor_port
from research.alpha_v2.run_gate12_push import build_quality_value

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_combo_residualize.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"
NOTE = ROOT / "data" / "research" / "PHASE12_5_CS_RESIDUALIZE.md"

COST_RT = 0.001
OOS_START, OOS_END = "2024-01-01", "2025-12-31"


def multifactor_ols(y: pd.Series, factors: pd.DataFrame) -> dict:
    d = pd.concat([y.rename("y"), factors], axis=1).dropna()
    if len(d) < 60:
        return {"n": int(len(d)), "alpha_ann": None, "t_alpha": None, "betas": {}, "resid_sharpe": None}
    cols = list(factors.columns)
    X = np.column_stack([np.ones(len(d)), d[cols].to_numpy()])
    yv = d["y"].to_numpy()
    bh, *_ = np.linalg.lstsq(X, yv, rcond=None)
    resid = yv - X @ bh
    dof = max(len(d) - X.shape[1], 1)
    s2 = float((resid @ resid) / dof)
    xtx_inv = np.linalg.inv(X.T @ X)
    se_a = float(np.sqrt(s2 * xtx_inv[0, 0]))
    t_a = float(bh[0] / se_a) if se_a > 0 else None
    resid_s = pd.Series(resid, index=d.index)
    return {
        "n": int(len(d)),
        "alpha_daily": float(bh[0]),
        "alpha_ann": float(bh[0] * 252),
        "t_alpha": t_a,
        "betas": {c: float(bh[i + 1]) for i, c in enumerate(cols)},
        "r2": float(1.0 - (resid @ resid) / max(((yv - yv.mean()) @ (yv - yv.mean())), 1e-18)),
        "resid_sharpe": sharpe(resid_s),
        "resid_ann": float(resid_s.mean() * 252),
        "resid_vol": float(resid_s.std() * np.sqrt(252)),
        "factor_sharpes": {c: sharpe(d[c]) for c in cols},
        "combo_sharpe": sharpe(d["y"]),
    }


def verdict(fit: dict) -> dict:
    rs = fit.get("resid_sharpe")
    ta = fit.get("t_alpha")
    ra = fit.get("resid_ann")
    if rs is None or ta is None:
        return {"label": "MARGINAL_ARCHIVE", "reason": "insufficient residual stats"}
    if (rs < 0.3) or (abs(ta) < 1.0) or (abs(ra or 0) < 0.01):
        return {
            "label": "ARCHIVE_DEAD",
            "reason": (
                f"residual Sharpe={rs:.3f}, t_alpha={ta:.2f}, resid_ann={ra:.3%} "
                "after SPY+200MA and earnings_yield - no independent alpha left"
            ),
        }
    if rs >= 0.5 and ta >= 1.5:
        return {
            "label": "KEEP_DIGGING",
            "reason": (
                f"residual Sharpe={rs:.3f}, t_alpha={ta:.2f} still material after "
                "SPY+200MA / EY — investigate orthogonal piece"
            ),
        }
    return {
        "label": "MARGINAL_ARCHIVE",
        "reason": (
            f"residual Sharpe={rs:.3f}, t_alpha={ta:.2f} weak; lean archive unless "
            "a cleaner orthogonal construction appears"
        ),
    }


def write_note(results: dict) -> None:
    oos = results["oos"]
    fit = oos["residual_vs_spy_timing_and_ey"]
    v = results["verdict"]
    NOTE.write_text(
        f"""# Phase 12.5 — CS Combo Residualize

Date: 2026-08-03  
LIVE: **LOCKED** · alpha: **NOT_FOUND**

## Verdict

**{v['label']}**

{v['reason']}

## Setup

Same Gate13 book: weekly top-10%, max 5% weight, 10bps RT.  
Combo = equal CS-rank of `vol_adj_rev_5` + industry-neutral `near_high_252` + `earnings_yield` + `neg_leverage`.

Regression (OOS 2024-01-01 .. 2025-12-31):

```
R_combo = a + b1 * R_spy_timing + b2 * R_earnings_yield + e
```

## OOS numbers

| Metric | Value |
|--------|------:|
| Combo Sharpe | {oos['combo_sharpe']:.3f} |
| SPY+200MA Sharpe | {oos['spy_timing_sharpe']:.3f} |
| EY sleeve Sharpe | {oos['ey_sharpe']:.3f} |
| Residual Sharpe | **{fit['resid_sharpe']:.3f}** |
| Residual ann | {fit['resid_ann']:.2%} |
| Alpha ann (intercept) | {fit['alpha_ann']:.2%} |
| t(alpha) | {fit['t_alpha']:.2f} |
| R² | {fit['r2']:.3f} |
| beta_spy_timing | {fit['betas'].get('spy_timing', float('nan')):.3f} |
| beta_ey | {fit['betas'].get('earnings_yield', float('nan')):.3f} |

Unary checks:
- vs SPY+200MA alone: alpha_ann={oos['vs_spy_timing_only']['alpha_ann']}, t={oos['vs_spy_timing_only']['t_alpha']}
- vs EY alone: alpha_ann={oos['vs_ey_only']['alpha_ann']}, t={oos['vs_ey_only']['t_alpha']}

## Decision

1. CS combo Track A = research archive ({v['label']}).
2. PEAD remains PASS_AS_EVENT only (event module, not continuous alpha).
3. Do **not** unlock LIVE. Live candidates = 0.
4. Still **no ML**.

Artifact: `data/research/alpha_v2_combo_residualize.json`
""",
        encoding="utf-8",
    )


def main() -> None:
    close = pd.read_parquet(CACHE)
    elig = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in elig.columns]
    close = close[cols]
    elig = elig[cols].reindex(close.index).fillna(False)

    combo_score = build_score(close, elig)
    combo = daily_port_returns(close, combo_score, COST_RT)

    full = pd.read_parquet(CACHE)
    if "SPY" in close.columns:
        spy = close["SPY"].pct_change()
    elif "SPY" in full.columns:
        spy = full["SPY"].pct_change().reindex(close.index)
    else:
        spy = close.pct_change().mean(axis=1)

    spy_px = (1 + spy.fillna(0)).cumprod()
    ma200 = spy_px.rolling(200).mean()
    spy_timing = spy.where(spy_px > ma200, 0.0)

    fund = pd.read_parquet(FUND)
    qv = build_quality_value(close, fund)
    ey = qv["earnings_yield"].where(elig)
    ey_port = simple_factor_port(close, elig, ey, COST_RT)

    mask = (combo.index >= OOS_START) & (combo.index <= OOS_END)
    c = combo.loc[mask]
    factors = pd.concat(
        [
            spy_timing.reindex(c.index).fillna(0.0).rename("spy_timing"),
            ey_port.reindex(c.index).rename("earnings_yield"),
        ],
        axis=1,
    )
    fit = multifactor_ols(c, factors)
    vs_timing = ols_alpha(c, spy_timing.reindex(c.index).fillna(0.0))
    vs_ey = ols_alpha(c, ey_port.reindex(c.index))

    v = verdict(fit)
    results = {
        "meta": {
            "combo": "vol_adj_rev_5 + near_high_252_n + earnings_yield + neg_leverage",
            "cost_rt": COST_RT,
            "oos": [OOS_START, OOS_END],
            "regressors": ["spy_timing", "earnings_yield"],
        },
        "oos": {
            "combo_sharpe": sharpe(c),
            "combo_ann": float(c.mean() * 252),
            "spy_timing_sharpe": sharpe(spy_timing.reindex(c.index).fillna(0.0)),
            "ey_sharpe": sharpe(ey_port.reindex(c.index)),
            "vs_spy_timing_only": vs_timing,
            "vs_ey_only": vs_ey,
            "residual_vs_spy_timing_and_ey": fit,
        },
        "verdict": v,
        "recommendation": v["label"] + " - " + v["reason"],
    }

    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    write_note(results)
    print(json.dumps(results["oos"]["residual_vs_spy_timing_and_ey"], indent=2, default=float))
    print(results["recommendation"])
    print(f"Saved {OUT}")
    print(f"Saved {NOTE}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st["updated_at"] = "2026-08-03"
    st["phase"] = "phase12.5_cs_residualize"
    st["live"] = "LOCKED"
    track_a = st.setdefault("Track_A", {})
    best = track_a.setdefault("best_combo", {})
    best["residualize"] = {
        "verdict": v["label"],
        "resid_sharpe": fit.get("resid_sharpe"),
        "t_alpha": fit.get("t_alpha"),
        "resid_ann": fit.get("resid_ann"),
        "r2": fit.get("r2"),
        "artifact": "data/research/alpha_v2_combo_residualize.json",
    }
    if v["label"] in ("ARCHIVE_DEAD", "MARGINAL_ARCHIVE"):
        best["status"] = "research_archive_residual_dead"
    else:
        best["status"] = "residual_keep_digging"
    st.setdefault("strategies", {}).setdefault("combo_rev_trend_ey_leverage", {})
    st["strategies"]["combo_rev_trend_ey_leverage"]["status"] = (
        "research_archive_residual_dead"
        if v["label"] != "KEEP_DIGGING"
        else "residual_keep_digging"
    )
    st["strategies"]["combo_rev_trend_ey_leverage"]["live"] = False
    st["live_candidates"] = {
        "count": 0,
        "note": f"CS combo {v['label']}; PEAD PASS_AS_EVENT only; no live candidates",
    }
    sys_ = st.setdefault("system", {})
    sys_["live"] = "LOCKED"
    sys_["alpha"] = "NOT_FOUND"
    sys_["next"] = [
        f"CS combo: {v['label']}",
        "PEAD: PASS_AS_EVENT (event module only)",
        "Do not unlock LIVE",
        "No ML yet",
    ]
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Updated {STATUS}")


if __name__ == "__main__":
    main()
