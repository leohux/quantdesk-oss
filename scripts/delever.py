"""
Trim positions back to the per-name cap.
========================================
A cap that is only checked at entry is not a cap. Positions opened under the
old 25% ceiling are still sized at ~25% of equity each, which is how the book
ended up at 149% gross with a negative cash balance.

This sells the excess of every position above SINGLE_NAME_CAP_PCT and leaves
the rest alone. Selling has to cancel the resting bracket legs that lock the
shares, so protection is re-armed on the remainder as a GTC OCO — without that
step a trim would quietly strip the stop-loss off everything it touched.

Idempotent: once every position is at or under the cap it does nothing, so it
is safe to run daily from cron.

Usage:
  python /app/scripts/delever.py                 # dry run, prints the plan
  python /app/scripts/delever.py --apply
  python /app/scripts/delever.py --cap 12 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, "/app")

from core.portfolio.fills import await_fill
from core.portfolio.sleeve import UNASSIGNED, SleeveBook, ensure_schema
from core.trade_log import record_exit
from execution.alpaca_client import AlpacaPaperClient

DEFAULT_CAP_PCT = 10.0
# Leave a position alone unless it is meaningfully over; churning on rounding
# error costs spread for nothing.
TOLERANCE = 1.15
# Fallback protection, only used when the original bracket levels cannot be read.
STOP_PCT = 0.08
TAKE_PROFIT_RR = 2.5
FILL_WAIT_SEC = 45
LOCK_PATH = "/var/lock/quantdesk-delever.lock"
ARM_RETRIES = 3


def _acquire_lock():
    """Cross-process lock so delever cannot race the trading runners."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        print("another delever/runner holds the lock — abort")
        sys.exit(0)
    except ImportError:
        # Windows dev host: best-effort only.
        pass
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _resting_levels(client, symbols: list[str]) -> dict[str, dict[str, float]]:
    """Capture each symbol's live bracket stop / take-profit before we cancel it.

    Re-arming with the strategy's own levels keeps its risk profile intact; a
    flat percentage would silently rewrite every strategy's exit plan.
    """
    levels: dict[str, dict[str, float]] = {}
    try:
        resting = client.orders(status="open", limit=200, symbols=symbols)
    except Exception:
        return levels
    for o in resting:
        if str(o.get("side", "")).lower() != "sell":
            continue
        entry = levels.setdefault(str(o["symbol"]), {})
        if o.get("stop_price"):
            entry["stop"] = float(o["stop_price"])
        if o.get("limit_price"):
            entry["take"] = float(o["limit_price"])
    return levels


def _arm_protection(client, symbol: str, qty: int, price: float, original: dict) -> str:
    """Put a stop back under a position, keeping its take-profit if it had one.

    An OCO needs both legs, so when only a stop is known a plain GTC stop is
    used rather than inventing a take-profit the strategy never chose.
    """
    if qty <= 0 or price <= 0:
        return "skipped (no free shares)"

    stop = original.get("stop")
    take = original.get("take")
    source = "original"
    if not stop or not (0 < stop < price):
        stop = round(price * (1 - STOP_PCT), 2)
        source = "original TP + default stop" if take else "default"
    if take and not (take > price):
        take = None  # a take-profit already through the market would fill at once

    if take:
        client.oco_exit(symbol, qty, round(float(take), 2), round(float(stop), 2))
        return f"OCO stop=${float(stop):.2f} tp=${float(take):.2f} [{source}]"
    client.protective_stop(symbol, qty, round(float(stop), 2))
    return f"STOP ${float(stop):.2f} [{source}]"


def _plan(positions: list[dict], equity: float, cap_pct: float) -> list[dict]:
    budget = equity * cap_pct / 100
    plan = []
    for p in sorted(positions, key=lambda x: -float(x.get("market_value") or 0)):
        qty = float(p["qty"])
        price = float(p.get("current_price") or 0)
        mv = float(p.get("market_value") or 0)
        if qty <= 0 or price <= 0:
            continue
        if mv <= budget * TOLERANCE:
            continue
        target = int(budget / price)
        sell_qty = int(qty) - target
        if sell_qty <= 0:
            continue
        plan.append(
            {
                "symbol": str(p["symbol"]),
                "qty": int(qty),
                "price": price,
                "market_value": mv,
                "pct": mv / equity * 100 if equity else 0,
                "target_qty": target,
                "sell_qty": sell_qty,
                "proceeds": sell_qty * price,
            }
        )
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description="Trim positions to the per-name cap")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP_PCT, help="per-name cap, %% of equity")
    ap.add_argument("--apply", action="store_true", help="place orders (default: dry run)")
    args = ap.parse_args()

    lock_fh = None
    if args.apply:
        lock_fh = _acquire_lock()

    ensure_schema()
    client = AlpacaPaperClient()
    account = client.account()
    equity = float(account["equity"])
    positions = client.positions()
    book = SleeveBook.load()

    gross = sum(abs(float(p.get("market_value") or 0)) for p in positions)
    print(
        f"equity ${equity:,.0f} | cash ${float(account['cash']):,.0f} | "
        f"gross {gross / equity * 100:.0f}% | cap {args.cap:.0f}%"
    )

    all_symbols = [str(p["symbol"]) for p in positions]
    levels = _resting_levels(client, all_symbols)
    unprotected = [s for s in all_symbols if not (levels.get(s) or {}).get("stop")]
    if unprotected:
        print(
            f"\nWARNING no stop-loss resting on {len(unprotected)}/{len(all_symbols)} "
            f"position(s): {', '.join(sorted(unprotected))}"
        )
        print("  a stop will be armed on each while trimming")

    plan = _plan(positions, equity, args.cap)
    if plan:
        print(
            f"\n{'SYMBOL':7s} {'HELD':>6s} {'NOW%':>6s} {'SELL':>6s} {'KEEP':>6s} "
            f"{'PROCEEDS':>11s}  OWNER"
        )
        for row in plan:
            owner = book.owner_of(row["symbol"]) or "-"
            print(
                f"{row['symbol']:7s} {row['qty']:6d} {row['pct']:5.1f}% {row['sell_qty']:6d} "
                f"{row['target_qty']:6d} {row['proceeds']:11,.0f}  {owner}"
            )
        freed = sum(r["proceeds"] for r in plan)
        print(
            f"\nselling ${freed:,.0f} across {len(plan)} name(s) — "
            f"gross {gross / equity * 100:.0f}% -> {(gross - freed) / equity * 100:.0f}%"
        )
    else:
        print("\nevery position is already within the cap — no trimming needed")

    if not plan and not unprotected:
        return

    if not args.apply:
        print("\ndry run — re-run with --apply to place the orders")
        return

    if not client.is_market_open():
        print("\nmarket closed — refusing to place trims that cannot fill today")
        sys.exit(1)

    trimmed: list[dict] = []
    # unlock_and_sell cancels the resting legs before it sells, so a symbol it
    # touched needs protection re-armed even when the sell itself failed.
    touched: set[str] = set()
    for row in plan:
        symbol, sell_qty = row["symbol"], row["sell_qty"]
        owner = book.owner_of(symbol)
        touched.add(symbol)
        try:
            result = client.unlock_and_sell(symbol, sell_qty)
        except Exception as exc:
            print(f"  FAIL {symbol}: {exc}")
            continue
        fill = await_fill(client, {**result, "qty": result.get("qty") or sell_qty})
        print(f"  SOLD {symbol} order={result.get('id')} {fill.describe()}")
        if fill.dead:
            print(f"  {symbol}: nothing sold, ledger untouched")
            continue
        filled = int(fill.filled_qty) if fill.filled else int(float(result.get("qty") or sell_qty))
        if owner and owner != UNASSIGNED:
            book.reduce(owner, symbol, filled)
            record_exit(
                strategy_id=owner,
                strategy_name=owner,
                symbol=symbol,
                qty=filled,
                price=fill.price_or(row["price"]),
                order_id=result.get("id"),
                status=fill.status,
                reason=f"delever to {args.cap:.0f}% cap",
            )
        trimmed.append(row)

    # Every position that was trimmed had its resting legs cancelled to free the
    # shares, and the unprotected ones need a stop regardless. Both are handled
    # the same way: cancel whatever is left, then arm a stop on the full holding.
    to_arm = sorted(touched | set(unprotected))
    if not to_arm:
        return

    if trimmed:
        print(f"\nwaiting up to {FILL_WAIT_SEC}s for trims to settle")
        deadline = time.time() + FILL_WAIT_SEC
        targets = {r["symbol"]: r["target_qty"] for r in trimmed}
        while time.time() < deadline:
            live = {p["symbol"]: float(p.get("qty") or 0) for p in client.positions()}
            if all(live.get(s, 1e9) <= q for s, q in targets.items()):
                break
            time.sleep(5)

    print("\narming protection")
    arm_failures: list[str] = []
    for symbol in to_arm:
        pos = next((p for p in client.positions() if p["symbol"] == symbol), None)
        if not pos:
            print(f"  {symbol}: position gone, nothing to protect")
            continue
        # Free the shares from any leg that survived the trim.
        try:
            client.cancel_open_orders(symbol)
            time.sleep(0.5)
        except Exception as exc:
            print(f"  {symbol}: could not cancel resting legs: {exc}")
        pos = next((p for p in client.positions() if p["symbol"] == symbol), None)
        qty = int(float(pos.get("qty") or 0)) if pos else 0
        price = float(pos.get("current_price") or 0) if pos else 0.0
        last_err = None
        for attempt in range(1, ARM_RETRIES + 1):
            try:
                how = _arm_protection(client, symbol, qty, price, levels.get(symbol) or {})
                print(f"  {symbol:6s} x{qty:<6d} {how}")
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                print(f"  {symbol:6s} ARM attempt {attempt}/{ARM_RETRIES} failed: {exc}")
                time.sleep(1.0)
        if last_err is not None:
            arm_failures.append(symbol)
            print(f"  {symbol:6s} ARM FAILED — UNPROTECTED")

    acct = client.account()
    pos_after = client.positions()
    gross_after = sum(abs(float(p.get("market_value") or 0)) for p in pos_after)
    print(
        f"\nafter: equity ${float(acct['equity']):,.0f} | cash ${float(acct['cash']):,.0f} | "
        f"gross {gross_after / float(acct['equity']) * 100:.0f}%"
    )

    if lock_fh is not None:
        try:
            lock_fh.close()
        except Exception:
            pass

    if arm_failures:
        print(
            f"\nFATAL: {len(arm_failures)} position(s) still unprotected: "
            f"{', '.join(arm_failures)}"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
