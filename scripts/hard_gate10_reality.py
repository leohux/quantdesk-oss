# -*- coding: utf-8 -*-
"""Hard Gate 10: Reality Gate for ma_trend (no parameter search).

Question: does ma_trend still make money under realistic constraints?

Checks (fixed strategy params; no MA/RSI retuning):
  - PIT S&P membership (cached)
  - T+1 signal delay (already in engine)
  - Continuous OOS years 2024 & 2025 (exclude 2026 noise)
  - Fee stress: 20bp / 40bp / 60bp one-way
  - Capacity: max single-name weight 5% (top_k=20)
  - Optional SPY/VIX overlay
  - ETF-universe stability probe (no tuning)

Pass bar:
  Sharpe > 0.8
  MaxDD < 30%
  Both 2024 and 2025 profitable
  Turnover acceptable (< 1.0 per rebalance notionally)

Usage:
  .venv\\Scripts\\python.exe scripts/hard_gate10_reality.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yfinance as yf

from cross_section_bt import factor_trend_strength, run_cs_backtest
from hard_gate9_pit_universe import (
    CACHE,
    ELIG_CACHE,
    build_eligible,
    load_monthly_membership,
    year_hit_rate,
)
from ma_trend_risk_overlay import build_exposure, load_spy_vix

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "hard_gate10_reality.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

ETF_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA", "MDY",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
    "EFA", "EEM", "VGK", "EWJ",
    "TLT", "IEF", "HYG", "LQD", "GLD", "SLV", "USO", "UNG",
]


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


def pack(m, eq: pd.Series) -> dict:
    # calendar-year returns for continuous OOS check
    year_rets = {}
    for y, g in eq.groupby(eq.index.year):
        if len(g) >= 2:
            year_rets[str(y)] = float(g.iloc[-1] / g.iloc[0] - 1.0)
    return {
        "ann": m.ann_return,
        "sharpe": m.sharpe,
        "maxdd": m.max_dd,
        "total": m.total_return,
        "turnover": m.turnover,
        "n_rebalances": m.n_rebalances,
        "year_hit": year_hit_rate(eq),
        "year_returns": year_rets,
    }


def gate10_checks(stats: dict) -> dict:
    yr = stats.get("year_returns", {})
    y2024 = yr.get("2024")
    y2025 = yr.get("2025")
    checks = [
        {
            "gate": "sharpe",
            "pass": stats["sharpe"] > 0.8,
            "value": stats["sharpe"],
            "threshold": 0.8,
        },
        {
            "gate": "max_drawdown",
            "pass": abs(stats["maxdd"]) < 0.30,
            "value": stats["maxdd"],
            "threshold": -0.30,
        },
        {
            "gate": "continuous_oos_2024",
            "pass": y2024 is not None and y2024 > 0,
            "value": y2024,
            "threshold": 0.0,
        },
        {
            "gate": "continuous_oos_2025",
            "pass": y2025 is not None and y2025 > 0,
            "value": y2025,
            "threshold": 0.0,
        },
        {
            "gate": "turnover_acceptable",
            "pass": stats["turnover"] < 1.0,
            "value": stats["turnover"],
            "threshold": 1.0,
        },
    ]
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def run_case(
    name: str,
    close: pd.DataFrame,
    elig: pd.DataFrame | None,
    *,
    fee: float,
    top_k: int,
    max_weight: float | None,
    exposure: pd.Series | None,
    start: str = "2024-01-01",
    end: str | None = "2025-12-31",
) -> dict:
    m, eq = run_cs_backtest(
        name,
        close,
        factor_trend_strength,
        lookback=60,
        top_k=top_k,
        rebalance=10,
        fee=fee,
        eval_start=start,
        eval_end=end,
        eligible=elig,
        exposure=exposure,
        max_weight=max_weight,
    )
    stats = pack(m, eq)
    return {"name": name, "stats": stats, "gate10": gate10_checks(stats)}


def load_etf_panel() -> pd.DataFrame:
    raw = yf.download(
        ETF_UNIVERSE,
        start="2021-01-01",
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    series = []
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in ETF_UNIVERSE:
            try:
                if sym in raw.columns.get_level_values(0):
                    s = raw[sym]["Close"]
                elif ("Close", sym) in raw.columns:
                    s = raw[("Close", sym)]
                else:
                    continue
                s = pd.Series(pd.to_numeric(s, errors="coerce"), index=pd.to_datetime(s.index), name=sym)
                if s.notna().sum() > 200:
                    series.append(s[~s.index.duplicated(keep="last")].sort_index())
            except Exception:
                continue
    close = pd.concat(series, axis=1).sort_index().ffill(limit=3)
    return close


def main() -> None:
    close, elig = load_pit()
    spy, vix = load_spy_vix()
    exposure = build_exposure(close.index, spy, vix)

    cases = []
    specs = [
        ("pit_top5_fee20", 0.002, 5, None, None),
        ("pit_top5_fee40", 0.004, 5, None, None),
        ("pit_top5_fee60", 0.006, 5, None, None),
        ("pit_top20_maxw5_fee20", 0.002, 20, 0.05, None),
        ("pit_top20_maxw5_fee40", 0.004, 20, 0.05, None),
        ("pit_top20_maxw5_fee40_overlay", 0.004, 20, 0.05, exposure),
        ("pit_top5_fee40_overlay", 0.004, 5, None, exposure),
    ]

    print("=== Hard Gate 10 Reality (ma_trend, 2024-2025) ===")
    for name, fee, top_k, max_w, exp in specs:
        row = run_case(
            name,
            close,
            elig,
            fee=fee,
            top_k=top_k,
            max_weight=max_w,
            exposure=exp,
        )
        cases.append(row)
        s = row["stats"]
        g = "PASS" if row["gate10"]["pass"] else "FAIL"
        print(
            f"{name:<36} {g}  ann={s['ann']:6.1%} S={s['sharpe']:4.2f} "
            f"DD={s['maxdd']:6.1%} turn={s['turnover']:.2f} "
            f"y24={s['year_returns'].get('2024', float('nan')):+.1%} "
            f"y25={s['year_returns'].get('2025', float('nan')):+.1%}"
        )

    print("\n=== ETF universe stability probe (no PIT equities) ===")
    etf = load_etf_panel()
    etf_exp = build_exposure(etf.index, spy, vix)
    for name, fee, top_k, max_w, use_ov in [
        ("etf_top5_fee40", 0.004, 5, None, False),
        ("etf_top8_fee40", 0.004, 8, None, False),
        ("etf_top5_fee40_overlay", 0.004, 5, None, True),
    ]:
        row = run_case(
            name,
            etf,
            None,
            fee=fee,
            top_k=top_k,
            max_weight=max_w,
            exposure=etf_exp if use_ov else None,
        )
        cases.append(row)
        s = row["stats"]
        g = "PASS" if row["gate10"]["pass"] else "FAIL"
        print(
            f"{name:<36} {g}  ann={s['ann']:6.1%} S={s['sharpe']:4.2f} "
            f"DD={s['maxdd']:6.1%} "
            f"y24={s['year_returns'].get('2024', float('nan')):+.1%} "
            f"y25={s['year_returns'].get('2025', float('nan')):+.1%}"
        )

    any_pass = any(c["gate10"]["pass"] for c in cases if c["name"].startswith("pit_"))
    primary = next(c for c in cases if c["name"] == "pit_top20_maxw5_fee40")
    baseline = next(c for c in cases if c["name"] == "pit_top5_fee20")

    if any_pass:
        rec = "gate10_pass - unexpected clean reality pass; still require delisted completion before Live"
    elif (
        primary["stats"]["sharpe"] >= 0.5
        and abs(primary["stats"]["maxdd"]) < 0.30
        and primary["stats"]["year_returns"].get("2024", -1) > 0
    ):
        rec = (
            "diagnostic_continue - no Gate10 PASS; ma_trend survives mild reality stress "
            "but lacks confirmed alpha (Sharpe<0.8 / not both years green)"
        )
    else:
        rec = (
            "no_stable_alpha - ma_trend fails Reality Gate; keep PAPER_DIAGNOSTIC_ONLY, "
            "do not optimize parameters; consider beta/trend-exposure diagnosis next"
        )

    print(f"\nRECOMMENDATION: {rec}")

    out = {
        "strategy": "ma_trend_strength_60d",
        "window": "2024-01-01 .. 2025-12-31",
        "excludes_2026": True,
        "t_plus_1": True,
        "delisted_gap": "still present (~10% membership price coverage gap)",
        "pass_bar": {
            "sharpe": 0.8,
            "maxdd": 0.30,
            "continuous_years": ["2024", "2025"],
            "turnover": 1.0,
        },
        "baseline_top5_fee20": baseline,
        "reality_top20_fee40": primary,
        "cases": cases,
        "recommendation": rec,
        "gate10_pass": any_pass,
    }
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")

    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["hard_gates"]["gate10_reality"] = "PASS" if any_pass else "FAIL"
        st["gate10"] = {
            "pass": any_pass,
            "recommendation": rec,
            "baseline_sharpe": baseline["stats"]["sharpe"],
            "reality_sharpe": primary["stats"]["sharpe"],
            "reality_maxdd": primary["stats"]["maxdd"],
            "reality_years": primary["stats"]["year_returns"],
        }
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
