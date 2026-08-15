# -*- coding: utf-8 -*-
"""IS / Valid / Holdout + cost sensitivity for strategy-046bfa.

Uses store strategy_code (includes false-breakout filters + day_crash +
max_hold). Equal-weight average of per-symbol run_signal_backtest metrics.

Splits (aligned with rr_grid_surge):
  TRAIN   2022-01-01 .. 2023-12-31
  VALID   2024-01-01 .. 2025-06-30
  HOLDOUT 2025-07-01 .. now

Cost tiers: 0 / 5 / 10 / 20 bps one-way (fees=bps/10000).

Usage:
  python /app/scripts/surge_046bfa_is_oos.py
  python /app/scripts/surge_046bfa_is_oos.py --max-symbols 20
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/app")

from backtest.runner import run_signal_backtest
from config.store import ensure_strategy_code, get_strategy
from data.loader import load_ohlcv

SID = "strategy-046bfa"
START = "2022-01-01"
VALID_START = "2024-01-01"
HOLDOUT_START = "2025-07-01"
COST_BPS = (0, 5, 10, 20)
OUT = Path("/app/data/research/surge_046bfa_is_oos.json")
MIN_BARS = {"train": 80, "valid": 40, "holdout": 40}


def _finite(v, default=float("nan")) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


def _dd_ratio(res: dict) -> float:
    v = abs(_finite(res.get("max_drawdown_pct"), 0.0))
    return v / 100.0 if v > 1.0 else v


def _metrics(res: dict) -> dict:
    return {
        "sharpe": _finite(res.get("sharpe"), 0.0),
        "total_return": _finite(res.get("total_return"), 0.0),
        "max_drawdown": _dd_ratio(res),
        "trades": int(_finite(res.get("trades"), 0)),
        "win_rate": _finite(res.get("win_rate_pct"), 0.0) / 100.0
        if _finite(res.get("win_rate_pct"), 0.0) > 1
        else _finite(res.get("win_rate_pct"), 0.0),
        "profit_factor": min(_finite(res.get("profit_factor"), float("nan")), 10.0),
    }


def _slice(df, start: str | None, end: str | None):
    out = df
    if start:
        out = out.loc[start:]
    if end:
        out = out.loc[:end]
    return out


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"n_symbols": 0}
    keys = ["sharpe", "total_return", "max_drawdown", "win_rate", "profit_factor"]
    out = {"n_symbols": len(rows), "trades": int(sum(r["trades"] for r in rows))}
    for k in keys:
        vals = [r[k] for r in rows if r.get(k) == r.get(k)]
        out[f"avg_{k}"] = float(np.mean(vals)) if vals else float("nan")
        if k == "max_drawdown" and vals:
            out["worst_max_drawdown"] = float(np.max(vals))
    rets = [r["total_return"] for r in rows if r.get("total_return") == r.get("total_return")]
    out["pos_frac"] = float(np.mean([1.0 if x > 0 else 0.0 for x in rets])) if rets else float("nan")
    return out


def run_split(code: str, params: dict, data: dict, start: str | None, end: str | None, fees: float, min_bars: int):
    rows = []
    for sym, df in data.items():
        sub = _slice(df, start, end)
        if sub is None or len(sub) < min_bars:
            continue
        try:
            res = run_signal_backtest(sub, code, params, fees=fees)
            m = _metrics(res)
            m["symbol"] = sym
            rows.append(m)
        except Exception as exc:
            rows.append({"symbol": sym, "error": str(exc)[:160], "trades": 0, "sharpe": float("nan")})
    return aggregate(rows), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0, help="0 = all pool symbols")
    ap.add_argument("--skip-spcx", action="store_true", default=True)
    args = ap.parse_args()

    st = get_strategy(SID)
    code = ensure_strategy_code(st)
    params = dict(st.get("params") or {})
    symbols = [str(s).upper() for s in (params.get("symbols") or [])]
    if args.skip_spcx:
        symbols = [s for s in symbols if s != "SPCX"]
    if args.max_symbols and args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]

    print(f"strategy={st.get('name')} id={SID} symbols={len(symbols)}", flush=True)
    print(
        f"params SL={params.get('stop_loss')} TP={params.get('take_profit')} "
        f"surge=[{params.get('buy_surge')},{params.get('buy_cap')}) "
        f"fb={params.get('filter_false_breakout')} hold={params.get('max_hold_days')}",
        flush=True,
    )

    data = {}
    for sym in symbols:
        try:
            df = load_ohlcv(sym, start=START)
            data[sym] = df
            print(f"  loaded {sym}: {len(df)}", flush=True)
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)

    report = {
        "strategy_id": SID,
        "name": st.get("name"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "splits": {
            "train": f"{START}..{VALID_START}",
            "valid": f"{VALID_START}..{HOLDOUT_START}",
            "holdout": f"{HOLDOUT_START}..now",
        },
        "cost_bps": list(COST_BPS),
        "n_symbols_loaded": len(data),
        "params_snapshot": {
            k: params.get(k)
            for k in (
                "buy_surge",
                "buy_cap",
                "stop_loss",
                "take_profit",
                "trend_ma",
                "max_hold_days",
                "day_crash",
                "filter_false_breakout",
                "min_vol_ratio",
                "breakout_lookback",
                "max_prior_ret",
                "min_close_loc",
            )
        },
        "results": {},
        "gates": {},
    }

    split_specs = [
        ("train", START, VALID_START, MIN_BARS["train"]),
        ("valid", VALID_START, HOLDOUT_START, MIN_BARS["valid"]),
        ("holdout", HOLDOUT_START, None, MIN_BARS["holdout"]),
    ]

    for bps in COST_BPS:
        fees = bps / 10_000.0
        report["results"][str(bps)] = {}
        print(f"\n=== cost {bps}bp (fees={fees}) ===", flush=True)
        for name, start, end, min_bars in split_specs:
            agg, _rows = run_split(code, params, data, start, end, fees, min_bars)
            report["results"][str(bps)][name] = agg
            print(
                f"  {name:8s} n={agg.get('n_symbols')} trades={agg.get('trades')} "
                f"sharpe={agg.get('avg_sharpe', float('nan')):.3f} "
                f"ret={agg.get('avg_total_return', float('nan')):.3f} "
                f"dd={agg.get('avg_max_drawdown', float('nan')):.3f} "
                f"pos={agg.get('pos_frac', float('nan')):.2f}",
                flush=True,
            )

    # Simple gates at 10bp (MASTER / registry convention)
    v10 = report["results"]["10"].get("valid") or {}
    h10 = report["results"]["10"].get("holdout") or {}
    fails = []
    if (v10.get("trades") or 0) < 50:
        fails.append(f"valid_trades={v10.get('trades')}<50")
    if (v10.get("avg_sharpe") or 0) < 0.5:
        fails.append(f"valid_sharpe={v10.get('avg_sharpe'):.2f}<0.5")
    if (v10.get("avg_max_drawdown") or 1) > 0.30:
        fails.append(f"valid_dd={v10.get('avg_max_drawdown'):.2f}>0.30")
    # cost robustness: holdout sharpe at 20bp vs 5bp
    h5 = report["results"]["5"].get("holdout") or {}
    h20 = report["results"]["20"].get("holdout") or {}
    if (h5.get("avg_sharpe") or 0) > 0 and (h20.get("avg_sharpe") or 0) < 0:
        fails.append("holdout_sharpe flips negative 5bp→20bp")
    report["gates"] = {
        "focus_cost_bps": 10,
        "pass": not fails,
        "fail_reasons": fails,
        "note": "Equal-weight single-name avg; not portfolio-constrained. Diagnostic only.",
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nWrote {OUT}")
    print("GATES", report["gates"])
    return 0 if report["gates"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
