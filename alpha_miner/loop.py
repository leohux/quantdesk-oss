# -*- coding: utf-8 -*-
"""Continuous A+B alpha mining — keep 20 local workers hot.

Bottleneck explanation:
  Local backtests finish in a few seconds; waiting on MiMo API previously
  idled all workers → dashboard CPU ~0% most of the time.
  Fix: A-template batches run continuously; MiMo proposals are async.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed, Future
from datetime import datetime, timezone
from pathlib import Path

from .api_client import QuantDeskClient
from .gates import evaluate, extract_metrics
from .local_bt import run_candidate_backtest
from .propose import propose_with_cursor
from .templates import sample_candidates

from research_reviewer.reviewer import run_review
from research_reviewer.store import save_review

STORE = Path(os.environ.get("ALPHA_MINER_STORE", "/app/data/store/alpha_miner"))
INTERVAL = int(os.environ.get("ALPHA_MINER_INTERVAL_SEC", "0"))
A_COUNT = int(os.environ.get("ALPHA_MINER_A_COUNT", "60"))
B_COUNT = int(os.environ.get("ALPHA_MINER_B_COUNT", "4"))
WORKERS = int(os.environ.get("ALPHA_MINER_WORKERS", "20"))
ONCE = os.environ.get("ALPHA_MINER_ONCE", "").strip() in {"1", "true", "yes"}
# How many in-flight backtest batches to keep queued ahead of workers.
PREFETCH_BATCHES = int(os.environ.get("ALPHA_MINER_PREFETCH", "2"))

_write_lock = threading.Lock()
_promote_lock = threading.Lock()
_cursor_q: queue.Queue = queue.Queue(maxsize=8)
_name_cache: list[str] = []
_name_lock = threading.Lock()
_post_q: queue.Queue = queue.Queue(maxsize=8000)
_POST_WORKERS = int(os.environ.get("ALPHA_MINER_POST_WORKERS", "12"))
_run_buf: list[str] = []
_run_buf_last_flush = 0.0


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _append_run(record: dict) -> None:
    global _run_buf_last_flush
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / "runs.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _write_lock:
        _run_buf.append(line)
        now = time.time()
        if len(_run_buf) < 80 and (now - _run_buf_last_flush) < 1.0:
            return
        if path.exists() and path.stat().st_size > 400_000_000:
            rotated = path.with_name(path.stem + ".1.jsonl")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
            except Exception:
                pass
        chunk = "".join(_run_buf)
        _run_buf.clear()
        _run_buf_last_flush = now
        with path.open("a", encoding="utf-8") as f:
            f.write(chunk)


def _promote(client: QuantDeskClient, cand: dict, metrics: dict) -> str | None:
    with _promote_lock:
        created = client.create_strategy(
            name=cand["name"],
            type_=cand.get("type") or "custom",
            description=(cand.get("description") or "")
            + f" | mined={cand.get('source')} sharpe={metrics.get('sharpe')}",
            params={**(cand.get("params") or {}), "symbols": cand["symbols"]},
            code=cand["code"],
        )
        sid = created.get("id") or created.get("strategy_id")
        if sid:
            client.patch_strategy(
                sid,
                {
                    "enabled": False,
                    "metrics": {
                        "total_return": metrics.get("total_return"),
                        "sharpe": metrics.get("sharpe"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "trades": metrics.get("trades"),
                        "win_rate": metrics.get("win_rate"),
                        "source": cand.get("source"),
                    },
                },
            )
        return sid




def _deep_review_async(strategy_id: str, cand: dict, metrics: dict, backtest: dict) -> None:
    """Background IS/OOS + optional LLM review after promote."""
    import os

    def _work() -> None:
        try:
            use_llm = os.environ.get("RESEARCH_REVIEW_LLM", "0").strip() in {"1", "true", "yes"}
            review = run_review(
                cand,
                metrics,
                backtest=backtest,
                strategy_id=strategy_id,
                run_stat=True,
                run_llm=use_llm,
            )
            _log(
                f"DEEP_REVIEW {strategy_id} score={review.get('research_score')} "
                f"rec={review.get('recommendation')}"
            )
        except Exception as exc:
            _log(f"DEEP_REVIEW error {strategy_id}: {exc}")

    threading.Thread(target=_work, name=f"review-{strategy_id}", daemon=True).start()


def _handle_bt_result(client: QuantDeskClient, payload: dict) -> dict:
    cand = payload.get("cand") or {}
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": cand.get("source"),
        "name": cand.get("name"),
        "type": cand.get("type"),
        "symbols": cand.get("symbols"),
        "params": cand.get("params"),
        "status": "started",
        "engine": "local_process",
    }
    if not payload.get("ok"):
        record["status"] = "error"
        record["error"] = payload.get("error", "unknown")[:500]
        _append_run(record)
        _log(f"ERROR {cand.get('name')}: {record['error']}")
        return record

    metrics = extract_metrics(payload.get("backtest") or {})
    record["metrics"] = {k: v for k, v in metrics.items() if k != "raw"}
    passed, fails = evaluate(metrics)
    record["gate_pass"] = passed
    record["gate_fails"] = fails
    if not passed:
        record["status"] = "rejected"
        _append_run(record)
        _log(f"REJECT {cand.get('name')}: {fails}")
        return record

    # Layer-1 research gates (optional; skip to keep post-queue drained)
    if os.environ.get("ALPHA_MINER_QUICK_REVIEW", "1").strip() in {"0", "false", "no"}:
        quick_review = {
            "research_gate_pass": True,
            "research_score": 100.0,
            "rules": {"fails": []},
        }
    else:
        quick_review = run_review(
            cand,
            metrics,
            backtest=payload.get("backtest") or {},
            run_stat=False,
            run_llm=False,
        )
    record["research_gate_pass"] = quick_review["research_gate_pass"]
    record["research_score"] = quick_review["research_score"]
    record["research_fails"] = quick_review["rules"].get("fails", [])
    if not quick_review["research_gate_pass"]:
        record["status"] = "rejected"
        record["reject_stage"] = "research_gates"
        _append_run(record)
        _log(f"RESEARCH_REJECT {cand.get('name')}: {record['research_fails']}")
        return record

    try:
        sid = _promote(client, cand, metrics)
        record["strategy_id"] = sid
        record["status"] = "promoted"
        record["research_score"] = quick_review["research_score"]
        _append_run(record)
        _log(
            f"PROMOTE {cand.get('name')} -> {sid} sharpe={metrics.get('sharpe')} "
            f"research_score={quick_review.get('research_score')}"
        )
        if sid:
            save_review(sid, quick_review)
            _deep_review_async(sid, cand, metrics, payload.get("backtest") or {})
        with _name_lock:
            _name_cache.append(str(cand.get("name") or ""))
            del _name_cache[:-200]
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"promote: {exc}"[:500]
        _append_run(record)
        _log(f"ERROR promote {cand.get('name')}: {exc}")
    return record


def _login_with_retry(client: QuantDeskClient, attempts: int = 30) -> None:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            client.login()
            if i > 1:
                _log(f"login ok after {i} attempts")
            return
        except Exception as exc:
            last = exc
            _log(f"login wait {i}/{attempts}: {exc}")
            time.sleep(min(10, 1 + i))
    raise RuntimeError(f"login failed after {attempts} attempts: {last}")


def _cursor_worker() -> None:
    """Background thread: keep asking MiMo; never block the CPU pool."""
    while True:
        try:
            with _name_lock:
                names = list(_name_cache[-40:])
            cands = propose_with_cursor(B_COUNT, recent_names=names)
            if cands:
                _log(f"Cursor inbox proposed {len(cands)} candidates (async)")
                try:
                    _cursor_q.put(cands, timeout=120)
                except queue.Full:
                    _log("Cursor queue full; dropping batch")
            else:
                time.sleep(15)
        except Exception as exc:
            _log(f"Cursor inbox idle/fail (async): {exc}")
            time.sleep(15)


def _next_batch() -> list[dict]:
    batch = sample_candidates(A_COUNT)
    # Drain any ready MiMo ideas (non-blocking)
    while True:
        try:
            cands = _cursor_q.get_nowait()
            batch.extend(cands)
        except queue.Empty:
            break
    return batch


def _post_worker(worker_id: int) -> None:
    """Dedicated thread: gates + promote/API so process pool stays on CPU."""
    client = QuantDeskClient()
    try:
        _login_with_retry(client, attempts=60)
    except Exception as exc:
        _log(f"post-worker-{worker_id} login fail: {exc}")
        return
    _log(f"post-worker-{worker_id} ready")
    while True:
        payload = _post_q.get()
        try:
            _handle_bt_result(client, payload)
        except Exception as exc:
            _log(f"post-worker-{worker_id} error: {exc}")


def _drain_done(
    client: QuantDeskClient, inflight: dict[Future, str], results_acc: list
) -> None:
    # client unused here — post workers own API clients
    del client
    done = [f for f in list(inflight) if f.done()]
    for fut in done:
        inflight.pop(fut, None)
        try:
            payload = fut.result()
            # Non-blocking enqueue; if full, handle inline to apply backpressure
            try:
                _post_q.put(payload, timeout=30)
                results_acc.append({"status": "queued"})
            except queue.Full:
                # rare: process inline
                results_acc.append({"status": "queued"})
                _post_q.put(payload)
        except Exception as exc:
            _log(f"worker crash: {exc}")
            results_acc.append({"status": "error", "error": str(exc)[:500]})


def run_forever(pool: ProcessPoolExecutor) -> None:
    client = QuantDeskClient()
    _login_with_retry(client)
    try:
        existing = [str(s.get("name") or "") for s in client.list_strategies()]
        with _name_lock:
            _name_cache.extend(existing[-200:])
    except Exception as exc:
        _log(f"list strategies warn: {exc}")

    threading.Thread(target=_cursor_worker, name="cursor", daemon=True).start()
    for i in range(max(4, _POST_WORKERS)):
        threading.Thread(
            target=_post_worker, args=(i,), name=f"post-{i}", daemon=True
        ).start()
    _log(f"started {_POST_WORKERS} async post-workers for promote/API")

    inflight: dict[Future, str] = {}
    batch_i = 0
    t_stats = time.time()
    n_promoted = n_rejected = n_errors = n_total = 0

    _log(
        f"hot loop start workers={WORKERS} A={A_COUNT} B={B_COUNT} "
        f"prefetch={PREFETCH_BATCHES} (Cursor inbox async, CPU pool always fed)"
    )

    while True:
        # Keep workers saturated: submit until we have ~WORKERS*prefetch futures
        target = max(WORKERS * PREFETCH_BATCHES, WORKERS)
        while len(inflight) < target:
            batch = _next_batch()
            batch_i += 1
            _log(f"submit batch#{batch_i} n={len(batch)} inflight={len(inflight)}")
            for c in batch:
                fut = pool.submit(run_candidate_backtest, c)
                inflight[fut] = c.get("name") or "?"
            if ONCE:
                break

        # Wait for at least one completion
        if not inflight:
            time.sleep(0.5)
            continue
        done_futs = []
        try:
            for fut in as_completed(list(inflight.keys()), timeout=2):
                done_futs.append(fut)
                break
        except TimeoutError:
            pass

        finished: list[dict] = []
        _drain_done(client, inflight, finished)
        for r in finished:
            st = r.get("status")
            if st == "queued":
                n_total += 1  # completed backtests handed to post-workers
                continue
            n_total += 1
            if st == "promoted":
                n_promoted += 1
            elif st == "rejected":
                n_rejected += 1
            elif st == "error":
                n_errors += 1

        if time.time() - t_stats >= 30:
            _log(
                f"stats 30s window total={n_total} promoted={n_promoted} "
                f"rejected={n_rejected} errors={n_errors} inflight={len(inflight)} "
                f"post_q={_post_q.qsize()}"
            )
            t_stats = time.time()
            n_promoted = n_rejected = n_errors = n_total = 0

        if ONCE and not inflight:
            return

        # Pace mining: brief pause when queue is light
        if INTERVAL > 0 and len(inflight) < max(2, WORKERS // 2):
            _log(f"pace sleep {INTERVAL}s (inflight={len(inflight)})")
            time.sleep(INTERVAL)


def main() -> int:
    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, "1")

    _log(
        f"alpha-miner start interval={INTERVAL}s A={A_COUNT} B={B_COUNT} "
        f"workers={WORKERS} mode=hot_local_pool"
    )
    with ProcessPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        try:
            run_forever(pool)
        except Exception as exc:
            _log(f"fatal: {exc}")
            traceback.print_exc()
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
