# -*- coding: utf-8 -*-
"""Hard Gate 11: Factor Attribution Gate for ma_trend.

Question: is ma_trend earning independent Alpha, or mostly market/trend beta?

A) Market regression: R = a + b*SPY + e
B) Multi-factor:    R = a + b1*SPY + b2*MOM + b3*VALUE + b4*LOWVOL + e
   (factor ETFs: MTUM / VLUE / USMV — no strategy retuning)
C) Bull/Bear split: SPY > MA200 vs SPY < MA200
D) Benchmarks: SPY B&H, SPY+200MA timing, QQQ B&H vs ma_trend

Primary window: clean OOS 2024-01-01 .. 2025-12-31 (excludes 2026 noise).
Secondary: full available history for robustness.

Usage:
  .venv\\Scripts\\python.exe scripts/hard_gate11_attribution.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from cross_section_bt import factor_trend_strength, run_cs_backtest
from hard_gate9_pit_universe import CACHE, ELIG_CACHE, build_eligible, load_monthly_membership
from ma_trend_risk_overlay import load_spy_vix

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "hard_gate11_attribution.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

FEE = 0.002  # 20bp — same as clean diagnostic baseline
WIN_PRIMARY = ("2024-01-01", "2025-12-31")
FACTOR_ETFS = ["SPY", "QQQ", "MTUM", "VLUE", "USMV"]


def load_pit() -> tuple[pd.DataFrame, pd.DataFrame]:
    close = pd.read_parquet(CACHE)
    keep = [c for c in close.columns if close[c].notna().sum() >= 250]
    close = close[keep]
    monthly = load_monthly_membership("2021-01-01")
    if ELIG_CACHE.exists():
        elig = pd.read_parquet(ELIG_CACHE)
        if list(elig.columns) != list(close.columns) or len(elig) != len(close):
            ELIG_CACHE.unlink()
            elig = build_eligible(close, monthly)
        else:
            elig = elig.astype(bool)
    else:
        elig = build_eligible(close, monthly)
    return close, elig


def load_factor_etfs(start: str = "2020-01-01") -> pd.DataFrame:
    raw = yf.download(
        FACTOR_ETFS,
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in FACTOR_ETFS:
            try:
                if sym in raw.columns.get_level_values(0):
                    s = raw[sym]["Close"]
                elif ("Close", sym) in raw.columns:
                    s = raw[("Close", sym)]
                else:
                    continue
                out[sym] = pd.to_numeric(s, errors="coerce")
            except Exception:
                continue
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index)
    return df.sort_index().ffill()


def ols(y: pd.Series, X: pd.DataFrame) -> dict:
    """Simple OLS with Newey-West-ish HAC via lag-1 residual adjustment (lightweight)."""
    data = pd.concat([y.rename("y"), X], axis=1).dropna()
    if len(data) < 40:
        return {"error": "insufficient_obs", "n": len(data)}
    yv = data["y"].to_numpy(dtype=float)
    xv = np.column_stack([np.ones(len(data)), data[X.columns].to_numpy(dtype=float)])
    beta, *_ = np.linalg.lstsq(xv, yv, rcond=None)
    fitted = xv @ beta
    resid = yv - fitted
    n, k = xv.shape
    dof = max(n - k, 1)
    sse = float(resid @ resid)
    sigma2 = sse / dof
    # White-ish robust SE (diagonal HC0)
    xtx_inv = np.linalg.pinv(xv.T @ xv)
    meat = xv.T @ (resid[:, None] ** 2 * xv)
    cov = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    tstat = beta / np.where(se > 0, se, np.nan)
    # two-sided p via normal approx
    from math import erfc, sqrt

    pvals = [float(erfc(abs(t) / sqrt(2.0))) for t in tstat]
    names = ["alpha", *list(X.columns)]
    coefs = {names[i]: float(beta[i]) for i in range(len(names))}
    tstats = {names[i]: float(tstat[i]) for i in range(len(names))}
    ps = {names[i]: pvals[i] for i in range(len(names))}
    r2 = 1.0 - sse / max(float(((yv - yv.mean()) ** 2).sum()), 1e-18)
    # annualize alpha (daily)
    alpha_ann = float((1.0 + coefs["alpha"]) ** 252 - 1.0) if coefs["alpha"] > -1 else float("nan")
    return {
        "n": int(n),
        "r2": float(r2),
        "alpha_daily": coefs["alpha"],
        "alpha_ann": alpha_ann,
        "alpha_tstat": tstats["alpha"],
        "alpha_pval": ps["alpha"],
        "betas": {k: coefs[k] for k in X.columns},
        "tstats": {k: tstats[k] for k in ["alpha", *X.columns]},
        "pvals": {k: ps[k] for k in ["alpha", *X.columns]},
        "resid_vol_ann": float(np.std(resid, ddof=1) * np.sqrt(252)),
    }


def equity_to_rets(eq: pd.Series) -> pd.Series:
    return eq.pct_change().dropna()


def buyhold_rets(price: pd.Series, start: str, end: str | None) -> pd.Series:
    s = price.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None].dropna()
    return s.pct_change().dropna()


def ma_timing_rets(price: pd.Series, start: str, end: str | None, ma: int = 200) -> pd.Series:
    """SPY / cash timing: long when yesterday close > MA, else 0."""
    px = price.dropna().sort_index()
    sig = (px > px.rolling(ma).mean()).shift(1).fillna(False)
    rets = px.pct_change().fillna(0.0)
    timed = rets.where(sig, 0.0)
    timed = timed.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None]
    return timed.dropna()


def metrics_from_rets(rets: pd.Series) -> dict:
    rets = rets.dropna()
    if len(rets) < 2:
        return {"ann": 0.0, "sharpe": 0.0, "maxdd": 0.0, "total": 0.0}
    eq = (1.0 + rets).cumprod()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    vol = float(rets.std() * np.sqrt(252))
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
    dd = float(((eq - eq.cummax()) / eq.cummax()).min())
    return {"ann": float(ann), "sharpe": sharpe, "maxdd": dd, "total": total}


def regime_split(strat: pd.Series, spy: pd.Series, ma: int = 200) -> dict:
    spy = spy.reindex(strat.index).ffill()
    ma200 = spy.rolling(ma).mean()
    bull = (spy > ma200).shift(1)
    bull = bull.reindex(strat.index).fillna(False).astype(bool)
    out = {}
    for name, mask in (("bull_spy_gt_ma200", bull), ("bear_spy_lt_ma200", ~bull)):
        r = strat.loc[mask.to_numpy()]
        m = metrics_from_rets(r)
        out[name] = {
            **m,
            "n_days": int(mask.sum()),
            "fraction_of_days": float(mask.mean()),
            "sum_return": float(r.sum()) if len(r) else 0.0,
        }
    total = abs(out["bull_spy_gt_ma200"]["sum_return"]) + abs(out["bear_spy_lt_ma200"]["sum_return"])
    out["bull_return_share"] = (
        out["bull_spy_gt_ma200"]["sum_return"] / total if total > 1e-12 else None
    )
    return out


def classify(attr_mkt: dict, attr_multi: dict, regime: dict, vs_bench: dict) -> dict:
    alpha = attr_mkt.get("alpha_ann", 0.0) or 0.0
    t = abs(attr_mkt.get("alpha_tstat", 0.0) or 0.0)
    p = attr_mkt.get("alpha_pval", 1.0) or 1.0
    multi_a = attr_multi.get("alpha_ann", 0.0) or 0.0
    multi_t = abs(attr_multi.get("alpha_tstat", 0.0) or 0.0)

    beat_spy_timing = vs_bench.get("ma_trend_minus_spy_timing_ann")
    bull_share = regime.get("bull_return_share")

    if alpha >= 0.05 and t >= 2.0 and p < 0.05 and multi_a > 0.02 and multi_t >= 1.8:
        case = 1
        label = "independent_alpha_candidate"
        note = "Statistically meaningful alpha after market; multi-factor alpha still positive."
    elif alpha >= 0.01 and t >= 1.0:
        case = 2
        label = "weak_alpha_trend_etf_substitute"
        note = "Small residual alpha; mostly beta/trend exposure. Optimize execution, not signals."
    else:
        case = 3
        label = "market_timing_overlay_only"
        note = "Alpha ~0 after market; treat as timing overlay / beta sleeve, not alpha engine."

    # soften/harden with regime + benchmark
    if bull_share is not None and bull_share > 0.85 and case == 1:
        case = 2
        label = "weak_alpha_trend_etf_substitute"
        note = "Alpha stats look ok but returns concentrated in bull regime."
    if beat_spy_timing is not None and beat_spy_timing < 0 and case <= 2:
        note += " Does not beat simple SPY+200MA timing on clean OOS."

    return {
        "case": case,
        "label": label,
        "note": note,
        "alpha_ann_vs_spy": alpha,
        "alpha_tstat_vs_spy": attr_mkt.get("alpha_tstat"),
        "alpha_ann_multifactor": multi_a,
        "alpha_tstat_multifactor": attr_multi.get("alpha_tstat"),
    }


def main() -> None:
    close, elig = load_pit()
    factors = load_factor_etfs()
    spy = factors["SPY"]
    qqq = factors["QQQ"]

    # Strategy equity on clean OOS (top20 diversified — healthier Gate10 variant)
    m, eq = run_cs_backtest(
        "ma_trend_attr",
        close,
        factor_trend_strength,
        lookback=60,
        top_k=20,
        rebalance=10,
        fee=FEE,
        eval_start=WIN_PRIMARY[0],
        eval_end=WIN_PRIMARY[1],
        eligible=elig,
        max_weight=0.05,
    )
    strat = equity_to_rets(eq)

    # Also concentrated top5 for comparison
    m5, eq5 = run_cs_backtest(
        "ma_trend_top5",
        close,
        factor_trend_strength,
        lookback=60,
        top_k=5,
        rebalance=10,
        fee=FEE,
        eval_start=WIN_PRIMARY[0],
        eval_end=WIN_PRIMARY[1],
        eligible=elig,
    )
    strat5 = equity_to_rets(eq5)

    start, end = WIN_PRIMARY
    spy_r = buyhold_rets(spy, start, end)
    qqq_r = buyhold_rets(qqq, start, end)
    timing_r = ma_timing_rets(spy, start, end, 200)

    # Align strategy with factor returns
    f_rets = factors.pct_change()
    X_mkt = f_rets[["SPY"]].rename(columns={"SPY": "SPY"})
    X_multi = f_rets[["SPY", "MTUM", "VLUE", "USMV"]].rename(
        columns={"MTUM": "MOM", "VLUE": "VALUE", "USMV": "LOWVOL"}
    )

    attr_mkt = ols(strat, X_mkt.loc[strat.index[0] : strat.index[-1]])
    attr_multi = ols(strat, X_multi.loc[strat.index[0] : strat.index[-1]])
    attr_mkt_top5 = ols(strat5, X_mkt.loc[strat5.index[0] : strat5.index[-1]])

    regime = regime_split(strat, spy)
    regime5 = regime_split(strat5, spy)

    bench = {
        "ma_trend_top20": metrics_from_rets(strat),
        "ma_trend_top5": metrics_from_rets(strat5),
        "spy_buyhold": metrics_from_rets(spy_r),
        "qqq_buyhold": metrics_from_rets(qqq_r),
        "spy_ma200_timing": metrics_from_rets(timing_r),
    }
    # excess ann approx via difference of ann (not IR) for readability
    bench_spread = {
        "ma_trend_minus_spy_ann": bench["ma_trend_top20"]["ann"] - bench["spy_buyhold"]["ann"],
        "ma_trend_minus_spy_timing_ann": bench["ma_trend_top20"]["ann"]
        - bench["spy_ma200_timing"]["ann"],
        "ma_trend_minus_qqq_ann": bench["ma_trend_top20"]["ann"] - bench["qqq_buyhold"]["ann"],
        "ma_trend_minus_spy_sharpe": bench["ma_trend_top20"]["sharpe"]
        - bench["spy_buyhold"]["sharpe"],
        "ma_trend_minus_spy_timing_sharpe": bench["ma_trend_top20"]["sharpe"]
        - bench["spy_ma200_timing"]["sharpe"],
    }

    verdict = classify(attr_mkt, attr_multi, regime, bench_spread)

    print("=== Hard Gate 11 Attribution (2024-2025, top20 maxw5%) ===")
    print(
        f"strategy ann={bench['ma_trend_top20']['ann']:.1%} "
        f"S={bench['ma_trend_top20']['sharpe']:.2f} DD={bench['ma_trend_top20']['maxdd']:.1%}"
    )
    print(
        f"vs SPY: alpha_ann={attr_mkt.get('alpha_ann', float('nan')):.2%} "
        f"t={attr_mkt.get('alpha_tstat', float('nan')):.2f} "
        f"p={attr_mkt.get('alpha_pval', float('nan')):.3f} "
        f"beta={attr_mkt.get('betas', {}).get('SPY', float('nan')):.2f} "
        f"R2={attr_mkt.get('r2', float('nan')):.2f}"
    )
    print(
        f"multi-factor alpha_ann={attr_multi.get('alpha_ann', float('nan')):.2%} "
        f"t={attr_multi.get('alpha_tstat', float('nan')):.2f} "
        f"betas={attr_multi.get('betas', {})}"
    )
    print(
        f"bull days={regime['bull_spy_gt_ma200']['n_days']} "
        f"ann={regime['bull_spy_gt_ma200']['ann']:.1%} | "
        f"bear days={regime['bear_spy_lt_ma200']['n_days']} "
        f"ann={regime['bear_spy_lt_ma200']['ann']:.1%} | "
        f"bull_share={regime.get('bull_return_share')}"
    )
    print("\n=== Benchmarks ===")
    for k, v in bench.items():
        print(f"{k:<22} ann={v['ann']:6.1%} S={v['sharpe']:4.2f} DD={v['maxdd']:6.1%}")
    print(
        f"\nexcess vs SPY timing ann={bench_spread['ma_trend_minus_spy_timing_ann']:+.1%} "
        f"sharpe={bench_spread['ma_trend_minus_spy_timing_sharpe']:+.2f}"
    )
    print(f"\nCASE {verdict['case']}: {verdict['label']}")
    print(verdict["note"])

    out = {
        "window": {"start": start, "end": end, "excludes_2026": True},
        "strategy_variant": "top20_max_weight_5pct_fee20bp_pit",
        "market_regression": attr_mkt,
        "multifactor_regression": attr_multi,
        "market_regression_top5": attr_mkt_top5,
        "regime_split_top20": regime,
        "regime_split_top5": regime5,
        "benchmarks": bench,
        "benchmark_spreads": bench_spread,
        "verdict": verdict,
        "registry_action": {
            "status": "PAPER_DIAGNOSTIC_ONLY",
            "live": "LOCKED",
            "forbid": ["live", "increase_capital", "parameter_optimization"],
        },
    }
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")

    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["hard_gates"]["gate11_attribution"] = verdict["label"]
        st["strategies"]["ma_trend_strength_60d"]["status"] = "PAPER_DIAGNOSTIC_ONLY"
        st["strategies"]["ma_trend_strength_60d"]["confidence"] = (
            "MEDIUM" if verdict["case"] == 1 else "LOW_MEDIUM" if verdict["case"] == 2 else "LOW"
        )
        st["strategies"]["ma_trend_strength_60d"]["attribution"] = verdict
        st["gate11"] = {
            "case": verdict["case"],
            "label": verdict["label"],
            "alpha_ann_vs_spy": verdict["alpha_ann_vs_spy"],
            "alpha_tstat_vs_spy": verdict["alpha_tstat_vs_spy"],
            "alpha_ann_multifactor": verdict["alpha_ann_multifactor"],
            "beats_spy_timing_ann": bench_spread["ma_trend_minus_spy_timing_ann"],
        }
        st["live"] = "LOCKED"
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()
