# -*- coding: utf-8 -*-
"""Hammer ~80% CPUs: parallel backtest all hot-lab strategies × their symbols."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from backtest.parallel import cpu_budget, map_unordered, backtest_symbol_job
from config.store import list_strategies, ensure_strategy_code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=0, help="max strategies (0=all hot)")
    ap.add_argument("--min-sharpe", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()
    workers = args.workers or cpu_budget()

    items = list_strategies()
    items = [
        x
        for x in items
        if float((x.get("metrics") or {}).get("sharpe") or 0) >= args.min_sharpe
    ]
    items.sort(key=lambda x: float((x.get("metrics") or {}).get("sharpe") or 0), reverse=True)
    if args.limit:
        items = items[: args.limit]

    jobs = []
    meta = []
    for s in items:
        code = ensure_strategy_code(s)
        params = dict(s.get("params") or {})
        syms = [str(x).upper() for x in (params.get("symbols") or ["SPY"])]
        for sym in syms[:4]:  # cap per-strategy symbol fanout
            jobs.append((sym, code, params, args.start, args.end, 100_000.0, 0.001))
            meta.append((s.get("id"), s.get("name"), sym))

    print(f"strategies={len(items)} jobs={len(jobs)} workers={workers}")
    t0 = time.time()
    results = map_unordered(backtest_symbol_job, jobs, workers=workers)
    elapsed = time.time() - t0
    ok = 0
    rows = []
    for (sid, name, sym), r in zip(meta, results):
        if isinstance(r, dict) and r.get("error") and "sharpe" not in r:
            continue
        ok += 1
        rows.append(
            {
                "id": sid,
                "name": name,
                "symbol": sym,
                "sharpe": r.get("sharpe"),
                "ret": r.get("total_return_pct"),
                "dd": r.get("max_drawdown_pct"),
                "trades": r.get("trades"),
            }
        )
    rows.sort(key=lambda x: float(x.get("sharpe") or 0), reverse=True)
    out = {
        "workers": workers,
        "jobs": len(jobs),
        "ok": ok,
        "elapsed_sec": round(elapsed, 2),
        "jobs_per_sec": round(ok / max(elapsed, 0.01), 2),
        "top": rows[:20],
    }
    Path("/app/data/store/batch_backtest_hot.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: out[k] for k in ("workers", "jobs", "ok", "elapsed_sec", "jobs_per_sec")}, indent=2))
    print("top5:")
    for r in rows[:5]:
        print(f"  {r['sharpe']:.2f} {r['symbol']} {r['name']}")


if __name__ == "__main__":
    main()
