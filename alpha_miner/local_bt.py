# -*- coding: utf-8 -*-
"""Local multi-process backtests (uses host Python packages from quantdesk image)."""
from __future__ import annotations

import os
from typing import Any


def _pin_threads() -> None:
    for k in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ.setdefault(k, "1")


_WORKER_OHLCV: dict[tuple, Any] = {}


def run_candidate_backtest(cand: dict[str, Any]) -> dict[str, Any]:
    """Top-level worker for ProcessPoolExecutor — returns metrics or error."""
    _pin_threads()
    out: dict[str, Any] = {
        "name": cand.get("name"),
        "source": cand.get("source"),
        "cand": cand,
    }
    try:
        from strategies.engine import validate_strategy_code
        from data.loader import load_ohlcv
        from backtest.runner import run_signal_backtest

        code = cand["code"]
        validate_strategy_code(code)
        symbols = [str(s).upper() for s in (cand.get("symbols") or ["AAPL"])][:1]
        start = os.environ.get("ALPHA_MINER_START", "2021-01-01")
        params = cand.get("params") or {}
        fees = float(os.environ.get("ALPHA_MINER_FEES", "0.0005"))
        results = []
        for sym in symbols:
            ck = (sym, start)
            if ck not in _WORKER_OHLCV:
                _WORKER_OHLCV[ck] = load_ohlcv(sym, start=start, end=None)
            ohlcv = _WORKER_OHLCV[ck]
            result = run_signal_backtest(
                ohlcv,
                code=code,
                params=params,
                init_cash=100_000.0,
                fees=fees,
            )
            result["symbol"] = sym
            results.append(result)
        primary = results[0]
        out["backtest"] = primary
        out["ok"] = True
        return out
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)[:500]
        return out
