# -*- coding: utf-8 -*-
"""Hard gates for mined candidates (paper-promote, never live).

Tighten via env (defaults are stricter than v1):
  ALPHA_GATE_MIN_SHARPE=0.70
  ALPHA_GATE_MIN_TRADES=12
  ALPHA_GATE_MIN_RETURN=0.15
  ALPHA_GATE_MAX_DD=0.30
"""
from __future__ import annotations

import os
from typing import Any


def _num(d: dict, *keys: str, default: float | None = None) -> float | None:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _gate_cfg() -> dict[str, float]:
    return {
        "min_sharpe": float(os.environ.get("ALPHA_GATE_MIN_SHARPE", "0.70")),
        "min_trades": float(os.environ.get("ALPHA_GATE_MIN_TRADES", "12")),
        "min_return": float(os.environ.get("ALPHA_GATE_MIN_RETURN", "0.15")),
        "max_dd": float(os.environ.get("ALPHA_GATE_MAX_DD", "0.30")),
    }


def extract_metrics(backtest: dict[str, Any]) -> dict[str, Any]:
    m: dict[str, Any] = dict(backtest) if isinstance(backtest, dict) else {}
    nested = backtest.get("metrics") if isinstance(backtest, dict) else None
    if isinstance(nested, dict):
        m = {**m, **nested}
    for nest in ("summary", "performance", "stats", "result"):
        if isinstance(backtest.get(nest), dict):
            m = {**m, **backtest[nest]}

    ret = _num(m, "total_return", "return", "cum_return", default=None)
    if ret is None:
        pct = _num(m, "total_return_pct", default=None)
        ret = (pct / 100.0) if pct is not None else 0.0
    elif abs(ret) > 5:
        ret = ret / 100.0

    mdd = _num(m, "max_drawdown", "maxdd", "max_dd", "max_drawdown_pct", default=0.0) or 0.0
    win = _num(m, "win_rate", "winrate", "win_rate_pct", default=None)
    if win is not None and win > 1.5:
        win = win / 100.0

    return {
        "total_return": ret,
        "sharpe": _num(m, "sharpe", "sharpe_ratio", default=0.0) or 0.0,
        "max_drawdown": mdd,
        "win_rate": win,
        "trades": _num(m, "trades", "n_trades", "trade_count", default=0.0) or 0.0,
        "cagr": _num(m, "cagr", "CAGR", default=None),
        "raw": {
            k: m.get(k)
            for k in (
                "total_return",
                "total_return_pct",
                "sharpe",
                "max_drawdown_pct",
                "trades",
                "win_rate_pct",
            )
        },
    }


def evaluate(metrics: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (passed, failed_reasons)."""
    cfg = _gate_cfg()
    fails: list[str] = []
    sharpe = metrics.get("sharpe") or 0.0
    trades = metrics.get("trades") or 0.0
    ret = metrics.get("total_return") or 0.0
    mdd = metrics.get("max_drawdown") or 0.0
    mdd_abs = abs(float(mdd))
    if mdd_abs > 1.5:
        mdd_abs = mdd_abs / 100.0

    if sharpe < cfg["min_sharpe"]:
        fails.append(f"sharpe {sharpe:.3f} < {cfg['min_sharpe']:.2f}")
    if trades < cfg["min_trades"]:
        fails.append(f"trades {trades:.0f} < {cfg['min_trades']:.0f}")
    if ret < cfg["min_return"]:
        fails.append(f"total_return {ret:.4f} < {cfg['min_return']:.2f}")
    if mdd_abs > cfg["max_dd"]:
        fails.append(f"max_drawdown {mdd_abs:.2%} > {cfg['max_dd']:.0%}")
    return (len(fails) == 0, fails)
