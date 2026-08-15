#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate frozen SL/TP candidates on the untouched holdout window.

Reads /app/data/store/rr_candidates.json (written by rr_grid_surge.py) and
scores each frozen combo on HOLDOUT_START..now only. Nothing here feeds back
into parameter selection.
"""
from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np

ROOT = Path("/app")
sys.path.insert(0, str(ROOT))

from backtest.runner import run_signal_backtest
from config.store import ensure_strategy_code, get_strategy
from data.loader import load_ohlcv

HOLDOUT_START = os.environ.get("RR_HOLDOUT_START", "2025-07-01")
LOAD_START = "2024-06-01"


def _as_ratio(value) -> float:
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return v / 100.0 if v > 1.0 else v


def _finite(value, default=float("nan")) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _metrics(res: dict) -> dict:
    return {
        "sharpe": _finite(res.get("sharpe"), 0.0),
        "total_return": _finite(res.get("total_return"), 0.0),
        "max_drawdown": _as_ratio(res.get("max_drawdown_pct")),
        "trades": _finite(res.get("trades"), 0.0),
        "win_rate": _as_ratio(res.get("win_rate_pct")),
        "profit_factor": min(_finite(res.get("profit_factor"), float("nan")), 10.0),
    }


def _fmt(v, spec="{:.3f}") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if f != f else spec.format(f)


def main():
    store = Path("/app/data/store")
    cands = json.loads((store / "rr_candidates.json").read_text(encoding="utf-8"))

    out_rows = []
    for entry in cands:
        sid = entry["strategy_id"]
        s = get_strategy(sid)
        code = ensure_strategy_code(s)
        base = dict(s.get("params") or {})
        if "Intraday" in entry.get("label", ""):
            base["_disable_engine_stops"] = True
        symbols = [str(x).upper() for x in (base.get("symbols") or [])]

        data = {}
        for sym in symbols:
            try:
                df = load_ohlcv(sym, start=LOAD_START)
                data[sym] = df.loc[HOLDOUT_START:]
            except Exception as e:
                print(f"  skip {sym}: {e}", flush=True)

        print(f"\n=== {entry['label']} holdout {HOLDOUT_START}..now ===", flush=True)
        for combo in entry["combos"]:
            params = deepcopy(base)
            params["stop_loss"] = float(combo["stop_loss"])
            params["take_profit"] = float(combo["take_profit"])
            per = []
            for sym, df in data.items():
                if df is None or len(df) < 40:
                    continue
                per.append(_metrics(run_signal_backtest(df, code, params)))
            if not per:
                continue

            def avg(key):
                vals = [p[key] for p in per if p[key] == p[key]]
                return float(np.mean(vals)) if vals else float("nan")

            row = {
                "label": entry["label"],
                "stop_loss": params["stop_loss"],
                "take_profit": params["take_profit"],
                "rr": abs(params["take_profit"] / params["stop_loss"]),
                "baseline": bool(combo.get("baseline")),
                "sharpe": avg("sharpe"),
                "total_return": avg("total_return"),
                "max_drawdown": avg("max_drawdown"),
                "trades": float(sum(p["trades"] for p in per)),
                "profit_factor": avg("profit_factor"),
                "win_rate": avg("win_rate"),
                "pos_frac": float(np.mean([1.0 if p["total_return"] > 0 else 0.0 for p in per])),
            }
            out_rows.append(row)
            print(
                f"  SL={row['stop_loss']:.0%} TP={row['take_profit']:.0%} "
                f"RR={row['rr']:.2f} sharpe={_fmt(row['sharpe'])} "
                f"ret={_fmt(row['total_return'])} dd={_fmt(row['max_drawdown'], '{:.1%}')} "
                f"trades={row['trades']:.0f}"
                + ("  <-- baseline" if row["baseline"] else ""),
                flush=True,
            )

    (store / "rr_holdout_surge.json").write_text(json.dumps(out_rows, indent=2), encoding="utf-8")

    lines = [
        "# 追涨策略 真正 holdout 验证",
        "",
        f"- Holdout: {HOLDOUT_START}..now (未参与任何搜参/排名)",
        "",
        "| 策略 | SL | TP | 盈亏比 | Sharpe | Return | MaxDD | Trades | PF | WinRate | 正收益占比 | 备注 |",
        "|:--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--|",
    ]
    for r in out_rows:
        lines.append(
            f"| {r['label']} | {r['stop_loss']:.0%} | {r['take_profit']:.0%} | {r['rr']:.2f} | "
            f"{_fmt(r['sharpe'])} | {_fmt(r['total_return'])} | {_fmt(r['max_drawdown'], '{:.1%}')} | "
            f"{r['trades']:.0f} | {_fmt(r['profit_factor'], '{:.2f}')} | "
            f"{_fmt(r['win_rate'], '{:.1%}')} | {_fmt(r['pos_frac'], '{:.0%}')} | "
            f"{'baseline' if r['baseline'] else 'candidate'} |"
        )
    out_md = store / "rr_holdout_surge.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print("\nWrote", out_md, flush=True)
    print(out_md.read_text(), flush=True)


if __name__ == "__main__":
    main()
