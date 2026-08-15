"""
Intraday Paper Runner — Tech Breakout Continuation
==================================================
Polls Alpaca IEX every N minutes during RTH (09:35–15:55 ET).
When a watched symbol's day-return hits [buy_surge, buy_cap) AND is above
SMA(trend_ma), plus optional false-breakout filters, submits a BRACKET
market order with broker-side SL/TP.

Also enforces ``params.max_hold_days`` (NYSE trading sessions since journal
open; weekends/holidays do not count). Aged sleeve lots are sold via
unlock_and_sell before new entries are scanned.

Shares the paper account with phase6_runner / news_trader via Sleeve +
combined_position_guard. ``strategies.json`` ``enabled`` is re-read every
loop tick — set enabled=false to pause without stopping the container.

Strategy id stays ``strategy-046bfa`` (former name 美股小盘早盘异动 is an alias).

Usage:
  python -m scripts.intraday_runner --once          # single scan (dry or live)
  python -m scripts.intraday_runner --once --execute
  python -m scripts.intraday_runner --loop          # stay alive, poll every N min
  python -m scripts.intraday_runner --loop --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

from core.portfolio.sleeve import (
    LedgerUnavailable,
    SleeveBook,
    broker_qty_map,
    ensure_schema as ensure_sleeve_schema,
    open_order_symbols,
)
from core.portfolio.account_guard import check_account_buy
from core.portfolio.combined_position_guard import check_combined_buy
from core.portfolio.fills import await_fill, reprice_protection
from core.portfolio.settle import format_settlements, settle_broker_exits
from core.portfolio.trail import (
    ensure_trail_columns,
    load_open_trail_rows,
    save_trail_state,
    seed_peak_from_entry,
    step_trail,
    trail_active_symbols,
    trail_params,
)
from core.config.calendar import USMarketCalendar, hold_trading_days
from core.trade_log import ensure_strategy, record_entry, record_exit
from data.loader import get_latest_prices, get_prior_close, load_ohlcv
from execution.broker_factory import get_trading_client

ET = ZoneInfo("America/New_York")
_MARKET_CAL = USMarketCalendar()
STORE = Path("/app/data/store")
STATE_FILE = STORE / "intraday_state.json"
LOG_FILE = STORE / "intraday_runner.log"
FILTER_LOG = STORE / "intraday_filter_events.jsonl"
STRATEGY_ID = os.environ.get("INTRADAY_STRATEGY_ID", "strategy-046bfa")

# Risk
MAX_POSITION_PCT = 10.0
# Default sleeve concurrency. Live path prefers params.stocknum when set
# (strategies.json); the old hardcoded 5 ignored stocknum entirely.
MAX_OPEN_POSITIONS = 3
MAX_GROSS_EXPOSURE_PCT = float(os.environ.get("MAX_GROSS_EXPOSURE_PCT", "150"))  # allow 1.5x book
POLL_SECONDS = int(os.environ.get("INTRADAY_POLL_SEC", "90"))
SYMBOL_PAUSE_SEC = float(os.environ.get("INTRADAY_SYMBOL_PAUSE", "0.35"))


def log(msg: str) -> None:
    line = f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}  {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_filter_event(event: dict[str, Any]) -> None:
    """Append structured filter / entry events for A/B attribution (discipline)."""
    try:
        FILTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy_id": STRATEGY_ID,
            **event,
        }
        with FILTER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log(f"  filter_event log failed: {exc}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries_today": {}, "last_scan": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_strategy() -> dict:
    from config.store import get_strategy

    return get_strategy(STRATEGY_ID)


def strategy_is_enabled(strat: dict | None = None) -> bool:
    """Honor strategies.json enabled — independent containers used to ignore it."""
    st = strat if strat is not None else load_strategy()
    return bool(st.get("enabled"))


def is_rth(now: datetime | None = None) -> bool:
    """Regular Trading Hours: 09:35–15:55 ET on NYSE sessions (skip first/last 5 min)."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    if not _MARKET_CAL.is_trading_day(now.date()):
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 35) <= mins <= (15 * 60 + 55)


def daily_sma(
    symbol: str, window: int = 50, *, min_window: int = 20
) -> tuple[float | None, int]:
    """Return (sma, window_used).

    Short-history fallback (explicit params.sma_short_history_fallback, 2026-08-04):
    if bars < ``window`` but >= ``min_window``, use the longest available
    trailing mean. Adopted so IPO names (e.g. SPCX) stay scannable; when bars
    later reach ``trend_ma``, compare for filter flip. Returns (None, 0) if
    history is shorter than ``min_window``.
    """
    need_days = max(window, min_window) * 3
    start = (datetime.now(timezone.utc) - timedelta(days=need_days)).strftime("%Y-%m-%d")
    df = load_ohlcv(symbol, start=start)
    if df is None or len(df) < min_window:
        return None, 0
    used = window if len(df) >= window else len(df)
    return float(df["Close"].rolling(used).mean().iloc[-1]), used


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def journal_opened_at(strategy_id: str, symbol: str) -> datetime | None:
    """Earliest open journal timestamp for this sleeve lot (UTC)."""
    try:
        from sqlalchemy import text

        from core.db import SyncSessionLocal

        s = SyncSessionLocal()
        try:
            row = s.execute(
                text(
                    """
                    SELECT MIN(opened_at) FROM trade_journal
                    WHERE strategy_id = :sid AND symbol = :sym
                      AND side = 'buy' AND status = 'open'
                    """
                ),
                {"sid": strategy_id, "sym": symbol},
            ).scalar()
            return _as_utc(row)
        finally:
            s.close()
    except Exception as exc:
        log(f"  journal_opened_at {symbol} failed: {exc}")
        return None


def hold_calendar_days(opened_at: datetime, now: datetime | None = None) -> int:
    """Deprecated alias — counts NYSE trading sessions, not calendar days."""
    return hold_trading_days(opened_at, now))


def manage_time_exits(
    client: Any,
    sleeve: SleeveBook | None,
    *,
    max_hold_days: int,
    execute: bool,
    strategy_name: str,
    positions: dict[str, dict[str, Any]],
    entries_today: dict[str, Any],
    exempt_symbols: set[str] | None = None,
    trail_exempt_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Flat sleeve lots that reached params.max_hold_days (NYSE sessions).

    ``params.time_exit_exempt`` symbols are skipped (manual hold / pending decision).
    ``trail_exempt_symbols`` skips lots with trail_active (profit-protecting trail
    owns the exit — see exempt_trail_active_from_time_exit).

    Co-owned names: sell only qty_available without canceling foreign brackets
    when possible; if shares are locked, unlock_and_sell may cancel sibling
    legs on that symbol (logged).
    """
    exits: list[dict[str, Any]] = []
    if sleeve is None or max_hold_days <= 0:
        return exits

    exempt = {s.upper() for s in (exempt_symbols or set())}
    trail_exempt = {s.upper() for s in (trail_exempt_symbols or set())}
    now = datetime.now(ET)
    for sym in sorted(sleeve.symbols_of(STRATEGY_ID)):
        if sym in exempt:
            log(f"  TIME_EXIT skip {sym}: in time_exit_exempt")
            continue
        if sym in trail_exempt:
            log(f"  TIME_EXIT skip {sym}: trail_active (exempt_trail_active_from_time_exit)")
            continue
        opened = journal_opened_at(STRATEGY_ID, sym)
        if opened is None:
            log(f"  TIME_EXIT skip {sym}: no open journal row for age")
            continue
        age = hold_trading_days(opened, now)
        if age < max_hold_days:
            continue

        owned = float(sleeve.qty_of(STRATEGY_ID, sym))
        pos = positions.get(sym)
        if not pos or owned <= 0:
            log(f"  TIME_EXIT skip {sym}: owned={owned} broker_pos={bool(pos)}")
            continue

        broker_qty = float(pos.get("qty") or 0)
        available = int(float(pos.get("qty_available") or 0))
        sell_qty = int(min(owned, broker_qty))
        if sell_qty <= 0:
            continue

        co_owners = [p for p in sleeve.owners_of(sym) if p.strategy_id != STRATEGY_ID and p.qty > 0]
        reason = f"time_exit {age} trading_d >= max_hold_days={max_hold_days}"
        info = {
            "symbol": sym,
            "qty": sell_qty,
            "age_days": age,
            "reason": reason,
            "co_owned": bool(co_owners),
        }

        if not execute:
            log(f"  -> DRY TIME_EXIT {sym} qty={sell_qty} {reason} co_owned={bool(co_owners)}")
            info["status"] = "dry-run"
            exits.append(info)
            continue

        try:
            if co_owners and available >= sell_qty:
                # Leave other sleeves' resting brackets alone.
                result = client.market_order(sym, sell_qty, "sell")
                note = "available_sell_no_cancel"
            else:
                if co_owners:
                    log(
                        f"  TIME_EXIT {sym}: co-owned and locked "
                        f"(avail={available}<{sell_qty}) — unlock_and_sell may "
                        f"cancel sibling brackets"
                    )
                result = client.unlock_and_sell(sym, sell_qty)
                note = "unlock_and_sell"

            fill = await_fill(client, {**result, "qty": sell_qty})
            log(
                f"  -> TIME_EXIT {sym} qty={sell_qty} {reason} "
                f"order={result.get('id')} {fill.describe()} ({note})"
            )
            if fill.dead:
                info["status"] = f"no_fill:{fill.status}"
                exits.append(info)
                continue
            if fill.filled:
                sell_qty = int(fill.filled_qty)
            px = fill.price_or(float(pos.get("current_price") or 0))
            sleeve.reduce(STRATEGY_ID, sym, sell_qty)
            record_exit(
                strategy_id=STRATEGY_ID,
                strategy_name=strategy_name,
                symbol=sym,
                qty=sell_qty,
                price=px,
                order_id=result.get("id"),
                status=fill.status,
                reason=reason,
                filled_qty=float(fill.filled_qty or sell_qty),
            )
            entries_today.pop(sym, None)
            remaining = broker_qty - sell_qty
            if remaining <= 1e-9:
                positions.pop(sym, None)
            else:
                positions[sym] = {
                    **pos,
                    "qty": remaining,
                    "qty_available": max(0.0, float(available) - sell_qty),
                }
            info.update({"status": fill.status, "exit_price": px, "sold_qty": sell_qty})
            exits.append(info)
        except Exception as exc:
            log(f"  TIME_EXIT {sym} ERROR: {exc}")
            info["status"] = f"error:{exc}"
            exits.append(info)

    return exits


def manage_day_crash(
    client: Any,
    sleeve: SleeveBook | None,
    *,
    day_crash: float,
    execute: bool,
    strategy_name: str,
    positions: dict[str, dict[str, Any]],
    entries_today: dict[str, Any],
    latest: dict[str, float],
) -> list[dict[str, Any]]:
    """Flatten sleeve lots when live day-return <= params.day_crash (e.g. -5%).

    Mirrors strategy_code backtest exit; previously params-only (dead on live).
    Applies even when trail_active — crash exit is a hard risk cut.
    """
    exits: list[dict[str, Any]] = []
    if sleeve is None or day_crash >= 0:
        return exits

    for sym in sorted(sleeve.symbols_of(STRATEGY_ID)):
        pos = positions.get(sym)
        if not pos:
            continue
        try:
            prior = get_prior_close(sym)
        except Exception:
            continue
        px = float(latest.get(sym) or pos.get("current_price") or 0)
        if not prior or prior <= 0 or px <= 0:
            continue
        day_ret = px / float(prior) - 1.0
        if day_ret > day_crash:
            continue

        owned = float(sleeve.qty_of(STRATEGY_ID, sym))
        sell_qty = int(min(owned, float(pos.get("qty") or 0)))
        if sell_qty <= 0:
            continue
        reason = f"day_crash {day_ret:.2%} <= {day_crash:.2%}"
        info = {"symbol": sym, "qty": sell_qty, "day_ret": day_ret, "reason": reason}
        if not execute:
            log(f"  -> DRY DAY_CRASH {sym} qty={sell_qty} {reason}")
            info["status"] = "dry-run"
            exits.append(info)
            continue
        try:
            result = client.unlock_and_sell(sym, sell_qty)
            fill = await_fill(client, {**result, "qty": sell_qty})
            log(f"  -> DAY_CRASH {sym} qty={sell_qty} {reason} {fill.describe()}")
            if fill.dead:
                info["status"] = f"no_fill:{fill.status}"
                exits.append(info)
                continue
            if fill.filled:
                sell_qty = int(fill.filled_qty)
            px = fill.price_or(px)
            sleeve.reduce(STRATEGY_ID, sym, sell_qty)
            record_exit(
                strategy_id=STRATEGY_ID,
                strategy_name=strategy_name,
                symbol=sym,
                qty=sell_qty,
                price=px,
                order_id=result.get("id"),
                status=fill.status,
                reason=reason,
                filled_qty=float(fill.filled_qty or sell_qty),
            )
            entries_today.pop(sym, None)
            positions.pop(sym, None)
            info.update({"status": fill.status, "exit_price": px})
            exits.append(info)
        except Exception as exc:
            log(f"  DAY_CRASH {sym} ERROR: {exc}")
            info["status"] = f"error:{exc}"
            exits.append(info)
    return exits


def manage_trailing_stops(
    client: Any,
    sleeve: SleeveBook | None,
    *,
    positions: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Update peak / activate trail / raise broker stop leg (or dry-run log).

    Broker replace only when ``params.trail_execute`` is True. State is always
    persisted to trade_journal so time-exit exemption can see trail_active.
    """
    out: list[dict[str, Any]] = []
    if sleeve is None:
        return out

    tp = trail_params(params)
    try:
        ensure_trail_columns()
        seeded = seed_peak_from_entry(STRATEGY_ID)
        if seeded:
            log(f"  TRAIL seeded peak_price on {seeded} open row(s)")
    except Exception as exc:
        log(f"  TRAIL schema/seed FAILED: {exc}")
        return out

    rows = load_open_trail_rows(STRATEGY_ID)
    mine = sleeve.symbols_of(STRATEGY_ID)
    for row in rows:
        sym = str(row["symbol"]).upper()
        if sym not in mine:
            continue
        pos = positions.get(sym)
        if not pos:
            continue
        px = float(pos.get("current_price") or 0)
        entry = float(row["entry_price"] or 0)
        if px <= 0 or entry <= 0:
            continue

        stepped = step_trail(
            entry_price=entry,
            current_price=px,
            peak_price=row.get("peak_price"),
            trail_active=bool(row.get("trail_active")),
            trail_stop_price=row.get("trail_stop_price"),
            activate_pct=tp["activate_pct"],
            trail_pct=tp["trail_pct"],
        )
        prev_active = bool(row.get("trail_active"))
        prev_stop = row.get("trail_stop_price")
        events = list(stepped["events"])

        # Persist state even in dry-run so exempt_trail_from_time_exit works.
        try:
            save_trail_state(
                int(row["id"]),
                peak_price=stepped["peak_price"],
                trail_active=stepped["trail_active"],
                trail_stop_price=stepped["trail_stop_price"],
            )
        except Exception as exc:
            log(f"  TRAIL save FAILED {sym}: {exc}")

        # Real validation case: trail would fire (drawdown from peak ≥ trail_pct)
        # while price is still below hard TP — the path the AMC/MSFT 1min replay
        # never exercised. Flag it; do not treat mere TRAIL_ACTIVATE as proof.
        hard_tp_px = entry * (1.0 + float(params.get("take_profit", 0.15)))
        peak = float(stepped["peak_price"])
        dd_from_peak = (1.0 - px / peak) if peak > 0 else 0.0
        if (
            stepped["trail_active"]
            and dd_from_peak + 1e-12 >= float(tp["trail_pct"])
            and px < hard_tp_px - 1e-9
        ):
            would_hit = (
                stepped["trail_stop_price"] is not None
                and px <= float(stepped["trail_stop_price"]) + 1e-9
            )
            log(
                f"  -> TRAIL_TESTCASE {sym} peak={peak:.4f} px={px:.4f} "
                f"dd_from_peak={dd_from_peak*100:.2f}% "
                f"trail_stop={stepped['trail_stop_price']} "
                f"hard_tp={hard_tp_px:.4f} would_broker_hit={would_hit} "
                f"trail_execute={tp['execute']} "
                f"— first real trail-takeover scenario (not hard-TP exit)"
            )
            events = list(events) + ["TRAIL_TESTCASE"]

        broker_note = None
        need_replace = stepped["trail_active"] and stepped["trail_stop_price"] is not None
        stop_moved = (
            need_replace
            and (
                not prev_active
                or prev_stop is None
                or float(stepped["trail_stop_price"]) > float(prev_stop) + 1e-9
            )
        )
        if stop_moved:
            new_stop = float(stepped["trail_stop_price"])
            if not tp["execute"]:
                broker_note = "dry-run (trail_execute=false)"
                for ev in events or ["TRAIL_UPDATE"]:
                    if ev == "TRAIL_TESTCASE":
                        continue
                    log(
                        f"  -> {ev} {sym} peak={stepped['peak_price']:.4f} "
                        f"trail_stop={new_stop:.4f} "
                        f"gain={stepped['gain_pct']*100:+.2f}% {broker_note}"
                    )
            else:
                try:
                    leg = client.find_open_stop_order(sym)
                    if leg is None:
                        # No resting stop — arm a protective stop (do not cancel TP).
                        owned = int(sleeve.qty_of(STRATEGY_ID, sym))
                        if owned > 0:
                            placed = client.protective_stop(sym, owned, new_stop)
                            broker_note = f"armed stop id={placed.get('id')}"
                        else:
                            broker_note = "no sleeve qty"
                            log(f"  TRAIL_REPLACE FAILED {sym}: {broker_note}")
                    else:
                        old_px = leg.get("stop_price")
                        if old_px is not None and abs(float(old_px) - new_stop) < 0.005:
                            broker_note = f"stop unchanged @{new_stop:.2f}"
                        else:
                            replaced = client.replace_stop_price(str(leg["id"]), new_stop)
                            broker_note = (
                                f"replaced {leg.get('id')} "
                                f"{old_px}->{replaced.get('stop_price')}"
                            )
                    for ev in events or ["TRAIL_UPDATE"]:
                        log(
                            f"  -> {ev} {sym} peak={stepped['peak_price']:.4f} "
                            f"trail_stop={new_stop:.4f} "
                            f"gain={stepped['gain_pct']*100:+.2f}% {broker_note}"
                        )
                except Exception as exc:
                    broker_note = f"FAILED ({exc})"
                    log(
                        f"  TRAIL_REPLACE FAILED {sym} stop={new_stop:.4f}: {exc} "
                        f"— position may be unprotected"
                    )

        if events or stop_moved:
            out.append(
                {
                    "symbol": sym,
                    "events": events,
                    "peak_price": stepped["peak_price"],
                    "trail_active": stepped["trail_active"],
                    "trail_stop_price": stepped["trail_stop_price"],
                    "gain_pct": round(stepped["gain_pct"] * 100, 2),
                    "broker": broker_note,
                    "execute": tp["execute"],
                }
            )
    return out


def ipo_vol_gate(symbol: str, params: dict[str, Any]) -> tuple[bool, str | None]:
    """Block short-history names whose realized vol exceeds IPO cap.

    Applies while daily bars < sma_short_history_fallback.recheck_at_bars (50).
    Once history is full, normal SMA50 + FB filters own the risk.
    Returns (blocked, reason).
    """
    fb = params.get("sma_short_history_fallback") or {}
    if not bool(fb.get("enabled", False)):
        return False, None
    recheck = int(fb.get("recheck_at_bars", 50) or 50)
    max_ann = float(params.get("ipo_max_ann_vol", 0.80))
    if max_ann <= 0:
        return False, None
    start = (datetime.now(timezone.utc) - timedelta(days=recheck * 3)).strftime("%Y-%m-%d")
    try:
        df = load_ohlcv(symbol, start=start)
    except Exception as exc:
        return True, f"ipo_ohlcv:{exc}"
    if df is None or len(df) < 10:
        return True, "ipo_bars<10"
    if len(df) >= recheck:
        return False, None
    close = df["Close"].astype(float)
    rets = close.pct_change().dropna()
    window = min(20, len(rets))
    if window < 5:
        return True, "ipo_rets_short"
    ann = float(rets.iloc[-window:].std(ddof=0) * (252 ** 0.5))
    if ann > max_ann:
        return True, f"ipo_ann_vol:{ann:.0%}>{max_ann:.0%}"
    return False, None


def passes_false_breakout(
    symbol: str, price: float, params: dict[str, Any]
) -> tuple[bool, str | None]:
    """Live mirror of strategy_code false-breakout filters (daily bars).

    Defaults match generate_signals: min_vol_ratio=1.2, breakout_lookback=10,
    max_prior_ret=0.08. Sentinel disables: min_vol_ratio<=0, lookback<=0,
    max_prior_ret>=9, min_close_loc<=0.
    """
    if not bool(params.get("filter_false_breakout", True)):
        return True, None

    min_close_loc = float(params.get("min_close_loc", 0.55))
    vol_ma = int(params.get("vol_ma", 20))
    min_vol_ratio = float(params.get("min_vol_ratio", 1.2))
    breakout_lookback = int(params.get("breakout_lookback", 10))
    max_prior_ret = float(params.get("max_prior_ret", 0.08))

    need = max(vol_ma, breakout_lookback, 5) + 5
    start = (datetime.now(timezone.utc) - timedelta(days=need * 3)).strftime("%Y-%m-%d")
    try:
        df = load_ohlcv(symbol, start=start)
    except Exception as e:
        return False, f"fb_ohlcv:{e}"
    if df is None or len(df) < 3:
        return False, "fb_ohlcv_short"

    # Normalize columns
    cols = {c.lower(): c for c in df.columns}
    close_c = cols.get("close") or "Close"
    high_c = cols.get("high") or "High"
    low_c = cols.get("low") or "Low"
    vol_c = cols.get("volume") or "Volume"
    high = df[high_c].astype(float)
    low = df[low_c].astype(float)

    today = datetime.now(ET).strftime("%Y-%m-%d")
    last_idx = df.index[-1]
    if hasattr(last_idx, "strftime"):
        last_day = last_idx.strftime("%Y-%m-%d")
    elif hasattr(last_idx, "date"):
        last_day = last_idx.date().isoformat()
    else:
        last_day = str(last_idx)[:10]
    # Drop a partial "today" bar so lookbacks use completed sessions only.
    completed = df.iloc[:-1] if last_day >= today and len(df) > 1 else df

    if max_prior_ret < 9.0 and len(completed) >= 2:
        y_close = float(completed[close_c].iloc[-1])
        y_prev = float(completed[close_c].iloc[-2])
        if y_prev > 0:
            y_ret = y_close / y_prev - 1.0
            if y_ret >= max_prior_ret:
                return False, f"prior_ret:{y_ret:.2%}>={max_prior_ret:.2%}"

    if breakout_lookback > 0 and len(completed) >= breakout_lookback:
        prior_high = float(completed[high_c].iloc[-breakout_lookback:].max())
        if price <= prior_high:
            return False, f"breakout:px<={prior_high:.2f}"

    if min_vol_ratio > 0 and vol_c in completed.columns and len(completed) >= max(vol_ma, 2):
        vol_series = completed[vol_c].astype(float)
        vol_sma = float(vol_series.rolling(vol_ma).mean().iloc[-1])
        last_vol = float(vol_series.iloc[-1])
        if vol_sma > 0 and (last_vol / vol_sma) < min_vol_ratio:
            return False, f"vol_ratio:{last_vol / vol_sma:.2f}<{min_vol_ratio:.2f}"

    if min_close_loc > 0 and last_day >= today:
        # Session bar available — live price vs today's range so far.
        day_high = max(float(high.iloc[-1]), float(price))
        day_low = min(float(low.iloc[-1]), float(price))
        rng = day_high - day_low
        if rng > 0:
            close_loc = (float(price) - day_low) / rng
            if close_loc < min_close_loc:
                return False, f"close_loc:{close_loc:.2f}<{min_close_loc:.2f}"

    return True, None


def scan_once(execute: bool = False) -> dict[str, Any]:
    strat = load_strategy()
    if not strategy_is_enabled(strat):
        log(
            f"SCAN skip — {STRATEGY_ID} enabled=false in strategies.json "
            "(flip enabled to resume; container can stay up)"
        )
        return {
            "time": datetime.now(ET).isoformat(),
            "skipped": "disabled",
            "strategy_id": STRATEGY_ID,
        }

    params = strat.get("params") or {}
    symbols = [str(s).upper() for s in (params.get("symbols") or [])]
    buy_surge = float(params.get("buy_surge", 0.02))
    buy_cap = float(params.get("buy_cap", 0.10))
    stop_loss = float(params.get("stop_loss", -0.08))
    take_profit = float(params.get("take_profit", 0.15))
    trend_ma = int(params.get("trend_ma", 50))
    max_hold_days = int(params.get("max_hold_days", 3) or 0)
    day_crash = float(params.get("day_crash", -0.05) or 0.0)
    time_exit_exempt = {
        str(s).upper() for s in (params.get("time_exit_exempt") or []) if s
    }
    max_open = int(params.get("stocknum", MAX_OPEN_POSITIONS) or MAX_OPEN_POSITIONS)
    if max_open < 1:
        max_open = MAX_OPEN_POSITIONS
    use_fb = bool(params.get("filter_false_breakout", True))
    strategy_name = str(strat.get("name") or STRATEGY_ID)

    # Paper only today; live mode is reserved in broker_factory (refuses loudly).
    client = get_trading_client()
    acct = client.account()
    equity = float(acct["equity"])
    buying_power = float(acct.get("buying_power") or acct.get("cash") or 0)
    positions = {p["symbol"]: p for p in client.positions()}
    state = load_state()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    entries_today = state.get("entries_today") or {}
    if state.get("day") != today:
        entries_today = {}
        state["day"] = today

    # Keep Postgres strategies FK happy for trade_journal / order logs
    ensure_strategy(
        STRATEGY_ID,
        name=strategy_name,
        stype=str(strat.get("type") or "intraday_surge"),
        params=params,
    )
    try:
        ensure_trail_columns()
    except Exception as exc:
        log(f"  TRAIL ensure columns failed: {exc}")

    # Ownership ledger — this process shares the account with phase6_runner
    try:
        ensure_sleeve_schema()
        sleeve = SleeveBook.load()
        changes = sleeve.reconcile(
            broker_qty_map(positions.values()),
            protected=open_order_symbols(client),
        )
        for change in changes:
            log(f"  [SLEEVE {change.kind}] {change}")
            # A stop-out frees the symbol; allow a fresh entry the same day.
            if change.kind == "closed" and change.strategy_id == STRATEGY_ID:
                entries_today.pop(change.symbol, None)
        for line in format_settlements(settle_broker_exits(client, changes)):
            log(f"  [SETTLED] {line}")
    except LedgerUnavailable as e:
        log(f"  [SLEEVE] ledger unavailable ({e}) — not opening new positions")
        sleeve = None
        execute = False

    # Batch quotes early — day_crash + entry scan share this map.
    try:
        latest_prices = get_latest_prices(symbols) if symbols else {}
    except Exception as e:
        log(f"  batch latest ERROR: {e}")
        latest_prices = {}

    # Trail BEFORE time exit so a just-activated winner is not day-forced out.
    trail_events = manage_trailing_stops(
        client, sleeve, positions=positions, params=params
    )
    crash_exits = manage_day_crash(
        client,
        sleeve,
        day_crash=day_crash,
        execute=execute,
        strategy_name=strategy_name,
        positions=positions,
        entries_today=entries_today,
        latest=latest_prices,
    )
    tp_cfg = trail_params(params)
    trail_skip: set[str] = set()
    if tp_cfg["exempt_trail_from_time_exit"]:
        try:
            trail_skip = trail_active_symbols(STRATEGY_ID)
        except Exception as exc:
            log(f"  TRAIL active lookup failed: {exc}")

    time_exits = manage_time_exits(
        client,
        sleeve,
        max_hold_days=max_hold_days,
        execute=execute,
        strategy_name=strategy_name,
        positions=positions,
        entries_today=entries_today,
        exempt_symbols=time_exit_exempt,
        trail_exempt_symbols=trail_skip,
    )
    # Reload sleeve after forced exits so entry scan sees freed slots.
    if (time_exits or crash_exits) and sleeve is not None:
        try:
            sleeve = SleeveBook.load()
        except Exception:
            pass

    mine = sleeve.symbols_of(STRATEGY_ID) if sleeve else set()
    allow_new_entries = bool(params.get("allow_new_entries", True))
    gross = sum(abs(float(p.get("market_value") or 0)) for p in positions.values())
    gross_pct = gross / equity * 100 if equity > 0 else 0.0

    report = {
        "time": datetime.now(ET).isoformat(),
        "equity": equity,
        "positions": len(positions),
        "symbols": symbols,
        "candidates": [],
        "orders": [],
        "skips": [],
        "time_exits": time_exits,
        "day_crash_exits": crash_exits,
        "trail_events": trail_events,
        "allow_new_entries": allow_new_entries,
        "sleeve_symbols": sorted(mine),
    }

    log(
        f"SCAN equity=${equity:,.0f} bp=${buying_power:,.0f} pos={len(positions)} "
        f"sleeve={len(mine)}/{max_open} gross={gross_pct:.0f}% "
        f"range=[{buy_surge:.0%},{buy_cap:.0%}) max_hold={max_hold_days}d "
        f"day_crash={day_crash:.0%} allow_new={allow_new_entries} execute={execute}"
    )

    # Wind-down: manage exits only; when sleeve empty, auto-disable strategy.
    if not allow_new_entries:
        log(
            f"WIND_DOWN exits-only sleeve={sorted(mine) or '[]'} "
            "(allow_new_entries=false; no new opens)"
        )
        if not mine:
            try:
                from config.store import update_strategy

                update_strategy(
                    STRATEGY_ID,
                    {
                        "enabled": False,
                        "params": {
                            **params,
                            "allow_new_entries": False,
                            "wind_down_completed_at": datetime.now(ET).isoformat(),
                        },
                    },
                )
                log(
                    "WIND_DOWN_COMPLETE — sleeve empty; set enabled=false "
                    "(frees shared paper capacity)"
                )
                report["skipped"] = "wind_down_complete"
            except Exception as exc:
                log(f"WIND_DOWN_COMPLETE but failed to disable strategy: {exc}")
        state["entries_today"] = entries_today
        state["last_scan"] = datetime.now(ET).isoformat()
        save_state(state)
        return report

    latest = latest_prices

    for sym in symbols:
        try:
            price = latest.get(sym)
            if price is None:
                report["skips"].append({"symbol": sym, "reason": "no_price"})
                log(f"  {sym:5s} SKIP no latest price")
                continue
            try:
                prior = get_prior_close(sym)
            except Exception as e:
                report["skips"].append({"symbol": sym, "reason": f"prior_close:{e}"})
                log(f"  {sym:5s} SKIP prior_close ({e})")
                continue
            if prior is None or prior != prior or prior <= 0:
                report["skips"].append({"symbol": sym, "reason": "bad_prior"})
                log(f"  {sym:5s} SKIP bad prior_close")
                continue
            day_ret = price / prior - 1.0
            sma, sma_n = daily_sma(sym, trend_ma)
            above_trend = sma is not None and price >= sma
            in_range = buy_surge <= day_ret < buy_cap
            owner = sleeve.owner_of(sym) if sleeve else None
            has_pos = owner is not None
            already = sym in entries_today

            info = {
                "symbol": sym,
                "price": round(price, 4),
                "prior_close": round(prior, 4),
                "day_ret_pct": round(day_ret * 100, 2),
                "sma": round(sma, 4) if sma else None,
                "sma_window": sma_n or None,
                "above_trend": above_trend,
                "in_range": in_range,
                "has_position": has_pos,
            }
            report["candidates"].append(info)

            log(
                f"  {sym:5s} px={price:8.2f} ret={day_ret*100:+6.2f}% "
                f"sma{sma_n or trend_ma}={sma or 0:8.2f} trend={above_trend} "
                f"surge={in_range} pos={has_pos}"
            )
            if SYMBOL_PAUSE_SEC > 0:
                time.sleep(SYMBOL_PAUSE_SEC)

            if not (in_range and above_trend):
                if in_range and not above_trend:
                    log_filter_event(
                        {
                            "event": "skip_trend",
                            "symbol": sym,
                            **info,
                            "reason": "below_sma",
                        }
                    )
                continue

            blocked_ipo, ipo_reason = ipo_vol_gate(sym, params)
            if blocked_ipo:
                report["skips"].append({**info, "reason": ipo_reason or "ipo_vol_gate"})
                log(f"  -> SKIP {sym} ipo_vol_gate ({ipo_reason})")
                log_filter_event(
                    {
                        "event": "skip_ipo_vol",
                        "symbol": sym,
                        **info,
                        "reason": ipo_reason,
                    }
                )
                continue

            if use_fb:
                ok_fb, fb_reason = passes_false_breakout(sym, float(price), params)
                if not ok_fb:
                    report["skips"].append({**info, "reason": fb_reason or "false_breakout"})
                    log(f"  -> SKIP {sym} false_breakout ({fb_reason})")
                    log_filter_event(
                        {
                            "event": "skip_false_breakout",
                            "symbol": sym,
                            **info,
                            "filter_false_breakout": True,
                            "reason": fb_reason,
                        }
                    )
                    continue
            if has_pos or already:
                reason = "already_in" if owner == STRATEGY_ID else f"owned_by:{owner}"
                report["skips"].append({**info, "reason": reason})
                continue
            if len(mine) + len(report["orders"]) >= max_open:
                report["skips"].append({**info, "reason": "max_positions"})
                continue
            if gross_pct >= MAX_GROSS_EXPOSURE_PCT:
                report["skips"].append({**info, "reason": "gross_exposure"})
                log(f"  -> SKIP {sym} gross exposure {gross_pct:.0f}% >= {MAX_GROSS_EXPOSURE_PCT:.0f}%")
                continue

            # Per-symbol override (e.g. SPCX IPO high-vol → 5%); else account default.
            sym_caps = params.get("symbol_position_pct") or {}
            pos_pct = float(sym_caps.get(sym, MAX_POSITION_PCT))
            if pos_pct <= 0:
                pos_pct = MAX_POSITION_PCT
            qty = int(equity * (pos_pct / 100) / price)
            max_by_bp = int(buying_power / price) if price > 0 else 0
            if max_by_bp <= 0:
                report["skips"].append({**info, "reason": "buying_power"})
                log(f"  -> SKIP {sym} buying_power insufficient (${buying_power:,.0f})")
                continue
            if qty > max_by_bp:
                log(f"  -> clamp {sym} qty {qty}->{max_by_bp} (bp=${buying_power:,.0f})")
                qty = max_by_bp
            if qty <= 0:
                report["skips"].append({**info, "reason": "qty0"})
                continue

            if not execute:
                log(f"  -> DRY BUY {sym} qty={qty} SL={stop_loss:.0%} TP={take_profit:.0%}")
                report["orders"].append(
                    {"symbol": sym, "qty": qty, "status": "dry-run", "price": price}
                )
                continue

            # Claim first: phase6_runner may be scanning the same symbol right now
            if sleeve is None or not sleeve.claim(STRATEGY_ID, sym, qty, price):
                report["skips"].append({**info, "reason": "sleeve_claim"})
                log(f"  -> SKIP {sym} sleeve claim failed")
                continue

            cap = check_combined_buy(
                client, sym, float(qty) * float(price), ref_price=float(price)
            )
            if not cap.allowed:
                sleeve.release(STRATEGY_ID, sym)
                report["skips"].append({**info, "reason": "combined_position_cap"})
                log(f"  -> SKIP {sym} combined position cap ({cap.reason})")
                continue
            acct_cap = check_account_buy(client, float(qty) * float(price))
            if not acct_cap.allowed:
                sleeve.release(STRATEGY_ID, sym)
                report["skips"].append({**info, "reason": "account_exposure"})
                log(f"  -> SKIP {sym} account cap ({acct_cap.reason})")
                continue

            signal_px = price
            try:
                result = client.bracket_order(
                    symbol=sym,
                    qty=qty,
                    entry_price=price,
                    stop_loss_pct=stop_loss,
                    take_profit_pct=take_profit,
                    side="buy",
                )
            except Exception:
                sleeve.release(STRATEGY_ID, sym)
                raise

            # Confirm with the broker before the ledger and journal are told
            # anything: a rejected order must not leave a claim behind, and a
            # partial fill must not be booked as a full one.
            fill = await_fill(client, result)
            log(f"  -> ORDER {sym} id={result.get('id')} {fill.describe()}")
            if fill.dead:
                sleeve.release(STRATEGY_ID, sym)
                report["skips"].append({"symbol": sym, "reason": f"no fill ({fill.status})"})
                continue
            if fill.filled:
                qty = int(fill.filled_qty)
                price = fill.price_or(price)
                sleeve.claim(STRATEGY_ID, sym, qty, price)
                note = reprice_protection(
                    client, sym, qty, price, stop_loss, take_profit, signal_price=signal_px
                )
                if note:
                    log(f"  -> {sym} {note}")

            mine.add(sym)
            buying_power = max(0.0, buying_power - qty * price)
            gross += qty * price
            gross_pct = gross / equity * 100 if equity > 0 else 0.0
            entries_today[sym] = {
                "at": datetime.now(ET).isoformat(),
                "price": price,
                "qty": qty,
                "order_id": result.get("id"),
                "sl": result.get("stop_loss"),
                "tp": result.get("take_profit"),
            }
            report["orders"].append(result)

            record_entry(
                strategy_id=STRATEGY_ID,
                strategy_name=strategy_name,
                symbol=sym,
                qty=qty,
                price=price,
                order_id=result.get("id"),
                status=fill.status,
                reason=f"intraday surge {day_ret:.2%}",
                filled_qty=fill.filled_qty,
                position_value_pct=round(qty * price / equity * 100, 2) if equity else None,
            )
            log_filter_event(
                {
                    "event": "entry",
                    "symbol": sym,
                    **info,
                    "qty": qty,
                    "filter_false_breakout": use_fb,
                    "filters_passed": True,
                    "order_id": result.get("id"),
                }
            )

        except Exception as e:
            log(f"  {sym} ERROR: {e}")
            report["skips"].append({"symbol": sym, "reason": str(e)})

    state["entries_today"] = entries_today
    state["last_scan"] = datetime.now(ET).isoformat()
    save_state(state)
    return report


def seconds_until_next_rth(now: datetime | None = None) -> int:
    """Sleep budget until next 09:35 ET RTH open (cap 1h chunks for wakeups)."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    # Already in RTH
    if is_rth(now):
        return POLL_SECONDS

    candidate = now.replace(hour=9, minute=35, second=0, microsecond=0)
    today = now.date()
    if _MARKET_CAL.is_trading_day(today) and now < candidate:
        pass  # before today's 09:35
    else:
        nxt = _MARKET_CAL.next_trading_day(today + timedelta(days=1))
        candidate = datetime(nxt.year, nxt.month, nxt.day, 9, 35, tzinfo=ET)

    secs = int((candidate - now).total_seconds())
    # Wake at least every hour so deploy/restart signals are noticed
    return max(30, min(secs, 3600))


def run_loop(execute: bool = False) -> None:
    log(f"LOOP start poll={POLL_SECONDS}s execute={execute} strategy={STRATEGY_ID}")
    while True:
        sleep_for = POLL_SECONDS
        try:
            # ATRBreak/orphan flatten is account-level; retry even if 046bfa is paused.
            if execute and is_rth():
                try:
                    from ops_atrbreak_orphans import maybe_flatten_from_runner

                    maybe_flatten_from_runner()
                except Exception as exc:
                    log(f"OPS flatten retry failed: {exc}")
            # Re-read strategies.json every tick so enabled=false actually stops
            # new scans without requiring a container restart.
            if not strategy_is_enabled():
                now = datetime.now(ET)
                sleep_for = min(POLL_SECONDS * 5, 300)
                log(
                    f"DISABLED {STRATEGY_ID} at {now.strftime('%a %H:%M %Z')} — "
                    f"sleep {sleep_for}s (set enabled=true in strategies.json to resume)"
                )
            elif is_rth():
                scan_once(execute=execute)
            else:
                now = datetime.now(ET)
                sleep_for = seconds_until_next_rth(now)
                log(
                    f"outside RTH ({now.strftime('%a %H:%M %Z')}), "
                    f"sleep {sleep_for}s until next open window"
                )
        except Exception as e:
            log(f"LOOP ERROR: {e}")
            sleep_for = min(POLL_SECONDS * 5, 300)
        time.sleep(sleep_for)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tech breakout continuation paper runner")
    parser.add_argument("--once", action="store_true", help="single scan then exit")
    parser.add_argument("--loop", action="store_true", help="poll forever during RTH")
    parser.add_argument("--execute", action="store_true", help="actually place bracket orders")
    parser.add_argument("--force", action="store_true", help="scan even outside RTH (testing)")
    args = parser.parse_args()

    if not args.once and not args.loop:
        args.once = True

    if args.once:
        if not args.force and not is_rth():
            log("outside RTH — use --force to scan anyway")
        report = scan_once(execute=args.execute)
        print(json.dumps(report, indent=2, default=str))
    elif args.loop:
        run_loop(execute=args.execute)


if __name__ == "__main__":
    main()
