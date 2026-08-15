# -*- coding: utf-8 -*-
"""Batch review existing strategies from strategies.json."""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .store import list_reviewed_ids
from .version import ENGINE_VERSION

STRATEGIES_FILE = Path(os.environ.get("STRATEGIES_FILE", "/app/data/store/strategies.json"))


def _default_workers() -> int:
    env = os.environ.get("RESEARCH_REVIEW_WORKERS", "").strip()
    if env:
        return max(1, int(env))
    cpu = os.cpu_count() or 4
    # ~80% of cores by default
    return max(1, int(cpu * 0.8))


def _load_mined(limit: int | None, skip_reviewed: bool, min_sharpe: float | None) -> list[dict]:
    items = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
    mined = [
        x
        for x in items
        if str(x.get("id", "")).startswith(("alpha-", "mimo-"))
    ]
    mined.sort(
        key=lambda x: float((x.get("metrics") or {}).get("sharpe") or 0),
        reverse=True,
    )

    if min_sharpe is not None:
        mined = [
            x
            for x in mined
            if float((x.get("metrics") or {}).get("sharpe") or 0) >= min_sharpe
        ]

    if skip_reviewed:
        done = set(list_reviewed_ids())
        mined = [x for x in mined if x["id"] not in done]

    if limit is not None:
        mined = mined[:limit]
    return mined


def _review_one_fast(item: dict[str, Any]) -> dict[str, Any]:
    """Worker: rule-only review from list metadata (no code I/O)."""
    from research_reviewer.reviewer import run_review

    sid = item["id"]
    try:
        cand = {
            "name": item.get("name"),
            "type": item.get("type"),
            "description": item.get("description"),
            "params": item.get("params") or {},
            "symbols": (item.get("params") or {}).get("symbols") or ["AAPL"],
            "code": "",
            "source": (item.get("metrics") or {}).get("source", "batch"),
        }
        metrics = {
            "sharpe": (item.get("metrics") or {}).get("sharpe"),
            "trades": (item.get("metrics") or {}).get("trades"),
            "total_return": (item.get("metrics") or {}).get("total_return"),
            "max_drawdown": (item.get("metrics") or {}).get("max_drawdown")
            or (item.get("metrics") or {}).get("max_drawdown_pct"),
            "win_rate": (item.get("metrics") or {}).get("win_rate"),
        }
        review = run_review(
            cand,
            metrics,
            strategy_id=sid,
            run_stat=False,
            run_llm=False,
        )
        return {
            "id": sid,
            "ok": True,
            "pass": bool(review.get("research_gate_pass")),
            "score": review.get("research_score"),
            "codes": review.get("reason_codes") or [],
        }
    except Exception as exc:
        return {"id": sid, "ok": False, "error": str(exc)[:300]}


def _review_one_full(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker: optional stat/llm review (loads code)."""
    from config.store import ensure_strategy_code, get_strategy
    from research_reviewer.reviewer import run_review

    sid = payload["id"]
    run_stat = payload.get("run_stat", True)
    run_llm = payload.get("run_llm", False)
    try:
        detail = get_strategy(sid)
        code = detail.get("code") or ensure_strategy_code(detail)
        cand = {
            "name": detail.get("name"),
            "type": detail.get("type"),
            "description": detail.get("description"),
            "params": detail.get("params") or {},
            "symbols": (detail.get("params") or {}).get("symbols") or ["AAPL"],
            "code": code,
            "source": (detail.get("metrics") or {}).get("source", "batch"),
        }
        metrics = {
            "sharpe": (detail.get("metrics") or {}).get("sharpe"),
            "trades": (detail.get("metrics") or {}).get("trades"),
            "total_return": (detail.get("metrics") or {}).get("total_return"),
            "max_drawdown": (detail.get("metrics") or {}).get("max_drawdown")
            or (detail.get("metrics") or {}).get("max_drawdown_pct"),
            "win_rate": (detail.get("metrics") or {}).get("win_rate"),
        }
        review = run_review(
            cand,
            metrics,
            strategy_id=sid,
            run_stat=run_stat,
            run_llm=run_llm,
        )
        return {
            "id": sid,
            "ok": True,
            "pass": bool(review.get("research_gate_pass")),
            "score": review.get("research_score"),
            "codes": review.get("reason_codes") or [],
        }
    except Exception as exc:
        return {"id": sid, "ok": False, "error": str(exc)[:300]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Batch evidence-based research review")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--all",
        action="store_true",
        help="Review all mined strategies (ignores --limit)",
    )
    p.add_argument("--skip-reviewed", action="store_true", default=False)
    p.add_argument("--force", action="store_true", help="Re-review even if already reviewed")
    p.add_argument("--no-stat", action="store_true")
    p.add_argument("--llm", action="store_true", help="Enable LLM layer")
    p.add_argument("--llm-top", type=int, default=10, help="LLM only for top N by order")
    p.add_argument("--strategy-id", type=str, default="", help="Review single strategy")
    p.add_argument(
        "--min-sharpe",
        type=float,
        default=None,
        help="Only review strategies with metrics.sharpe >= this",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel workers (default ~80%% of CPU cores)",
    )
    args = p.parse_args(argv)

    run_stat = not args.no_stat
    skip = args.skip_reviewed and not args.force
    workers = args.workers or _default_workers()

    if args.strategy_id:
        from config.store import ensure_strategy_code, get_strategy
        from .reviewer import run_review

        detail = get_strategy(args.strategy_id)
        code = detail.get("code") or ensure_strategy_code(detail)
        cand = {
            "name": detail.get("name"),
            "type": detail.get("type"),
            "description": detail.get("description"),
            "params": detail.get("params") or {},
            "symbols": (detail.get("params") or {}).get("symbols") or ["AAPL"],
            "code": code,
            "source": (detail.get("metrics") or {}).get("source", "batch"),
        }
        metrics = {
            "sharpe": (detail.get("metrics") or {}).get("sharpe"),
            "trades": (detail.get("metrics") or {}).get("trades"),
            "total_return": (detail.get("metrics") or {}).get("total_return"),
            "max_drawdown": (detail.get("metrics") or {}).get("max_drawdown")
            or (detail.get("metrics") or {}).get("max_drawdown_pct"),
            "win_rate": (detail.get("metrics") or {}).get("win_rate"),
        }
        review = run_review(
            cand,
            metrics,
            strategy_id=args.strategy_id,
            run_stat=run_stat,
            run_llm=args.llm,
        )
        print(
            f"done id={args.strategy_id} pass={review['research_gate_pass']} "
            f"score={review['research_score']} engine=v{ENGINE_VERSION}",
            flush=True,
        )
        return 0

    limit = None if args.all else args.limit
    targets = _load_mined(limit, skip, args.min_sharpe)
    total = len(targets)
    print(
        f"reviewing {total} strategies engine=v{ENGINE_VERSION} "
        f"stat={run_stat} llm={args.llm} workers={workers}",
        flush=True,
    )
    if total == 0:
        print("nothing to do", flush=True)
        return 0

    passed = rejected = errors = 0
    done_n = 0

    # Fast path: rule-only parallel
    if not run_stat and not args.llm:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_review_one_fast, item): item["id"] for item in targets}
            for fut in as_completed(futs):
                done_n += 1
                try:
                    r = fut.result()
                except Exception as exc:
                    errors += 1
                    print(f"[{done_n}/{total}] ERROR worker: {exc}", flush=True)
                    continue
                if not r.get("ok"):
                    errors += 1
                    print(f"[{done_n}/{total}] ERROR {r.get('id')}: {r.get('error')}", flush=True)
                elif r.get("pass"):
                    passed += 1
                else:
                    rejected += 1
                if done_n % 200 == 0 or done_n == total:
                    print(
                        f"progress {done_n}/{total} passed={passed} "
                        f"rejected={rejected} errors={errors}",
                        flush=True,
                    )
    else:
        # Stat/LLM: fewer workers (IO + CPU heavy)
        w = min(workers, max(2, (os.cpu_count() or 4) // 4))
        payloads = []
        for i, item in enumerate(targets):
            payloads.append(
                {
                    "id": item["id"],
                    "run_stat": run_stat,
                    "run_llm": args.llm and i < args.llm_top,
                }
            )
        print(f"full-review workers={w}", flush=True)
        with ProcessPoolExecutor(max_workers=w) as pool:
            futs = {pool.submit(_review_one_full, p): p["id"] for p in payloads}
            for fut in as_completed(futs):
                done_n += 1
                try:
                    r = fut.result()
                except Exception as exc:
                    errors += 1
                    print(f"[{done_n}/{total}] ERROR worker: {exc}", flush=True)
                    continue
                if not r.get("ok"):
                    errors += 1
                elif r.get("pass"):
                    passed += 1
                else:
                    rejected += 1
                if done_n % 50 == 0 or done_n == total:
                    print(
                        f"progress {done_n}/{total} passed={passed} "
                        f"rejected={rejected} errors={errors}",
                        flush=True,
                    )

    print(
        f"done passed={passed} rejected={rejected} errors={errors} "
        f"engine=v{ENGINE_VERSION}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
