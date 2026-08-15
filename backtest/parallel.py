# -*- coding: utf-8 -*-
"""Shared high-throughput process pool for backtests / strategy runs.

Targets ~80% of host CPUs by default (override with QUANT_CPU_WORKERS).
Each worker keeps BLAS/OMP at 1 thread to avoid oversubscription.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Callable, Iterable

# Pin BLAS before numpy/vectorbt import in workers
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


def cpu_budget(fraction: float = 0.80) -> int:
    """Return worker count = floor(nproc * fraction), min 2."""
    override = os.environ.get("QUANT_CPU_WORKERS", "").strip()
    if override:
        return max(1, int(override))
    n = os.cpu_count() or 4
    return max(2, int(n * fraction))


def _worker_init() -> None:
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    root = os.environ.get("PYTHONPATH", "/app")
    if root not in sys.path:
        sys.path.insert(0, root)


_POOL: ProcessPoolExecutor | None = None
_POOL_SIZE: int | None = None


def get_pool(workers: int | None = None) -> ProcessPoolExecutor:
    """Lazy singleton process pool (fork-friendly)."""
    global _POOL, _POOL_SIZE
    size = workers or cpu_budget()
    if _POOL is not None and _POOL_SIZE == size:
        return _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
    _POOL = ProcessPoolExecutor(max_workers=size, initializer=_worker_init)
    _POOL_SIZE = size
    return _POOL


def map_unordered(
    fn: Callable[..., Any],
    jobs: Iterable[tuple],
    *,
    workers: int | None = None,
) -> list[Any]:
    """Run fn(*args) for each args tuple; return results (errors as dict)."""
    job_list = list(jobs)
    if not job_list:
        return []
    # Small batches: stay in-process to avoid spawn overhead
    if len(job_list) == 1 or (workers or cpu_budget()) <= 1:
        out = []
        for args in job_list:
            try:
                out.append(fn(*args))
            except Exception as exc:
                out.append({"error": str(exc), "args": args})
        return out

    pool = get_pool(workers)
    futs = {pool.submit(fn, *args): i for i, args in enumerate(job_list)}
    results: list[Any] = [None] * len(job_list)
    for fut in as_completed(futs):
        i = futs[fut]
        try:
            results[i] = fut.result()
        except Exception as exc:
            results[i] = {"error": str(exc), "args": job_list[i]}
    return results


def backtest_symbol_job(
    symbol: str,
    code: str,
    params: dict[str, Any],
    start: str | None,
    end: str | None,
    init_cash: float,
    fees: float,
) -> dict[str, Any]:
    """Picklable worker: load OHLCV + run_signal_backtest for one symbol."""
    from backtest.runner import run_signal_backtest
    from data.loader import load_ohlcv

    ohlcv = load_ohlcv(symbol, start=start, end=end)
    result = run_signal_backtest(
        ohlcv,
        code=code,
        params=params,
        init_cash=init_cash,
        fees=fees,
    )
    result["symbol"] = symbol
    result["bars"] = len(ohlcv)
    result["start"] = str(ohlcv.index[0].date())
    result["end"] = str(ohlcv.index[-1].date())
    return result


def parallel_backtest_symbols(
    *,
    symbols: list[str],
    code: str,
    params: dict[str, Any],
    start: str | None,
    end: str | None,
    init_cash_each: float,
    fees: float,
    workers: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    jobs = [
        (sym, code, params, start, end, init_cash_each, fees) for sym in symbols
    ]
    raw = map_unordered(backtest_symbol_job, jobs, workers=workers)
    ok: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for sym, item in zip(symbols, raw):
        if isinstance(item, dict) and item.get("error") and "symbol" not in item:
            errors.append({"symbol": sym, "error": str(item["error"])})
        elif isinstance(item, dict):
            ok.append(item)
        else:
            errors.append({"symbol": sym, "error": "unknown worker result"})
    return ok, errors
