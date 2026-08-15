# -*- coding: utf-8 -*-
"""Robustness checks for shortlisted cross-sectional strategies."""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from cross_section_bt import (
    END,
    START,
    UNIVERSE,
    factor_rank_composite,
    factor_skip5_mom,
    factor_trend_strength,
    load_close_panel,
    run_cs_backtest,
)

OUT = Path(__file__).resolve().parents[1] / "data" / "cross_section_robustness.csv"

CANDIDATES = {
    "ma_trend_strength_60d": (factor_trend_strength, 60),
    "rotation_skip5_mom": (factor_skip5_mom, 125),
    "panda_rank_composite": (factor_rank_composite, 60),
}

PERIODS = {
    "early_2021_2023": ("2021-07-01", "2023-12-31"),
    "oos_2024_now": ("2024-01-01", None),
    "recent_2025_now": ("2025-01-01", None),
}


def aligned_benchmark(
    close: pd.DataFrame, start: str, end: str | None
) -> tuple[float, float, float]:
    sample = close.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None]
    daily = sample.pct_change().mean(axis=1).dropna()
    equity = (1.0 + daily).cumprod()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    annual = float((1.0 + total) ** (1.0 / years) - 1.0)
    vol = float(daily.std() * 252**0.5)
    sharpe = float(daily.mean() * 252 / vol) if vol > 1e-12 else 0.0
    return total, annual, sharpe


def record(
    rows: list[dict[str, object]],
    *,
    test: str,
    period: str,
    strategy: str,
    metric,
    benchmark_ann: float | None = None,
    top_k: int = 5,
    rebalance: int = 5,
    fee: float = 0.0005,
) -> None:
    rows.append(
        {
            "test": test,
            "period": period,
            "strategy": strategy,
            "top_k": top_k,
            "rebalance_days": rebalance,
            "fee_bps": fee * 10_000,
            "total_return": metric.total_return,
            "annual_return": metric.ann_return,
            "sharpe": metric.sharpe,
            "max_drawdown": metric.max_dd,
            "turnover_per_rebalance": metric.turnover,
            "benchmark_annual": benchmark_ann,
            "annual_excess": (
                metric.ann_return - benchmark_ann
                if benchmark_ann is not None
                else None
            ),
        }
    )


def main() -> None:
    close = load_close_panel(UNIVERSE, START, END)
    rows: list[dict[str, object]] = []

    print("\n=== Rolling periods ===")
    for period, (start, end) in PERIODS.items():
        _, bench_ann, bench_sharpe = aligned_benchmark(close, start, end)
        print(f"\n{period}: benchmark ann={bench_ann:.1%}, sharpe={bench_sharpe:.2f}")
        for name, (fn, lookback) in CANDIDATES.items():
            metric, _ = run_cs_backtest(
                name,
                close,
                fn,
                lookback=lookback,
                eval_start=start,
                eval_end=end,
            )
            record(
                rows,
                test="rolling_period",
                period=period,
                strategy=name,
                metric=metric,
                benchmark_ann=bench_ann,
            )
            print(
                f"{name:<26} ann={metric.ann_return:6.1%} "
                f"excess={metric.ann_return-bench_ann:6.1%} "
                f"S={metric.sharpe:4.2f} DD={metric.max_dd:6.1%}"
            )

    print("\n=== OOS parameter perturbation (2024-now) ===")
    oos_start = "2024-01-01"
    _, bench_ann, _ = aligned_benchmark(close, oos_start, None)
    for name, (fn, lookback) in CANDIDATES.items():
        for top_k in (3, 5, 8):
            for rebalance in (5, 10, 20):
                metric, _ = run_cs_backtest(
                    name,
                    close,
                    fn,
                    lookback=lookback,
                    top_k=top_k,
                    rebalance=rebalance,
                    eval_start=oos_start,
                )
                record(
                    rows,
                    test="parameter",
                    period="oos_2024_now",
                    strategy=name,
                    metric=metric,
                    benchmark_ann=bench_ann,
                    top_k=top_k,
                    rebalance=rebalance,
                )

    print("\n=== OOS cost stress (2024-now) ===")
    for name, (fn, lookback) in CANDIDATES.items():
        for fee in (0.0005, 0.001, 0.002):
            metric, _ = run_cs_backtest(
                name,
                close,
                fn,
                lookback=lookback,
                fee=fee,
                eval_start=oos_start,
            )
            record(
                rows,
                test="cost",
                period="oos_2024_now",
                strategy=name,
                metric=metric,
                benchmark_ann=bench_ann,
                fee=fee,
            )
            print(
                f"{name:<26} fee={fee*10000:4.0f}bp "
                f"ann={metric.ann_return:6.1%} S={metric.sharpe:4.2f}"
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} checks to {OUT}")

    parameter_rows = [r for r in rows if r["test"] == "parameter"]
    print("\n=== Parameter robustness summary ===")
    for name in CANDIDATES:
        subset = [r for r in parameter_rows if r["strategy"] == name]
        positive = sum(float(r["annual_excess"]) > 0 for r in subset)
        med_ann = pd.Series([float(r["annual_return"]) for r in subset]).median()
        med_sharpe = pd.Series([float(r["sharpe"]) for r in subset]).median()
        worst_dd = min(float(r["max_drawdown"]) for r in subset)
        print(
            f"{name:<26} beat={positive}/{len(subset)} "
            f"median_ann={med_ann:.1%} median_S={med_sharpe:.2f} "
            f"worst_DD={worst_dd:.1%}"
        )


if __name__ == "__main__":
    main()
