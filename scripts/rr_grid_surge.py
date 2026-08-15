#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grid-search 盈亏比 (TP / |SL|) on chase/surge — parallel.

Splits:
  TRAIN  START..VALID_START      -> parameter search happens here
  VALID  VALID_START..HOLDOUT_START -> candidate ranking
  HOLDOUT_START..now             -> untouched here, see rr_holdout_surge.py
"""
from __future__ import annotations

import itertools
import json
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path

import numpy as np

ROOT = Path("/app")
sys.path.insert(0, str(ROOT))

from backtest.runner import run_signal_backtest
from config.store import ensure_strategy_code, get_strategy
from data.loader import load_ohlcv

STOPS = [-0.04, -0.05, -0.06, -0.08, -0.10, -0.12]
TPS = [0.06, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]
START = "2022-01-01"
VALID_START = "2024-01-01"
HOLDOUT_START = "2025-07-01"
WORKERS = int(os.environ.get("RR_GRID_WORKERS", "47"))  # ~65% of 72 cores

MAX_DD_GATE = float(os.environ.get("RR_GRID_MAX_DD", "0.30"))
MIN_TRADES_GATE = int(os.environ.get("RR_GRID_MIN_TRADES", "50"))

_DATA: dict = {}


def _init_worker(data_path: str) -> None:
    global _DATA
    with open(data_path, "rb") as fh:
        _DATA = pickle.load(fh)


def resolve_surge_id() -> str:
    from config.store import list_strategies

    for s in list_strategies():
        if s.get("enabled") and "Cursor-Surge" in (s.get("name") or ""):
            return s["id"]
    for s in list_strategies():
        if s.get("name") == "Cursor-Surge-NVDA-052828-63859c":
            return s["id"]
    raise KeyError("Cursor-Surge not found")


def _as_ratio(value) -> float:
    """Normalize a drawdown/return that may be pct (27.3) or ratio (0.273)."""
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
        # engine exposes max_drawdown_pct; keep everything as a ratio internally
        "max_drawdown": _as_ratio(res.get("max_drawdown_pct")),
        "trades": _finite(res.get("trades"), 0.0),
        "win_rate": _as_ratio(res.get("win_rate_pct")),
        # runaway PF (no losing trade) is capped so averages stay meaningful
        "profit_factor": min(_finite(res.get("profit_factor"), float("nan")), 10.0),
    }


def _run_one(payload: dict) -> dict:
    code = payload["code"]
    sl = payload["sl"]
    tp = payload["tp"]

    params = deepcopy(payload["base"])
    params["stop_loss"] = float(sl)
    params["take_profit"] = float(tp)
    if params.get("_grid_disable_engine_stops"):
        params["_disable_engine_stops"] = True
    rr = abs(tp / sl) if sl else None

    rows = []
    for sym, df in _DATA.items():
        if df is None or len(df) < 80:
            continue
        train_df = df.loc[:VALID_START]
        valid_df = df.loc[VALID_START:HOLDOUT_START]
        train = _metrics(run_signal_backtest(train_df, code, params)) if len(train_df) >= 80 else None
        valid = _metrics(run_signal_backtest(valid_df, code, params)) if len(valid_df) >= 40 else None
        rows.append({"symbol": sym, "train": train, "valid": valid})

    def avg(split: str, key: str) -> float:
        vals = [r[split][key] for r in rows if r.get(split) and r[split][key] == r[split][key]]
        return float(np.mean(vals)) if vals else float("nan")

    def worst(split: str, key: str) -> float:
        vals = [r[split][key] for r in rows if r.get(split) and r[split][key] == r[split][key]]
        return float(np.max(vals)) if vals else float("nan")

    def total(split: str, key: str) -> float:
        return float(sum((r.get(split) or {}).get(key) or 0 for r in rows))

    valid_rets = [r["valid"]["total_return"] for r in rows if r.get("valid")]
    pos_frac = float(np.mean([1.0 if x > 0 else 0.0 for x in valid_rets])) if valid_rets else float("nan")

    valid_sharpe = avg("valid", "sharpe")
    valid_dd = avg("valid", "max_drawdown")
    valid_trades = total("valid", "trades")

    # Score = Sharpe x (1 - MaxDD) x stability, per user's ranking rule.
    score = float("nan")
    if valid_sharpe == valid_sharpe and valid_dd == valid_dd and pos_frac == pos_frac:
        score = valid_sharpe * max(0.0, 1.0 - valid_dd) * pos_frac

    gates = []
    if not (valid_dd == valid_dd and valid_dd < MAX_DD_GATE):
        gates.append(f"DD>={MAX_DD_GATE:.0%}")
    if valid_trades < MIN_TRADES_GATE:
        gates.append(f"trades<{MIN_TRADES_GATE}")

    return {
        "stop_loss": sl,
        "take_profit": tp,
        "rr": round(rr, 3) if rr else None,
        "n_symbols": len(rows),
        "score": score,
        "pass_gates": not gates,
        "gate_fail": gates,
        "train_sharpe": avg("train", "sharpe"),
        "train_return": avg("train", "total_return"),
        "train_dd": avg("train", "max_drawdown"),
        "train_trades": total("train", "trades"),
        "valid_sharpe": valid_sharpe,
        "valid_return": avg("valid", "total_return"),
        "valid_dd": valid_dd,
        "valid_dd_worst": worst("valid", "max_drawdown"),
        "valid_trades": valid_trades,
        "valid_pf": avg("valid", "profit_factor"),
        "valid_win_rate": avg("valid", "win_rate"),
        "valid_pos_frac": pos_frac,
        "per_symbol": rows,
    }


def run_for_strategy(sid: str, label: str, disable_engine_stops: bool = False) -> dict:
    s = get_strategy(sid)
    code = ensure_strategy_code(s)
    base = dict(s.get("params") or {})
    if disable_engine_stops:
        base["_grid_disable_engine_stops"] = True
    symbols = [str(x).upper() for x in (base.get("symbols") or [])]
    print(
        f"\n=== {label} ({sid}) symbols={len(symbols)} "
        f"baseline SL={base.get('stop_loss')} TP={base.get('take_profit')} "
        f"workers={WORKERS} ===",
        flush=True,
    )

    data_by_sym = {}
    for sym in symbols:
        try:
            df = load_ohlcv(sym, start=START)
            data_by_sym[sym] = df
            print(f"  loaded {sym}: {len(df)}", flush=True)
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)

    data_path = f"/tmp/rr_grid_data_{sid}.pkl"
    with open(data_path, "wb") as fh:
        pickle.dump(data_by_sym, fh, protocol=pickle.HIGHEST_PROTOCOL)

    grid = list(itertools.product(STOPS, TPS))
    payloads = [{"code": code, "base": base, "sl": sl, "tp": tp} for sl, tp in grid]
    results: list[dict] = []
    done = 0
    with ProcessPoolExecutor(
        max_workers=WORKERS, initializer=_init_worker, initargs=(data_path,)
    ) as ex:
        futs = {ex.submit(_run_one, p): p for p in payloads}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if done == 1 or done % 10 == 0 or done == len(payloads):
                print(
                    f"  [{done}/{len(payloads)}] SL={r['stop_loss']} TP={r['take_profit']} "
                    f"RR={r['rr']} score={r['score']:.3f} "
                    f"valid_sharpe={r['valid_sharpe']:.3f} valid_dd={r['valid_dd']:.3f}",
                    flush=True,
                )

    def sort_key(x: dict):
        sc = x["score"] if x["score"] == x["score"] else -999
        sh = x["valid_sharpe"] if x["valid_sharpe"] == x["valid_sharpe"] else -999
        return (1 if x["pass_gates"] else 0, sc, sh)

    results.sort(key=sort_key, reverse=True)
    baseline = [
        r
        for r in results
        if abs(r["stop_loss"] - float(base.get("stop_loss", -0.08))) < 1e-9
        and abs(r["take_profit"] - float(base.get("take_profit", 0.15))) < 1e-9
    ]
    return {
        "strategy_id": sid,
        "name": s.get("name"),
        "label": label,
        "baseline_params": {
            "stop_loss": base.get("stop_loss"),
            "take_profit": base.get("take_profit"),
        },
        "baseline_result": baseline[0] if baseline else None,
        "top": results[:15],
        "all": results,
    }


def _fmt(v, spec="{:.3f}") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if f != f else spec.format(f)


def main():
    reports = []
    reports.append(run_for_strategy(resolve_surge_id(), "Paper Cursor-Surge (追涨)"))
    try:
        reports.append(
            run_for_strategy(
                "strategy-046bfa", "Intraday Morning-Surge (追涨)", disable_engine_stops=True
            )
        )
    except Exception as e:
        print("intraday skip", e)

    store = Path("/app/data/store")
    out_json = store / "rr_grid_surge.json"
    out_md = store / "rr_grid_surge.md"
    out_cand = store / "rr_candidates.json"

    slim = []
    for rep in reports:
        slim.append(
            {
                **{
                    k: rep[k]
                    for k in ("strategy_id", "name", "label", "baseline_params", "baseline_result")
                },
                "top": [{k: v for k, v in r.items() if k != "per_symbol"} for r in rep["top"]],
                "all": [{k: v for k, v in r.items() if k != "per_symbol"} for r in rep["all"]],
            }
        )
    out_json.write_text(json.dumps(slim, indent=2), encoding="utf-8")

    lines = [
        "# 追涨策略盈亏比网格 (TP / |SL|) — v2",
        "",
        f"- TRAIN (搜参): {START}..{VALID_START}",
        f"- VALID (排名): {VALID_START}..{HOLDOUT_START}",
        f"- HOLDOUT (冻结未用): {HOLDOUT_START}..now",
        f"- Gates: MaxDD < {MAX_DD_GATE:.0%}, trades >= {MIN_TRADES_GATE}",
        "- Score = Sharpe x (1 - MaxDD) x 正收益标的占比",
        f"- Workers: {WORKERS}",
        "",
    ]
    for rep in slim:
        lines.append(f"## {rep['label']}")
        lines.append(f"- id: `{rep['strategy_id']}`")
        lines.append(
            f"- baseline: SL={rep['baseline_params'].get('stop_loss')} "
            f"TP={rep['baseline_params'].get('take_profit')}"
        )
        br = rep.get("baseline_result")
        if br:
            lines.append(
                f"- baseline VALID Sharpe={_fmt(br['valid_sharpe'])} "
                f"Ret={_fmt(br['valid_return'])} MaxDD={_fmt(br['valid_dd'], '{:.1%}')} "
                f"Trades={_fmt(br['valid_trades'], '{:.0f}')} PF={_fmt(br['valid_pf'], '{:.2f}')}"
            )
        lines.append("")
        lines.append(
            "| Rank | SL | TP | 盈亏比 | Score | Valid Sharpe | Valid Ret | Valid MaxDD | "
            "Worst DD | Trades | PF | WinRate | 正收益占比 | Train Sharpe | Gate |"
        )
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--|")
        for i, r in enumerate(rep["top"], 1):
            gate = "PASS" if r["pass_gates"] else ",".join(r["gate_fail"])
            lines.append(
                f"| {i} | {r['stop_loss']:.0%} | {r['take_profit']:.0%} | {_fmt(r['rr'], '{:.2f}')} | "
                f"{_fmt(r['score'])} | {_fmt(r['valid_sharpe'])} | {_fmt(r['valid_return'])} | "
                f"{_fmt(r['valid_dd'], '{:.1%}')} | {_fmt(r['valid_dd_worst'], '{:.1%}')} | "
                f"{_fmt(r['valid_trades'], '{:.0f}')} | {_fmt(r['valid_pf'], '{:.2f}')} | "
                f"{_fmt(r['valid_win_rate'], '{:.1%}')} | {_fmt(r['valid_pos_frac'], '{:.0%}')} | "
                f"{_fmt(r['train_sharpe'])} | {gate} |"
            )
        lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")

    # Freeze candidates for the holdout step: top gate-passers + baseline.
    candidates = []
    for rep in slim:
        picks = [r for r in rep["all"] if r["pass_gates"]][:3]
        base_p = rep["baseline_params"]
        combos = [{"stop_loss": r["stop_loss"], "take_profit": r["take_profit"]} for r in picks]
        combos.append(
            {
                "stop_loss": float(base_p.get("stop_loss") or -0.08),
                "take_profit": float(base_p.get("take_profit") or 0.15),
                "baseline": True,
            }
        )
        candidates.append(
            {
                "strategy_id": rep["strategy_id"],
                "label": rep["label"],
                "combos": combos,
            }
        )
    out_cand.write_text(json.dumps(candidates, indent=2), encoding="utf-8")

    print("\nWrote", out_json, out_md, out_cand, flush=True)
    print(out_md.read_text(), flush=True)


if __name__ == "__main__":
    main()
