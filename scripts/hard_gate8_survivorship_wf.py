# -*- coding: utf-8 -*-
"""Hard Gate 8: Survivorship Bias + Walk-Forward + Cost/Slippage.

Phase 1 — universe de-bias proxies (no paid index-membership history):
  - original: prior 24-name liquid pool
  - no_mega: drop known mega-winners (NVDA/AVGO/META/TSLA/AMD/NFLX/LLY)
  - etf_only: sector/style ETFs only (no single-stock winner bias)
  - broad50: larger liquid survivors (dilution proxy)

Phase 2 — Walk Forward:
  train params on IS grid, freeze on OOS fold.
  Fold1: train 2021-2023 → test 2024
  Fold2: train 2021-2024 → test 2025
  Fold3: train 2022-2025 → test 2026-now

Hard Gate 8 pass (aggregated OOS, 20bp one-way):
  OOS Sharpe > 1.0
  OOS annual return > 10%
  MaxDD < 25%

Usage:
  .venv\\Scripts\\python.exe scripts/hard_gate8_survivorship_wf.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from cross_section_bt import (
    Metrics,
    factor_rank_composite,
    factor_skip5_mom,
    factor_trend_strength,
    load_close_panel,
    run_cs_backtest,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_CSV = ROOT / "data" / "hard_gate8_results.csv"
OUT_JSON = ROOT / "data" / "hard_gate8_summary.json"

MEGA_WINNERS = {"NVDA", "AVGO", "META", "TSLA", "AMD", "NFLX", "LLY"}

ORIGINAL = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "NFLX", "AVGO", "COST", "JPM", "XOM", "UNH", "LLY", "V",
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "GLD",
]
ETF_ONLY = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI",
    "XLP", "XLU", "XLB", "XLRE", "XLC", "GLD", "TLT", "HYG", "EFA", "EEM",
]
BROAD50 = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA", "BRK-B",
    "JPM", "JNJ", "V", "PG", "UNH", "HD", "MA", "XOM", "CVX", "MRK",
    "ABBV", "PEP", "KO", "COST", "WMT", "MCD", "CSCO", "ACN", "CRM",
    "ORCL", "ADBE", "INTC", "IBM", "QCOM", "TXN", "AMGN", "PM", "TMO",
    "NEE", "LIN", "BAC", "WFC", "SPY", "QQQ", "IWM", "XLK", "XLF",
    "XLE", "XLV", "GLD", "TLT", "DIA",
]

UNIVERSES = {
    "original": ORIGINAL,
    "no_mega": [s for s in ORIGINAL if s not in MEGA_WINNERS],
    "etf_only": ETF_ONLY,
    "broad50": BROAD50,
}

STRATEGIES = {
    "rotation_skip5_mom": (factor_skip5_mom, 125),
    "ma_trend_strength_60d": (factor_trend_strength, 60),
    "panda_rank_composite": (factor_rank_composite, 60),
}

FOLDS = [
    # name, train_start, train_end, test_start, test_end
    ("wf1_2024", "2021-07-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("wf2_2025", "2021-07-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("wf3_2026", "2022-01-01", "2025-12-31", "2026-01-01", None),
]

PARAM_GRID = [(3, 5), (5, 5), (5, 10), (8, 10), (5, 20), (8, 20)]
# 10bp fee + 10bp slippage = 20bp one-way all-in
FEE_ALL_IN = 0.002


def metrics_from_equity(name: str, eq: pd.Series) -> Metrics:
    eq = eq.dropna()
    if len(eq) < 2:
        return Metrics(name, 0, 0, 0, 0, 0, 0, float(eq.iloc[-1]) if len(eq) else 0)
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    vol = float(rets.std() * (252**0.5)) if len(rets) else 0.0
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
    dd = float(((eq - eq.cummax()) / eq.cummax()).min())
    return Metrics(name, total, float(ann), sharpe, dd, 0.0, 0, float(eq.iloc[-1]))


def pick_params(
    close: pd.DataFrame,
    name: str,
    fn,
    lookback: int,
    train_start: str,
    train_end: str,
) -> tuple[int, int, float]:
    best = (5, 5, -1e9)
    for top_k, reb in PARAM_GRID:
        m, _ = run_cs_backtest(
            name,
            close,
            fn,
            lookback=lookback,
            top_k=top_k,
            rebalance=reb,
            fee=FEE_ALL_IN,
            eval_start=train_start,
            eval_end=train_end,
        )
        score = m.sharpe if m.max_dd > -0.40 else m.sharpe - 1.0
        if score > best[2]:
            best = (top_k, reb, score)
    return best[0], best[1], best[2]


def hard_gate8(m: Metrics) -> dict:
    checks = [
        {
            "gate": "oos_sharpe",
            "pass": m.sharpe > 1.0,
            "value": m.sharpe,
            "threshold": 1.0,
        },
        {
            "gate": "oos_annual_return",
            "pass": m.ann_return > 0.10,
            "value": m.ann_return,
            "threshold": 0.10,
        },
        {
            "gate": "max_drawdown",
            "pass": abs(m.max_dd) < 0.25,
            "value": m.max_dd,
            "threshold": -0.25,
        },
    ]
    return {
        "pass": all(c["pass"] for c in checks),
        "checks": checks,
    }


def load_master() -> dict[str, pd.DataFrame]:
    all_syms = sorted(set(sum(UNIVERSES.values(), [])))
    print(f"Loading master panel ({len(all_syms)} symbols)...")
    master = load_close_panel(all_syms, "2021-01-01", None)
    panels = {}
    for uname, syms in UNIVERSES.items():
        cols = [s for s in syms if s in master.columns]
        panels[uname] = master[cols]
        print(f"  universe {uname}: {len(cols)} symbols")
    return panels


def run_walk_forward(panels: dict[str, pd.DataFrame]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    oos_curves: dict[tuple[str, str], list[pd.Series]] = {}

    for uname, close in panels.items():
        for sname, (fn, lookback) in STRATEGIES.items():
            print(f"\n=== WF {uname} / {sname} ===")
            for fold, tr_s, tr_e, te_s, te_e in FOLDS:
                top_k, reb, train_score = pick_params(close, sname, fn, lookback, tr_s, tr_e)
                m, eq = run_cs_backtest(
                    sname,
                    close,
                    fn,
                    lookback=lookback,
                    top_k=top_k,
                    rebalance=reb,
                    fee=FEE_ALL_IN,
                    eval_start=te_s,
                    eval_end=te_e,
                )
                gate = hard_gate8(m)
                row = {
                    "test": "walk_forward",
                    "universe": uname,
                    "strategy": sname,
                    "fold": fold,
                    "train_start": tr_s,
                    "train_end": tr_e,
                    "test_start": te_s,
                    "test_end": te_e or "now",
                    "top_k": top_k,
                    "rebalance": reb,
                    "train_score": train_score,
                    "oos_ann": m.ann_return,
                    "oos_sharpe": m.sharpe,
                    "oos_maxdd": m.max_dd,
                    "oos_total": m.total_return,
                    "gate8_pass": gate["pass"],
                }
                rows.append(row)
                oos_curves.setdefault((uname, sname), []).append(eq)
                print(
                    f"  {fold}: k={top_k} reb={reb} "
                    f"ann={m.ann_return:.1%} S={m.sharpe:.2f} "
                    f"DD={m.max_dd:.1%} gate={'PASS' if gate['pass'] else 'FAIL'}"
                )

    # Aggregate OOS equity across folds (stitch)
    summary: dict[str, dict] = {}
    for (uname, sname), curves in oos_curves.items():
        pieces = []
        capital = 1.0
        for eq in curves:
            r = eq.pct_change().fillna(0.0)
            piece = (1.0 + r).cumprod() * capital
            capital = float(piece.iloc[-1])
            pieces.append(piece)
        stitched = pd.concat(pieces)
        stitched = stitched[~stitched.index.duplicated(keep="last")].sort_index()
        agg = metrics_from_equity(f"{uname}:{sname}", stitched)
        gate = hard_gate8(agg)
        summary[f"{uname}:{sname}"] = {
            "universe": uname,
            "strategy": sname,
            "agg_ann": agg.ann_return,
            "agg_sharpe": agg.sharpe,
            "agg_maxdd": agg.max_dd,
            "agg_total": agg.total_return,
            "gate8": gate,
            "equity": stitched,
        }
        rows.append(
            {
                "test": "wf_aggregate",
                "universe": uname,
                "strategy": sname,
                "fold": "ALL_OOS",
                "train_start": "",
                "train_end": "",
                "test_start": "2024-01-01",
                "test_end": "now",
                "top_k": "",
                "rebalance": "",
                "train_score": "",
                "oos_ann": agg.ann_return,
                "oos_sharpe": agg.sharpe,
                "oos_maxdd": agg.max_dd,
                "oos_total": agg.total_return,
                "gate8_pass": gate["pass"],
            }
        )
        print(
            f"AGG {uname}/{sname}: ann={agg.ann_return:.1%} "
            f"S={agg.sharpe:.2f} DD={agg.max_dd:.1%} "
            f"gate={'PASS' if gate['pass'] else 'FAIL'}"
        )
    return rows, summary


def run_portfolio_blend(summary: dict[str, dict], universe: str = "no_mega") -> dict:
    """60% skip5 mom + 30% trend + 10% panda on same universe OOS equity."""
    keys = {
        "rotation_skip5_mom": 0.60,
        "ma_trend_strength_60d": 0.30,
        "panda_rank_composite": 0.10,
    }
    rets = []
    for sname, w in keys.items():
        key = f"{universe}:{sname}"
        if key not in summary:
            raise KeyError(key)
        eq = summary[key]["equity"]
        rets.append(eq.pct_change().fillna(0.0) * w)
    port = sum(rets).fillna(0.0)
    # align index
    port = port.sort_index()
    eq = (1.0 + port).cumprod() * 100_000.0
    m = metrics_from_equity(f"blend60_30_10_{universe}", eq)
    gate = hard_gate8(m)
    return {
        "universe": universe,
        "weights": keys,
        "ann": m.ann_return,
        "sharpe": m.sharpe,
        "maxdd": m.max_dd,
        "total": m.total_return,
        "gate8": gate,
    }


def main() -> None:
    panels = load_master()
    rows, summary = run_walk_forward(panels)

    print("\n=== Portfolio blend 60/30/10 on debiased universes ===")
    blends = {}
    for uname in ("no_mega", "etf_only", "broad50", "original"):
        try:
            b = run_portfolio_blend(summary, uname)
            blends[uname] = b
            print(
                f"blend@{uname}: ann={b['ann']:.1%} S={b['sharpe']:.2f} "
                f"DD={b['maxdd']:.1%} gate={'PASS' if b['gate8']['pass'] else 'FAIL'}"
            )
            rows.append(
                {
                    "test": "portfolio_blend",
                    "universe": uname,
                    "strategy": "blend_60_30_10",
                    "fold": "ALL_OOS",
                    "train_start": "",
                    "train_end": "",
                    "test_start": "2024-01-01",
                    "test_end": "now",
                    "top_k": "",
                    "rebalance": "",
                    "train_score": "",
                    "oos_ann": b["ann"],
                    "oos_sharpe": b["sharpe"],
                    "oos_maxdd": b["maxdd"],
                    "oos_total": b["total"],
                    "gate8_pass": b["gate8"]["pass"],
                }
            )
        except Exception as exc:
            print(f"blend@{uname} failed: {exc}")

    # Decision table: primary focus = no_mega + etf_only aggregates
    print("\n=== Hard Gate 8 Decision ===")
    decision = {"strategies": {}, "blends": {}, "recommendation": ""}
    for key, info in summary.items():
        g = info["gate8"]
        decision["strategies"][key] = {
            "ann": info["agg_ann"],
            "sharpe": info["agg_sharpe"],
            "maxdd": info["agg_maxdd"],
            "pass": g["pass"],
            "checks": g["checks"],
        }
        mark = "PASS" if g["pass"] else "FAIL"
        print(
            f"{key:<42} {mark}  "
            f"ann={info['agg_ann']:.1%} S={info['agg_sharpe']:.2f} DD={info['agg_maxdd']:.1%}"
        )

    for uname, b in blends.items():
        decision["blends"][uname] = {
            "ann": b["ann"],
            "sharpe": b["sharpe"],
            "maxdd": b["maxdd"],
            "pass": b["gate8"]["pass"],
        }

    # Recommendation logic
    core = decision["strategies"].get("no_mega:rotation_skip5_mom", {})
    etf = decision["strategies"].get("etf_only:rotation_skip5_mom", {})
    blend_nm = decision["blends"].get("no_mega", {})
    if core.get("pass") and etf.get("pass"):
        rec = "paper_trading - skip5 mom passes Gate8 on both no_mega and etf_only"
    elif core.get("pass") or (blend_nm.get("pass") and core.get("sharpe", 0) > 1):
        rec = "paper_trading_conditional - passes debiased stock pool; monitor ETF-only weakness"
    elif core.get("sharpe", 0) > 0.8 and abs(core.get("maxdd", 1)) < 0.30:
        rec = "reoptimize - close but fails Gate8; do not go live"
    else:
        rec = "archive_or_rework - failed survivorship/WF hard gate"

    # Override: if any debiased strategy fully passes Gate8, surface it
    debiased_passes = [
        k for k, v in decision["strategies"].items()
        if v.get("pass") and k.startswith(("no_mega:", "etf_only:", "broad50:"))
    ]
    if debiased_passes:
        rec = (
            "paper_trading_conditional - Gate8 PASS on debiased universe(s): "
            + ", ".join(debiased_passes)
            + "; still need true point-in-time constituents before live"
        )
    decision["recommendation"] = rec
    print(f"\nRECOMMENDATION: {rec}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # drop equity objects before JSON
    json_safe = {
        "recommendation": rec,
        "fee_all_in_bps": FEE_ALL_IN * 10000,
        "strategies": {
            k: {kk: vv for kk, vv in v.items() if kk != "equity"}
            for k, v in (
                (k, {**decision["strategies"][k]}) for k in decision["strategies"]
            )
        },
        "blends": decision["blends"],
        "note": (
            "Universes are proxies: true point-in-time index constituents "
            "require paid membership history; etf_only + no_mega are the primary de-bias checks."
        ),
    }
    # Fix strategies dict - decision["strategies"] already has no equity
    json_safe["strategies"] = decision["strategies"]

    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    OUT_JSON.write_text(json.dumps(json_safe, indent=2, default=float), encoding="utf-8")
    print(f"\nSaved {OUT_CSV}")
    print(f"Saved {OUT_JSON}")


if __name__ == "__main__":
    main()
