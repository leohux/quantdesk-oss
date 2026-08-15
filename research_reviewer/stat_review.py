# -*- coding: utf-8 -*-
"""Layer 2: Statistical Review — programmatic IS/OOS, param sensitivity."""
from __future__ import annotations

import copy
import os
from typing import Any

import pandas as pd

from alpha_miner.gates import extract_metrics


def _cfg() -> dict[str, str | float]:
    return {
        "start": os.environ.get("ALPHA_MINER_START", "2021-01-01"),
        "is_end": os.environ.get("RESEARCH_IS_END", "2023-12-31"),
        "fees": float(os.environ.get("ALPHA_MINER_FEES", "0.0005")),
        "param_shock": float(os.environ.get("RESEARCH_PARAM_SHOCK", "0.10")),
        "collapse_ratio": float(os.environ.get("RESEARCH_PARAM_COLLAPSE", "0.50")),
    }


def _run_bt(code: str, params: dict, symbol: str, start: str, end: str | None) -> dict[str, Any]:
    from data.loader import load_ohlcv
    from backtest.runner import run_signal_backtest

    ohlcv = load_ohlcv(symbol, start=start, end=end)
    return run_signal_backtest(
        ohlcv, code=code, params=params, init_cash=100_000.0, fees=_cfg()["fees"]
    )


def _rolling_sharpe_stability(equity_curve: list[dict]) -> float | None:
    if len(equity_curve) < 80:
        return None
    eq = pd.Series({pd.Timestamp(p["date"]): float(p["equity"]) for p in equity_curve}).sort_index()
    rets = eq.pct_change().dropna()
    if len(rets) < 63:
        return None
    roll = rets.rolling(63).apply(lambda x: (x.mean() / x.std()) * (252**0.5) if x.std() > 0 else 0)
    roll = roll.dropna()
    if len(roll) < 5:
        return None
    mean = float(roll.mean())
    std = float(roll.std())
    if mean == 0:
        return None
    return round(max(0.0, min(1.0, 1.0 - std / abs(mean))), 3)


def _numeric_params(params: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (params or {}).items():
        if k == "symbols":
            continue
        try:
            fv = float(v)
            if fv != 0:
                out[k] = fv
        except (TypeError, ValueError):
            continue
    return out


def run_stat_review(cand: dict[str, Any], backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run IS/OOS split, parameter sensitivity, rolling stability."""
    cfg = _cfg()
    code = cand.get("code") or ""
    params = cand.get("params") or {}
    symbols = [str(s).upper() for s in (cand.get("symbols") or ["AAPL"])][:1]
    symbol = symbols[0]
    is_end = str(cfg["is_end"])
    oos_start = (pd.Timestamp(is_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    result: dict[str, Any] = {
        "symbol": symbol,
        "is_period": f"{cfg['start']}..{is_end}",
        "oos_period": f"{oos_start}..now",
    }

    try:
        is_bt = _run_bt(code, params, symbol, str(cfg["start"]), is_end)
        oos_bt = _run_bt(code, params, symbol, oos_start, None)
        is_m = extract_metrics(is_bt)
        oos_m = extract_metrics(oos_bt)
        is_sharpe = float(is_m.get("sharpe") or 0)
        oos_sharpe = float(oos_m.get("sharpe") or 0)
        ratio = round(oos_sharpe / is_sharpe, 3) if is_sharpe > 0.01 else 0.0

        result.update(
            {
                "is_sharpe": round(is_sharpe, 3),
                "oos_sharpe": round(oos_sharpe, 3),
                "oos_is_ratio": ratio,
                "is_trades": is_m.get("trades"),
                "oos_trades": oos_m.get("trades"),
                "walk_forward": "PASS" if ratio >= 0.6 and oos_sharpe > 0 else "FAIL",
            }
        )
    except Exception as exc:
        result["error"] = str(exc)[:300]
        result["walk_forward"] = "UNKNOWN"

    # Parameter sensitivity on full sample
    base_bt = backtest or {}
    if not base_bt.get("sharpe") and code:
        try:
            base_bt = _run_bt(code, params, symbol, str(cfg["start"]), None)
        except Exception:
            base_bt = {}
    base_m = extract_metrics(base_bt) if base_bt else {}
    base_sharpe = float(base_m.get("sharpe") or 0)
    num_params = _numeric_params(params)
    shocks: list[dict[str, Any]] = []
    worst_ratio = 1.0
    shock = float(cfg["param_shock"])

    for key, val in list(num_params.items())[:5]:
        for mult in (1.0 - shock, 1.0 + shock):
            p2 = copy.deepcopy(params)
            p2[key] = type(val)(val * mult) if isinstance(val, int) else val * mult
            try:
                bt = _run_bt(code, p2, symbol, str(cfg["start"]), None)
                m = extract_metrics(bt)
                s = float(m.get("sharpe") or 0)
                ratio = s / base_sharpe if base_sharpe > 0.01 else 0
                worst_ratio = min(worst_ratio, ratio)
                shocks.append({"param": key, "mult": round(mult, 2), "sharpe": round(s, 3)})
            except Exception as exc:
                shocks.append({"param": key, "mult": round(mult, 2), "error": str(exc)[:80]})

    collapse = float(cfg["collapse_ratio"])
    if base_sharpe > 0 and worst_ratio < collapse:
        stability = "BAD"
    elif worst_ratio < 0.75:
        stability = "OK"
    else:
        stability = "GOOD"

    result["parameter_stability"] = stability
    result["param_worst_ratio"] = round(worst_ratio, 3)
    result["param_shocks"] = shocks[:6]

    eq = (backtest or {}).get("equity_curve") or base_bt.get("equity_curve")
    regime = _rolling_sharpe_stability(eq or [])
    if regime is not None:
        result["regime_score"] = regime

    result["stat_score"] = _stat_score(result)
    return result


def _stat_score(stat: dict[str, Any]) -> float:
    score = 50.0
    ratio = stat.get("oos_is_ratio")
    if ratio is not None:
        score += min(25, float(ratio) * 20)
    if stat.get("walk_forward") == "PASS":
        score += 10
    elif stat.get("walk_forward") == "FAIL":
        score -= 15
    stability = stat.get("parameter_stability")
    if stability == "GOOD":
        score += 10
    elif stability == "OK":
        score += 5
    elif stability == "BAD":
        score -= 20
    regime = stat.get("regime_score")
    if regime is not None:
        score += float(regime) * 10
    return round(max(0.0, min(100.0, score)), 1)
