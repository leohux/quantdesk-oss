# -*- coding: utf-8 -*-
"""Wind-down ATRBreak + flatten __unassigned__ lots.

  python /app/scripts/ops_atrbreak_orphans.py
  python /app/scripts/ops_atrbreak_orphans.py --dry-run

- ATRBreak: allow_new_entries=false, max_hold_days=5, lifecycle WIND_DOWN
- Sell ATRBreak sleeve lots (already past 5 calendar days)
- Sell __unassigned__ GOOGL / META / TTWO / leftover AMZN

Overnight GTC cancels stay pending_cancel and qty_available=0 until RTH.
Failed sells write data/store/ops_pending_flatten.json; the intraday loop
retries during RTH (even if 046bfa is disabled).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config.store import get_strategy, update_strategy
from core.portfolio.fills import await_fill
from core.portfolio.sleeve import UNASSIGNED, SleeveBook
from core.trade_log import record_exit
from execution.alpaca_client import AlpacaPaperClient
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ATRBREAK_ID = "cursor-hybrid-classic-atrbreak-aapl-m15-061001-d4e35-44d4b4"
ORPHAN_SYMBOLS = ("GOOGL", "META", "TTWO", "AMZN")
PENDING_PATH = Path(
    os.environ.get("OPS_PENDING_FLATTEN", "/app/data/store/ops_pending_flatten.json")
)
RETRY_SEC = 600


def _patch_atrbreak() -> dict:
    st = get_strategy(ATRBREAK_ID)
    params = dict(st.get("params") or {})
    print(
        "before enabled=",
        st.get("enabled"),
        "allow_new=",
        params.get("allow_new_entries"),
        "max_hold=",
        params.get("max_hold_days"),
    )
    params["allow_new_entries"] = False
    params["max_hold_days"] = 5
    params["lifecycle"] = "WIND_DOWN_ARCHIVE"
    params["wind_down_note"] = (
        "2026-08-13: exits-only; max_hold_days=5 so overdue lots time-exit"
    )
    updated = update_strategy(
        ATRBREAK_ID,
        {
            "enabled": True,
            "params": params,
        },
    )
    p = updated.get("params") or {}
    print(
        "after enabled=",
        updated.get("enabled"),
        "allow_new=",
        p.get("allow_new_entries"),
        "max_hold=",
        p.get("max_hold_days"),
        "lifecycle=",
        p.get("lifecycle"),
    )
    return updated


def _plan(sleeve: SleeveBook) -> dict[str, list[tuple[str, str, int]]]:
    """symbol -> [(strategy_id, name, qty), ...]"""
    out: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    st = get_strategy(ATRBREAK_ID)
    name = str(st.get("name") or ATRBREAK_ID)
    for sym in sorted(sleeve.symbols_of(ATRBREAK_ID)):
        qty = int(float(sleeve.qty_of(ATRBREAK_ID, sym)))
        if qty > 0:
            out[sym].append((ATRBREAK_ID, name, qty))
    for sym in ORPHAN_SYMBOLS:
        qty = int(float(sleeve.qty_of(UNASSIGNED, sym)))
        if qty > 0:
            out[sym].append((UNASSIGNED, "unassigned", qty))
    return dict(out)


def _sell_plan(client: AlpacaPaperClient, sleeve: SleeveBook, dry_run: bool) -> dict:
    plan = _plan(sleeve)
    if not plan:
        print("nothing to sell")
        if not dry_run:
            PENDING_PATH.unlink(missing_ok=True)
        return {}
    positions = {p["symbol"]: p for p in client.positions()}
    for sym, lots in sorted(plan.items()):
        want = sum(q for _, _, q in lots)
        pos = positions.get(sym)
        broker_qty = int(float((pos or {}).get("qty") or 0))
        sell_qty = min(want, broker_qty)
        print(
            f"PLAN {sym} want={want} broker={broker_qty} sell={sell_qty} "
            f"lots={[(sid, q) for sid, _, q in lots]}"
        )
        if sell_qty <= 0:
            continue
        if dry_run:
            print(f"  DRY SELL {sym} qty={sell_qty}")
            continue
        try:
            # Overnight qty_available stays 0 after cancel. Full-symbol exit
            # lets unlock_and_sell fall back to close_position.
            if sell_qty >= broker_qty:
                result = client.unlock_and_sell(sym)
            else:
                result = client.unlock_and_sell(sym, sell_qty)
        except Exception as exc:
            print(f"  SELL FAIL {sym}: {exc}")
            continue
        fill = await_fill(client, {**result, "qty": sell_qty}, timeout_sec=25)
        print(f"  {fill.describe()} order={result.get('id')}")
        if not fill.filled:
            print(f"  queued/unfilled — sleeve left intact for {sym}")
            continue
        sold = int(fill.filled_qty)
        px = fill.price_or(float((pos or {}).get("current_price") or 0))
        remaining_sold = sold
        for sid, name, qty in lots:
            take = min(qty, remaining_sold)
            if take <= 0:
                break
            sleeve.reduce(sid, sym, take)
            if sid != UNASSIGNED:
                n = record_exit(
                    sid,
                    name,
                    sym,
                    take,
                    px,
                    order_id=result.get("id"),
                    status=fill.status,
                    reason="ops_winddown_flatten",
                    filled_qty=float(take),
                )
                print(f"  journal {sid} {sym} closed_rows={n} qty={take} px={px}")
            else:
                print(f"  sleeve {UNASSIGNED} {sym} reduced {take}")
            remaining_sold -= take
    held = sorted(sleeve.symbols_of(ATRBREAK_ID))
    print("ATRBreak remaining sleeve:", held or "[]")
    remaining = _plan(sleeve)
    orphans_left = [
        (sym, int(float(sleeve.qty_of(UNASSIGNED, sym))))
        for sym in ORPHAN_SYMBOLS
        if float(sleeve.qty_of(UNASSIGNED, sym)) > 0
    ]
    if remaining and not dry_run:
        _write_pending(remaining)
        print(f"pending flatten written → {PENDING_PATH}")
    elif not remaining and not dry_run:
        PENDING_PATH.unlink(missing_ok=True)
    if not held and not orphans_left and not dry_run:
        st = get_strategy(ATRBREAK_ID)
        params = dict(st.get("params") or {})
        params["allow_new_entries"] = False
        params["wind_down_completed_at"] = datetime.now(timezone.utc).isoformat()
        params["lifecycle"] = "RESEARCH_ARCHIVE_GATE_FAIL"
        update_strategy(ATRBREAK_ID, {"enabled": False, "params": params})
        print("ATRBreak sleeve empty → enabled=false")
    return remaining


def _write_pending(
    plan: dict[str, list[tuple[str, str, int]]], *, last_attempt: float | None = None
) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_attempt": time.time() if last_attempt is None else last_attempt,
        "reason": "atrbreak_winddown_orphans",
        "symbols": {
            sym: [{"strategy_id": sid, "qty": qty} for sid, _, qty in lots]
            for sym, lots in sorted(plan.items())
        },
    }
    PENDING_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def maybe_flatten_from_runner(*, min_interval_sec: int = RETRY_SEC) -> None:
    """RTH retry from the intraday loop. No-op when the book is already clean."""
    sleeve = SleeveBook.load()
    plan = _plan(sleeve)
    if not plan:
        PENDING_PATH.unlink(missing_ok=True)
        return
    now = time.time()
    last = 0.0
    if PENDING_PATH.exists():
        try:
            last = float(json.loads(PENDING_PATH.read_text(encoding="utf-8")).get("last_attempt") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            last = 0.0
    if last and now - last < min_interval_sec:
        return
    _write_pending(plan)
    print("=== pending flatten retry ===", flush=True)
    client = AlpacaPaperClient()
    _sell_plan(client, sleeve, dry_run=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("=== patch ATRBreak ===")
    if args.dry_run:
        st = get_strategy(ATRBREAK_ID)
        p = st.get("params") or {}
        print(
            "dry skip patch; current allow_new=",
            p.get("allow_new_entries"),
            "max_hold=",
            p.get("max_hold_days"),
        )
    else:
        _patch_atrbreak()
    print("=== flatten ===")
    sleeve = SleeveBook.load()
    client = AlpacaPaperClient()
    if not args.dry_run and not client.is_market_open():
        remaining = _plan(sleeve)
        if remaining:
            _write_pending(remaining, last_attempt=0)
            print("market closed — pending flatten queued for RTH")
            print("queued:", sorted(remaining))
        return
    remaining = _sell_plan(client, sleeve, args.dry_run)
    if remaining and args.dry_run:
        print("dry-run remaining:", sorted(remaining))


if __name__ == "__main__":
    main()
