# -*- coding: utf-8 -*-
"""ma_trend next-step validation: drop 2026 noise + risk overlay.

A) PIT OOS on 2024-2025 only (exclude short 2026 window)
B) Risk overlay:
   - SPY < MA200  -> exposure 0.30
   - VIX >= 30    -> exposure 0.00 (pause)
   - VIX >= 25    -> exposure min(current, 0.50)
Target: compress maxDD from ~-27% toward -15%~-20% without killing Sharpe>0.8.

Usage:
  .venv\\Scripts\\python.exe scripts/ma_trend_risk_overlay.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from cross_section_bt import factor_trend_strength, run_cs_backtest
from hard_gate9_pit_universe import (
    CACHE,
    ELIG_CACHE,
    FEE,
    build_eligible,
    load_monthly_membership,
    year_hit_rate,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ma_trend_risk_overlay.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def load_pit_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    close = pd.read_parquet(CACHE)
    keep = [c for c in close.columns if close[c].notna().sum() >= 250]
    close = close[keep]
    monthly = load_monthly_membership("2021-01-01")
    if ELIG_CACHE.exists():
        elig = pd.read_parquet(ELIG_CACHE)
        if list(elig.columns) != list(close.columns) or len(elig.index) != len(close.index):
            ELIG_CACHE.unlink()
            elig = build_eligible(close, monthly)
        else:
            elig = elig.astype(bool)
    else:
        elig = build_eligible(close, monthly)
    return close, elig


def load_spy_vix(start: str = "2020-01-01") -> tuple[pd.Series, pd.Series]:
    raw = yf.download(
        ["SPY", "^VIX"],
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    if isinstance(raw.columns, pd.MultiIndex):
        spy = raw["SPY"]["Close"] if "SPY" in raw.columns.get_level_values(0) else raw["Close"]["SPY"]
        vix = raw["^VIX"]["Close"] if "^VIX" in raw.columns.get_level_values(0) else raw["Close"]["^VIX"]
    else:
        raise RuntimeError("unexpected yfinance layout for SPY/VIX")
    spy = pd.Series(pd.to_numeric(spy, errors="coerce"), index=pd.to_datetime(spy.index), name="SPY")
    vix = pd.Series(pd.to_numeric(vix, errors="coerce"), index=pd.to_datetime(vix.index), name="VIX")
    return spy.sort_index().ffill(), vix.sort_index().ffill()


def build_exposure(
    index: pd.DatetimeIndex,
    spy: pd.Series,
    vix: pd.Series,
    *,
    ma: int = 200,
    below_ma_exp: float = 0.30,
    vix_reduce: float = 25.0,
    vix_pause: float = 30.0,
    vix_reduce_exp: float = 0.50,
) -> pd.Series:
    spy = spy.reindex(index).ffill()
    vix = vix.reindex(index).ffill()
    ma200 = spy.rolling(ma, min_periods=ma).mean()
    # delay 1 day: decide today's exposure from yesterday's completed bars
    trend_ok = (spy > ma200).shift(1)
    vix_y = vix.shift(1)

    exp = pd.Series(1.0, index=index)
    exp = exp.where(trend_ok.fillna(True), below_ma_exp)
    exp = exp.where(~(vix_y >= vix_reduce), np.minimum(exp, vix_reduce_exp))
    exp = exp.where(~(vix_y >= vix_pause), 0.0)
    return exp.fillna(1.0).clip(0.0, 1.0)


def summarize(name: str, m, eq: pd.Series) -> dict:
    return {
        "name": name,
        "ann": m.ann_return,
        "sharpe": m.sharpe,
        "maxdd": m.max_dd,
        "total": m.total_return,
        "turnover": m.turnover,
        "year_hit": year_hit_rate(eq),
        "gate_numeric": {
            "sharpe_gt_0_8": m.sharpe > 0.8,
            "ann_gt_0_10": m.ann_return > 0.10,
            "dd_lt_0_30": abs(m.max_dd) < 0.30,
            "dd_target_lt_0_20": abs(m.max_dd) < 0.20,
        },
    }


def main() -> None:
    close, elig = load_pit_panel()
    spy, vix = load_spy_vix()
    exposure = build_exposure(close.index, spy, vix)

    windows = {
        "oos_2024_2025_no2026": ("2024-01-01", "2025-12-31"),
        "oos_2024_only": ("2024-01-01", "2024-12-31"),
        "oos_2025_only": ("2025-01-01", "2025-12-31"),
        "full_incl_2026": ("2024-01-01", None),
    }

    rows = []
    print("=== ma_trend baseline vs risk overlay ===")
    for wname, (start, end) in windows.items():
        base, eq_b = run_cs_backtest(
            "ma_trend_base",
            close,
            factor_trend_strength,
            lookback=60,
            top_k=5,
            rebalance=10,
            fee=FEE,
            eval_start=start,
            eval_end=end,
            eligible=elig,
        )
        ov, eq_o = run_cs_backtest(
            "ma_trend_overlay",
            close,
            factor_trend_strength,
            lookback=60,
            top_k=5,
            rebalance=10,
            fee=FEE,
            eval_start=start,
            eval_end=end,
            eligible=elig,
            exposure=exposure,
        )
        sb = summarize("baseline", base, eq_b)
        so = summarize("overlay", ov, eq_o)
        rows.append({"window": wname, "baseline": sb, "overlay": so})
        print(
            f"\n{wname}\n"
            f"  base   ann={base.ann_return:6.1%} S={base.sharpe:4.2f} DD={base.max_dd:6.1%}\n"
            f"  overlay ann={ov.ann_return:6.1%} S={ov.sharpe:4.2f} DD={ov.max_dd:6.1%} "
            f"DDdelta={ov.max_dd - base.max_dd:+.1%}"
        )

    primary = next(r for r in rows if r["window"] == "oos_2024_2025_no2026")
    b, o = primary["baseline"], primary["overlay"]
    keep_sharpe = o["sharpe"] >= 0.80 or o["sharpe"] >= b["sharpe"] * 0.9
    dd_improved = abs(o["maxdd"]) < abs(b["maxdd"])
    dd_target = abs(o["maxdd"]) <= 0.20
    verdict = {
        "primary_window": "oos_2024_2025_no2026",
        "baseline_still_gate9ish": b["sharpe"] > 0.8 and b["ann"] > 0.10 and abs(b["maxdd"]) < 0.30,
        "overlay_sharpe_ok": keep_sharpe,
        "overlay_dd_improved": dd_improved,
        "overlay_dd_target_hit": dd_target,
        "recommendation": (
            "enable_overlay_on_paper"
            if keep_sharpe and dd_improved
            else "keep_baseline_paper_monitor_overlay"
            if b["sharpe"] > 0.8
            else "rework_ma_trend"
        ),
    }
    print(f"\nVERDICT: {verdict['recommendation']}")
    print(
        f"no-2026 baseline S={b['sharpe']:.2f} DD={b['maxdd']:.1%} | "
        f"overlay S={o['sharpe']:.2f} DD={o['maxdd']:.1%}"
    )

    out = {
        "fee_bps": FEE * 10000,
        "overlay_rules": {
            "spy_below_ma200_exposure": 0.30,
            "vix_reduce_threshold": 25,
            "vix_reduce_exposure": 0.50,
            "vix_pause_threshold": 30,
        },
        "delisted_gap_note": (
            "Price panel still lacks delisted/acquired members; "
            "true worst-case DD likely worse than reported."
        ),
        "windows": rows,
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")

    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["ma_trend_validation"] = {
            "no_2026_baseline": b,
            "no_2026_overlay": o,
            "verdict": verdict["recommendation"],
        }
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
