# -*- coding: utf-8 -*-
"""News AI → short-term paper buys for US tech stocks.

Scorer: SiliconFlow DeepSeek V4-Flash by default; V4-Pro for SEC
high-signal filings and Flash edge confidence (0.65–0.75).

Defaults (overridable via env):
  hold ~24h (model may set 6-48h)
  stops: AI SL/TP hard-clamped into ATR band (~0.8–1.2×1.5ATR, 2.5–7% / RR)
  min confidence 0.68
  max 8 concurrent news positions
  ~3% equity per name (capped)
  event-driven score wake on WS/SEC push (INTERVAL is max sleep, ~15s)
  score when closed; queue buys → premarket DAY limit (extended_hours)
  limit = last+aggress (near-open bump), capped at chase ceiling; reprice up
  no market chase at the open for queued intents
  chase gate: skip if entry already >MAX_ENTRY_VS_NEWS_PCT vs news/prev-close
  score up to 16 fresh items/cycle in parallel (non-earnings first)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.portfolio.sleeve import (
    UNASSIGNED,
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
from core.trade_log import (
    ensure_strategy,
    recent_strategy_lots,
    record_entry,
    record_exit,
    reopen_stale_journal,
)

from .broker import (
    get_broker,
    in_premarket,
    market_open,
    position_map,
    seconds_until_open,
)
from .dedupe import XDUPE
from .news_feed import bootstrap_inbox, fetch_news
from .news_inbox import INBOX, wait_scoring_wake
from .risk import (
    MAX_ENTRY_VS_NEWS_PCT,
    MAX_NEWS_AGE_SEC,
    chase_ref_price,
    chase_skip_reason,
    entry_vs_ref_pct,
    lag_breakdown,
    news_age_sec,
    news_catalyst_type,
    prioritize_news,
    resolve_exits,
)
from .score import (
    edge_band,
    escalate_pro,
    pro_model,
    score_model,
    score_news_batch,
    score_timeout_sec,
)
from .state import State
from .universe import TECH_UNIVERSE

STRATEGY_ID = os.environ.get("NEWS_TRADER_STRATEGY_ID", "news-trader")
STRATEGY_NAME = "News AI 短线"

STORE = Path(os.environ.get("NEWS_TRADER_STORE", "/app/data/store/news_trader"))
# Max sleep between cycles; WS/SEC wake early via wait_scoring_wake.
INTERVAL = int(os.environ.get("NEWS_TRADER_INTERVAL_SEC", "15"))
OFF_HOURS_INTERVAL = int(os.environ.get("NEWS_TRADER_OFF_HOURS_SEC", "45"))
SCORE_WHEN_CLOSED = os.environ.get("NEWS_TRADER_SCORE_WHEN_CLOSED", "1") == "1"
MIN_CONF = float(os.environ.get("NEWS_TRADER_MIN_CONF", "0.68"))
MAX_POS = int(os.environ.get("NEWS_TRADER_MAX_POSITIONS", "8"))
MAX_BUYS_PER_CYCLE = int(os.environ.get("NEWS_TRADER_MAX_BUYS", "3"))
PCT_EQUITY = float(os.environ.get("NEWS_TRADER_PCT_EQUITY", "0.03"))
MAX_NOTIONAL = float(os.environ.get("NEWS_TRADER_MAX_NOTIONAL", "4000"))
MIN_NOTIONAL = float(os.environ.get("NEWS_TRADER_MIN_NOTIONAL", "200"))
HOURS_BACK = float(os.environ.get("NEWS_TRADER_HOURS_BACK", "18"))
DRY_RUN = os.environ.get("NEWS_TRADER_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
ONCE = os.environ.get("NEWS_TRADER_ONCE", "").strip().lower() in {"1", "true", "yes"}
SCORE_LIMIT = int(os.environ.get("NEWS_TRADER_SCORE_LIMIT", "16"))
SCORE_WORKERS = max(1, int(os.environ.get("NEWS_TRADER_SCORE_WORKERS", "4")))
USE_NEWS_WS = os.environ.get("NEWS_TRADER_NEWS_WS", "1") == "1"
# If WS silent this long, seed inbox via REST that cycle (does not change score cadence)
WS_STALE_SEC = float(os.environ.get("NEWS_TRADER_WS_STALE_SEC", "900"))
# When closed with pending buys, nap at most this long before open.
PREOPEN_POLL_SEC = float(os.environ.get("NEWS_TRADER_PREOPEN_POLL_SEC", "10"))
# Earnings throttle (audit HARD FLAG: earnings concentrate losses).
EARNINGS_MIN_CONF = float(os.environ.get("NEWS_TRADER_EARNINGS_MIN_CONF", "0.78"))
EARNINGS_MAX_OPEN = int(os.environ.get("NEWS_TRADER_EARNINGS_MAX_OPEN", "1"))
EARNINGS_QUEUE_CLOSED = os.environ.get(
    "NEWS_TRADER_EARNINGS_QUEUE_CLOSED", "0"
).strip().lower() in {"1", "true", "yes"}
# Drop queued buys once news (or score time) is older than this.
PENDING_MAX_AGE_SEC = float(
    os.environ.get("NEWS_TRADER_PENDING_MAX_AGE_SEC", str(MAX_NEWS_AGE_SEC))
)
# Premarket: DAY limit + extended_hours (no market chase at the open).
PREMARKET_LIMITS = os.environ.get("NEWS_TRADER_PREMARKET_LIMITS", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
# Buy-limit aggressiveness toward chase ceiling (bps above last). 0 = join last.
# Shave is legacy (under last); prefer aggress. Near open we bump aggress.
PREMARKET_LIMIT_AGGRESS_BPS = float(
    os.environ.get("NEWS_TRADER_PREMARKET_LIMIT_AGGRESS_BPS", "25")
)
PREMARKET_LIMIT_SHAVE_BPS = float(os.environ.get("NEWS_TRADER_PREMARKET_LIMIT_SHAVE_BPS", "0"))
PREMARKET_NEAR_OPEN_MIN = float(os.environ.get("NEWS_TRADER_PREMARKET_NEAR_OPEN_MIN", "45"))
PREMARKET_NEAR_OPEN_AGGRESS_BPS = float(
    os.environ.get("NEWS_TRADER_PREMARKET_NEAR_OPEN_AGGRESS_BPS", "80")
)
# Reprice resting limit upward when ideal lim moves this many bps (still ≤ ceiling).
PREMARKET_REPRICE_BPS = float(os.environ.get("NEWS_TRADER_PREMARKET_REPRICE_BPS", "15"))
PREMARKET_REPRICE_MIN_SEC = float(os.environ.get("NEWS_TRADER_PREMARKET_REPRICE_MIN_SEC", "90"))


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _append_jsonl(name: str, row: dict) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / name
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _qty_for(equity: float, price: float) -> int:
    notional = min(equity * PCT_EQUITY, MAX_NOTIONAL)
    notional = max(notional, 0.0)
    if price <= 0 or notional < MIN_NOTIONAL:
        return 0
    qty = int(math.floor(notional / price))
    return max(qty, 0)


def _is_earnings(catalyst: str | None) -> bool:
    return str(catalyst or "").lower() == "earnings"


def _earnings_open_count(open_trades: dict) -> int:
    return sum(
        1 for m in open_trades.values() if _is_earnings((m or {}).get("catalyst_type"))
    )


def _earnings_gate(
    *,
    catalyst: str,
    confidence: float,
    open_trades: dict,
    pending_buys: dict | None = None,
    symbol: str,
    for_queue: bool = False,
) -> str | None:
    """Return skip reason for earnings throttle, else None."""
    if not _is_earnings(catalyst):
        return None
    if confidence < EARNINGS_MIN_CONF:
        return f"earnings conf {confidence:.2f} < {EARNINGS_MIN_CONF:.2f}"
    # Already holding this name — allow manage/exit path only; no second lot.
    if symbol in open_trades:
        return "earnings already_open"
    n_earn = _earnings_open_count(open_trades)
    pending = pending_buys or {}
    n_earn += sum(
        1
        for s, row in pending.items()
        if s != symbol and _is_earnings((row or {}).get("catalyst_type"))
    )
    if n_earn >= EARNINGS_MAX_OPEN:
        return f"earnings max_open {n_earn}>={EARNINGS_MAX_OPEN}"
    if for_queue and not market_open() and not EARNINGS_QUEUE_CLOSED:
        return "earnings no_queue_when_closed"
    return None


def _expire_pending_buys(
    state: State,
    *,
    positions: dict,
    open_trades: dict,
    broker=None,
    sleeve=None,
) -> int:
    """Drop queued buys that are too old or already held. Returns drop count."""
    pending = dict(state.data.get("pending_buys") or {})
    if not pending:
        return 0
    now = datetime.now(timezone.utc)
    dropped = 0
    for symbol, intent in list(pending.items()):
        reason = None
        armed = bool(intent.get("limit_order_id"))
        # Filled but not yet booked — leave for poll (do not cancel/drop).
        if armed and symbol in positions and symbol not in open_trades:
            continue
        # Resting DAY limit: keep until fill / ran_away / broker cancel — not news age.
        if armed and symbol not in positions and symbol not in open_trades:
            continue
        if symbol in positions or symbol in open_trades:
            reason = "already_held"
        else:
            age = news_age_sec(intent.get("news_created_at"), now=now)
            if age is None:
                age = news_age_sec(intent.get("scored_at"), now=now)
            if age is not None and age > PENDING_MAX_AGE_SEC:
                reason = f"expired age={age:.0f}s > {PENDING_MAX_AGE_SEC:.0f}s"
            elif _is_earnings(intent.get("catalyst_type")) and not EARNINGS_QUEUE_CLOSED:
                # Stale earnings intents should not survive overnight into RTH.
                scored_age = news_age_sec(intent.get("scored_at"), now=now)
                if scored_age is not None and scored_age > min(PENDING_MAX_AGE_SEC, 6 * 3600):
                    reason = f"earnings_pending_stale scored_age={scored_age:.0f}s"
        if reason:
            _drop_pending(
                state, symbol, reason=reason, broker=broker, sleeve=sleeve
            )
            dropped += 1
    return dropped


def _adopt_orphans(sleeve, broker, state: State) -> int:
    """Re-claim __unassigned__ lots that belong to news-trader; restore journal/state."""
    if sleeve is None:
        return 0
    positions = position_map(broker)
    open_trades = dict(state.data.get("open_trades") or {})
    adopted = 0

    # Candidates: local open_trades missing a sleeve claim, plus recent journal lots.
    candidates: dict[str, dict[str, Any]] = {}
    for sym, meta in open_trades.items():
        candidates[str(sym).upper()] = dict(meta or {})
    for lot in recent_strategy_lots(STRATEGY_ID, lookback_days=7):
        sym = str(lot["symbol"]).upper()
        candidates.setdefault(
            sym,
            {
                "symbol": sym,
                "qty": lot.get("qty"),
                "entry_price": lot.get("entry_price"),
                "thesis": lot.get("signal_reason") or "",
                "catalyst_type": "other",
                "hold_hours": 24,
                "entered_at": (
                    lot["opened_at"].isoformat()
                    if hasattr(lot.get("opened_at"), "isoformat")
                    else str(lot.get("opened_at") or "")
                ),
            },
        )

    for sym, meta in candidates.items():
        if sleeve.qty_of(STRATEGY_ID, sym) > 0:
            continue
        bp = positions.get(sym)
        if not bp or float(bp.get("qty") or 0) <= 0:
            continue
        owners = sleeve.owners_of(sym)
        foreign = [
            p for p in owners if p.strategy_id not in (STRATEGY_ID, UNASSIGNED)
        ]
        orphan_qty = sleeve.qty_of(UNASSIGNED, sym)
        if foreign and orphan_qty <= 0:
            continue
        want = float(orphan_qty or bp.get("qty") or meta.get("qty") or 0)
        if want <= 0:
            continue
        px = float(
            meta.get("entry_price")
            or bp.get("avg_entry_price")
            or bp.get("current_price")
            or 0
        )
        took = sleeve.adopt_orphan(STRATEGY_ID, sym, want, px, source="adopt_orphan")
        if took <= 0:
            continue
        adopted += 1
        reopened = reopen_stale_journal(
            STRATEGY_ID, sym, qty=took, entry_price=px or None
        )
        if sym not in open_trades:
            open_trades[sym] = {
                "symbol": sym,
                "qty": took,
                "entry_price": px,
                "entered_at": meta.get("entered_at")
                or datetime.now(timezone.utc).isoformat(),
                "catalyst_type": meta.get("catalyst_type") or "other",
                "confidence": meta.get("confidence"),
                "thesis": meta.get("thesis") or meta.get("signal_reason") or "",
                "hold_hours": meta.get("hold_hours") or 24,
                "take_profit_pct": meta.get("take_profit_pct") or 9.0,
                "stop_loss_pct": meta.get("stop_loss_pct") or 5.0,
                "adopted": True,
            }
        else:
            open_trades[sym]["qty"] = took
            if px > 0:
                open_trades[sym]["entry_price"] = px
        _log(
            f"ADOPT {sym} qty={took:.0f} @ {px:.4f} "
            f"reopen_journal={reopened} (was unassigned/unowned)"
        )

    if adopted:
        state.data["open_trades"] = open_trades
        state.save()
        # Clear pending for names we just re-adopted.
        pending = dict(state.data.get("pending_buys") or {})
        for sym in list(pending):
            if sleeve.qty_of(STRATEGY_ID, sym) > 0:
                _drop_pending(state, sym, reason="adopted_held")
    return adopted


def manage_exits(broker, state: State, sleeve) -> None:
    positions = position_map(broker)
    open_trades = dict(state.data.get("open_trades") or {})
    now = datetime.now(timezone.utc)
    for symbol, meta in list(open_trades.items()):
        pos = positions.get(symbol)
        if not pos:
            # Bracket/manual close beat us to it — settle the journal from the
            # broker fill before dropping local state, or the exit price is lost.
            meta["exit_reason"] = meta.get("exit_reason") or "position_gone"
            meta["exited_at"] = now.isoformat()
            if sleeve is not None:
                owned = sleeve.qty_of(STRATEGY_ID, symbol)
                if owned > 0:
                    from core.portfolio.sleeve import LedgerChange
                    from core.portfolio.settle import settle_broker_exits

                    entered = meta.get("entered_at")
                    since = None
                    if entered:
                        try:
                            since = datetime.fromisoformat(
                                str(entered).replace("Z", "+00:00")
                            )
                        except Exception:
                            since = None
                    change = LedgerChange(
                        "closed",
                        STRATEGY_ID,
                        symbol,
                        owned,
                        0.0,
                        float(meta.get("entry_price") or 0),
                        since,
                    )
                    settled = settle_broker_exits(broker, [change], since=since)
                    if settled:
                        _log(
                            f"CLEAR {symbol} settled @ "
                            f"${settled[0]['exit_price']:.2f} "
                            f"({settled[0]['journal_rows']} row(s))"
                        )
                    else:
                        _log(f"CLEAR {symbol} no fill found — journal left for mark_stale")
                    sleeve.release(STRATEGY_ID, symbol)
                else:
                    _log(f"CLEAR {symbol} (no position, nothing owned)")
            else:
                _log(f"CLEAR {symbol} (no position, ledger down)")
            state.data.setdefault("history", []).append(meta)
            del open_trades[symbol]
            continue

        entry = float(meta.get("entry_price") or pos.get("avg_entry_price") or 0)
        px = float(pos.get("current_price") or 0)
        pnl_pct = ((px / entry) - 1.0) * 100.0 if entry > 0 else float(pos.get("unrealized_plpc") or 0) * 100.0
        tp = float(meta.get("take_profit_pct") or 5.5)
        sl = float(meta.get("stop_loss_pct") or 3.5)
        hold_h = float(meta.get("hold_hours") or 24)
        entered = meta.get("entered_at")
        aged_h = 0.0
        if entered:
            try:
                t0 = datetime.fromisoformat(entered.replace("Z", "+00:00"))
                aged_h = (now - t0).total_seconds() / 3600.0
            except Exception:
                aged_h = 0.0

        reason = None
        if pnl_pct >= tp:
            reason = f"take_profit {pnl_pct:.2f}% >= {tp}%"
        elif pnl_pct <= -sl:
            reason = f"stop_loss {pnl_pct:.2f}% <= -{sl}%"
        elif aged_h >= hold_h:
            reason = f"time_exit {aged_h:.1f}h >= {hold_h}h"

        if not reason:
            continue
        if not market_open():
            _log(f"EXIT wait market closed {symbol} ({reason})")
            continue
        # Sell only the shares this sleeve owns. close_position() would dump the
        # whole broker position, including any other strategy's shares, and
        # meta["qty"] is our own intent rather than proof of ownership — with no
        # ledger to check it against there is no safe quantity to sell.
        if sleeve is None:
            _log(f"EXIT blocked {symbol}: ledger unavailable, cannot prove ownership")
            continue
        owned = sleeve.qty_of(STRATEGY_ID, symbol)
        sell_qty = int(min(owned, float(pos.get("qty") or 0)))
        if sell_qty <= 0:
            _log(f"EXIT skip {symbol} nothing owned by this sleeve")
            continue

        if DRY_RUN:
            _log(f"DRY EXIT {symbol} qty={sell_qty} {reason} pnl={pnl_pct:.2f}%")
        else:
            try:
                order = broker.unlock_and_sell(symbol, sell_qty)
                fill = await_fill(broker, {**order, "qty": sell_qty})
                _log(
                    f"EXIT {symbol} {reason} order={order.get('id')} "
                    f"{fill.describe()} pnl={pnl_pct:.2f}%"
                )
                if fill.dead:
                    _log(f"EXIT {symbol} nothing sold, position kept")
                    continue
                if fill.filled:
                    sell_qty = int(fill.filled_qty)
                px = fill.price_or(px)
                sleeve.reduce(STRATEGY_ID, symbol, sell_qty)
                record_exit(
                    strategy_id=STRATEGY_ID,
                    strategy_name=STRATEGY_NAME,
                    symbol=symbol,
                    qty=sell_qty,
                    price=px,
                    order_id=order.get("id"),
                    status=fill.status,
                    reason=reason,
                )
                _append_jsonl(
                    "trades.jsonl",
                    {
                        "ts": now.isoformat(),
                        "side": "sell",
                        "symbol": symbol,
                        "qty": sell_qty,
                        "reason": reason,
                        "pnl_pct": pnl_pct,
                        "order": order,
                        "meta": meta,
                    },
                )
                _append_jsonl(
                    "outcomes.jsonl",
                    {
                        "ts": now.isoformat(),
                        "symbol": symbol,
                        "pnl_pct": pnl_pct,
                        "reason": reason,
                        "confidence": meta.get("confidence"),
                        "catalyst_type": meta.get("catalyst_type"),
                        "hold_hours": meta.get("hold_hours"),
                        "ingestion_lag_sec": meta.get("ingestion_lag_sec"),
                        "decision_lag_sec": meta.get("decision_lag_sec"),
                        "news_age_sec": meta.get("news_age_sec"),
                        "entry_vs_news_pct": meta.get("entry_vs_news_pct"),
                        "stop_loss_pct": meta.get("stop_loss_pct"),
                        "take_profit_pct": meta.get("take_profit_pct"),
                        "exit_source": meta.get("exit_source"),
                        "news_id": meta.get("news_id"),
                    },
                )
            except Exception as exc:
                _log(f"EXIT fail {symbol}: {exc}")
                continue
        meta["exit_reason"] = reason
        meta["exited_at"] = now.isoformat()
        meta["exit_pnl_pct"] = pnl_pct
        state.data.setdefault("history", []).append(meta)
        del open_trades[symbol]

    state.data["open_trades"] = open_trades
    state.save()


def _queue_pending_buy(
    state: State,
    *,
    symbol: str,
    item: dict[str, Any],
    score: dict[str, Any],
    catalyst: str,
) -> None:
    """Persist a scored buy for RTH open (replaces same-symbol weaker intents)."""
    pending = dict(state.data.get("pending_buys") or {})
    conf = float(score.get("confidence") or 0)
    prev = pending.get(symbol) or {}
    if prev and float(prev.get("confidence") or 0) > conf:
        _log(
            f"QUEUE keep {symbol} existing conf={prev.get('confidence')} "
            f"> new {conf:.2f}"
        )
        return
    pending[symbol] = {
        "symbol": symbol,
        "news_id": str(item.get("id") or ""),
        "headline": item.get("headline"),
        "news_created_at": item.get("created_at"),
        "received_at": item.get("received_at"),
        "feed": item.get("feed"),
        "news_source": item.get("source"),
        "catalyst_type": catalyst,
        "confidence": conf,
        "thesis": score.get("thesis") or "",
        "hold_hours": score.get("hold_hours"),
        "ai_take_profit_pct": score.get("take_profit_pct"),
        "ai_stop_loss_pct": score.get("stop_loss_pct"),
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    state.data["pending_buys"] = pending
    state.save()
    _log(
        f"QUEUE {symbol} until open conf={conf:.2f} "
        f"news_id={pending[symbol]['news_id'][:12]}… "
        f"[{catalyst}]"
    )


def _drop_pending(
    state: State,
    symbol: str,
    reason: str,
    *,
    broker=None,
    sleeve=None,
) -> None:
    pending = dict(state.data.get("pending_buys") or {})
    if symbol not in pending:
        return
    row = pending.pop(symbol)
    state.data["pending_buys"] = pending
    state.save()
    oid = row.get("limit_order_id")
    if broker is not None and oid:
        try:
            broker.cancel_open_orders(symbol)
            _log(f"QUEUE cancel resting {symbol} order={oid}")
        except Exception as exc:
            _log(f"QUEUE cancel fail {symbol}: {exc}")
    # Release sleeve lock if we claimed for a resting premarket limit and never filled.
    if (
        sleeve is not None
        and oid
        and sleeve.qty_of(STRATEGY_ID, symbol) > 0
        and symbol not in (state.data.get("open_trades") or {})
    ):
        sleeve.release(STRATEGY_ID, symbol)
    _append_jsonl(
        "signals.jsonl",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "news_id": row.get("news_id"),
            "symbol": symbol,
            "headline": row.get("headline"),
            "catalyst_type": row.get("catalyst_type"),
            "skipped": "pending_dropped",
            "reason": reason,
            "confidence": row.get("confidence"),
            "limit_order_id": oid,
        },
    )
    _log(f"QUEUE drop {symbol}: {reason}")


def _premarket_aggress_bps() -> float:
    """Bps above last for buy limits; higher in the final pre-open window."""
    aggress = PREMARKET_LIMIT_AGGRESS_BPS
    until = seconds_until_open()
    if (
        until is not None
        and 0 < until <= PREMARKET_NEAR_OPEN_MIN * 60.0
        and PREMARKET_NEAR_OPEN_AGGRESS_BPS > aggress
    ):
        aggress = PREMARKET_NEAR_OPEN_AGGRESS_BPS
    return max(0.0, float(aggress))


def _buy_limit_price(
    symbol: str,
    *,
    last: float,
    news_created_at: Any,
) -> tuple[float | None, dict[str, Any]]:
    """Limit for a non-chase buy: last + aggress, capped at chase ceiling.

    Returns (limit_or_None, audit). None ⇒ already above chase ceiling.
    """
    ref = chase_ref_price(symbol, news_created_at)
    ref_px = float(ref.get("price_at_news") or 0) or None
    ceiling = None
    if ref_px and ref_px > 0:
        ceiling = ref_px * (1.0 + MAX_ENTRY_VS_NEWS_PCT / 100.0)
    aggress_bps = _premarket_aggress_bps()
    audit = {
        "price_at_news": ref_px,
        "ref_source": ref.get("ref_source"),
        "chase_ceiling": round(ceiling, 4) if ceiling else None,
        "last": last,
        "aggress_bps": aggress_bps,
    }
    if last <= 0:
        return None, audit
    if ceiling is not None and last > ceiling + 1e-9:
        audit["entry_vs_news_pct"] = entry_vs_ref_pct(last, ref_px)
        return None, audit
    lim = last * (1.0 + aggress_bps / 10000.0)
    if ceiling is not None:
        lim = min(lim, ceiling)
    shave = PREMARKET_LIMIT_SHAVE_BPS / 10000.0
    if shave > 0:
        lim = lim * (1.0 - shave)
    # Never bid below last after aggress/shave unless ceiling forces it.
    if ceiling is None or last <= ceiling + 1e-9:
        lim = max(lim, last)
        if ceiling is not None:
            lim = min(lim, ceiling)
    lim = round(max(lim, 0.01), 2)
    audit["limit_price"] = lim
    audit["entry_vs_news_pct"] = entry_vs_ref_pct(lim, ref_px) if ref_px else None
    return lim, audit


def _book_filled_buy(
    broker,
    state: State,
    sleeve,
    *,
    symbol: str,
    qty: int,
    price: float,
    equity: float,
    intent: dict[str, Any],
    order: dict[str, Any],
    exits: dict[str, Any],
    fill_status: str,
    filled_qty: float,
    lags: dict[str, Any],
    chase_audit: dict[str, Any],
    entry_style: str,
) -> None:
    """Persist fill into journal + open_trades + trades.jsonl; arm OCO if needed."""
    if sleeve is not None:
        sleeve.claim(STRATEGY_ID, symbol, qty, price, coexist=True)
    if not DRY_RUN and entry_style in {"premarket_limit", "rth_limit"}:
        # Plain limit fill — attach broker OCO (bracket not allowed in extended hours).
        try:
            sl_pct = float(exits["stop_loss_pct"])
            tp_pct = float(exits["take_profit_pct"])
            stop = round(price * (1.0 - sl_pct / 100.0), 2)
            take = round(price * (1.0 + tp_pct / 100.0), 2)
            if 0 < stop < price < take:
                broker.oco_exit(symbol, int(qty), take, stop)
                _log(f"BUY {symbol} armed OCO SL=${stop} TP=${take}")
        except Exception as exc:
            _log(f"BUY {symbol} OCO arm fail: {exc}")

    record_entry(
        strategy_id=STRATEGY_ID,
        strategy_name=STRATEGY_NAME,
        symbol=symbol,
        qty=qty,
        price=price,
        order_id=order.get("id"),
        status=fill_status,
        reason=(intent.get("thesis") or "news signal")[:400],
        filled_qty=filled_qty,
        position_value_pct=round(qty * price / equity * 100, 2) if equity else None,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    meta = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": price,
        "entered_at": now_iso,
        "news_id": intent.get("news_id"),
        "headline": intent.get("headline"),
        "news_created_at": intent.get("news_created_at"),
        "received_at": intent.get("received_at"),
        "feed": intent.get("feed"),
        "news_source": intent.get("news_source"),
        "catalyst_type": intent.get("catalyst_type") or "other",
        "confidence": intent.get("confidence"),
        "thesis": intent.get("thesis") or "",
        "hold_hours": intent.get("hold_hours"),
        "take_profit_pct": exits["take_profit_pct"],
        "stop_loss_pct": exits["stop_loss_pct"],
        "atr_pct": exits.get("atr_pct"),
        "exit_source": exits.get("exit_source"),
        "ingestion_lag_sec": lags.get("ingestion_lag_sec"),
        "decision_lag_sec": lags.get("decision_lag_sec"),
        "news_age_sec": lags.get("news_age_sec"),
        "price_at_news": chase_audit.get("price_at_news"),
        "entry_vs_news_pct": chase_audit.get("entry_vs_news_pct"),
        "ref_source": chase_audit.get("ref_source"),
        "limit_price": chase_audit.get("limit_price") or intent.get("limit_price"),
        "entry_style": entry_style,
        "order_id": order.get("id"),
        "dry_run": DRY_RUN,
        "from_pending": bool(intent.get("from_pending")),
    }
    open_trades = dict(state.data.get("open_trades") or {})
    open_trades[symbol] = meta
    state.data["open_trades"] = open_trades
    pending = dict(state.data.get("pending_buys") or {})
    pending.pop(symbol, None)
    state.data["pending_buys"] = pending
    state.save()
    _append_jsonl(
        "trades.jsonl",
        {
            "ts": now_iso,
            "side": "buy",
            "symbol": symbol,
            "qty": qty,
            "order": order,
            "meta": meta,
        },
    )
    _log(f"BUY booked {symbol} qty={qty} @ {price:.4f} style={entry_style}")


def _arm_premarket_limit(
    broker,
    state: State,
    sleeve,
    *,
    symbol: str,
    equity: float,
    positions: dict,
    open_trades: dict,
    intent: dict[str, Any],
) -> str:
    """Place DAY+extended_hours limit for a queued intent. Returns armed|skip|held."""
    if not PREMARKET_LIMITS or not in_premarket():
        return "skip"
    if symbol in positions or symbol in open_trades:
        return "held"
    pending = dict(state.data.get("pending_buys") or {})
    row = dict(pending.get(symbol) or intent)
    if row.get("limit_order_id"):
        return "armed"

    last = _last_price(symbol)
    qty = _qty_for(equity, last) if last else 0
    if qty <= 0:
        _log(f"PREMARKET SIZE skip {symbol} last={last}")
        return "skip"

    lim, audit = _buy_limit_price(
        symbol, last=last, news_created_at=row.get("news_created_at")
    )
    if lim is None:
        _log(
            f"PREMARKET CHASE skip {symbol}: last={last} "
            f"> ceiling={audit.get('chase_ceiling')} "
            f"vs_news={audit.get('entry_vs_news_pct')}%"
        )
        _drop_pending(
            state, symbol, reason="premarket_chase", broker=broker, sleeve=sleeve
        )
        return "skip"

    age = news_age_sec(row.get("news_created_at"))
    if age is not None and age > PENDING_MAX_AGE_SEC:
        _drop_pending(
            state,
            symbol,
            reason=f"premarket_stale age={age:.0f}s",
            broker=broker,
            sleeve=sleeve,
        )
        return "skip"

    exits = resolve_exits(
        symbol,
        ai_sl_pct=float(row.get("ai_stop_loss_pct") or 0) or None,
        ai_tp_pct=float(row.get("ai_take_profit_pct") or 0) or None,
    )
    if DRY_RUN:
        _log(f"DRY PREMARKET LIMIT {symbol} qty={qty} lim={lim}")
        row["limit_order_id"] = "dry-run"
        row["limit_price"] = lim
        row["limit_placed_at"] = datetime.now(timezone.utc).isoformat()
        pending[symbol] = row
        state.data["pending_buys"] = pending
        state.save()
        return "armed"

    if sleeve is None or not sleeve.claim(STRATEGY_ID, symbol, qty, lim):
        # Keep pending — capacity may free after time exits / other fills.
        _log(f"PREMARKET CLAIM defer {symbol}")
        return "skip"
    cap = check_combined_buy(broker, symbol, float(qty) * float(lim), ref_price=float(lim))
    if not cap.allowed:
        sleeve.release(STRATEGY_ID, symbol)
        _log(f"PREMARKET CAP defer {symbol} — {cap.reason}")
        return "skip"
    acct_cap = check_account_buy(broker, float(qty) * float(lim))
    if not acct_cap.allowed:
        sleeve.release(STRATEGY_ID, symbol)
        _log(f"PREMARKET ACCOUNT CAP defer {symbol} — {acct_cap.reason}")
        return "skip"
    try:
        order = broker.limit_order(
            symbol, qty, "buy", lim, extended_hours=True, time_in_force="day"
        )
    except Exception as exc:
        sleeve.release(STRATEGY_ID, symbol)
        _log(f"PREMARKET LIMIT fail {symbol}: {exc}")
        return "skip"

    row.update(
        {
            "limit_order_id": order.get("id"),
            "limit_price": lim,
            "limit_qty": qty,
            "limit_placed_at": datetime.now(timezone.utc).isoformat(),
            "exits_snapshot": {
                "stop_loss_pct": exits["stop_loss_pct"],
                "take_profit_pct": exits["take_profit_pct"],
            },
            **{k: audit.get(k) for k in ("price_at_news", "ref_source", "chase_ceiling")},
        }
    )
    pending[symbol] = row
    state.data["pending_buys"] = pending
    state.save()
    _log(
        f"PREMARKET LIMIT {symbol} qty={qty} lim={lim} "
        f"order={order.get('id')} ceiling={audit.get('chase_ceiling')} "
        f"[{row.get('catalyst_type')}]"
    )
    _append_jsonl(
        "signals.jsonl",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "news_id": row.get("news_id"),
            "symbol": symbol,
            "catalyst_type": row.get("catalyst_type"),
            "confidence": row.get("confidence"),
            "action": "premarket_limit",
            "limit_price": lim,
            "order_id": order.get("id"),
            **audit,
        },
    )
    return "armed"


def _poll_pending_limits(
    broker,
    state: State,
    sleeve,
    *,
    equity: float,
    positions: dict,
    open_trades: dict,
) -> int:
    """Settle filled premarket/RTH limits; cancel chased resting orders. Returns fills."""
    pending = dict(state.data.get("pending_buys") or {})
    if not pending:
        return 0
    filled_n = 0
    for symbol, intent in list(pending.items()):
        oid = intent.get("limit_order_id")
        if not oid:
            continue
        if symbol in positions and symbol in open_trades:
            # Already booked (e.g. adopt) — clear pending residue.
            pending.pop(symbol, None)
            continue
        try:
            snap = {"id": oid, "qty": intent.get("limit_qty") or 0, "status": "new"}
            if oid != "dry-run":
                snap = broker.get_order(oid)
            fill = await_fill(broker, snap, timeout_sec=0.0)
        except Exception as exc:
            _log(f"POLL limit {symbol} fail: {exc}")
            continue

        if fill.filled:
            qty = int(fill.filled_qty)
            price = fill.price_or(float(intent.get("limit_price") or 0))
            snap_ex = intent.get("exits_snapshot") or {}
            exits = resolve_exits(
                symbol,
                ai_sl_pct=float(intent.get("ai_stop_loss_pct") or snap_ex.get("stop_loss_pct") or 0)
                or None,
                ai_tp_pct=float(intent.get("ai_take_profit_pct") or snap_ex.get("take_profit_pct") or 0)
                or None,
            )
            lags = lag_breakdown(
                news_created_at=intent.get("news_created_at"),
                received_at=intent.get("received_at"),
            )
            chase_audit = {
                "price_at_news": intent.get("price_at_news"),
                "entry_vs_news_pct": entry_vs_ref_pct(
                    price, float(intent.get("price_at_news") or 0) or None
                ),
                "ref_source": intent.get("ref_source"),
                "limit_price": intent.get("limit_price"),
            }
            _book_filled_buy(
                broker,
                state,
                sleeve,
                symbol=symbol,
                qty=qty,
                price=price,
                equity=equity,
                intent={**intent, "from_pending": True},
                order=snap if isinstance(snap, dict) else {"id": oid},
                exits=exits,
                fill_status=fill.status,
                filled_qty=fill.filled_qty,
                lags=lags,
                chase_audit=chase_audit,
                entry_style="premarket_limit",
            )
            filled_n += 1
            open_trades.update(state.data.get("open_trades") or {})
            continue

        if fill.dead:
            if sleeve is not None:
                sleeve.release(STRATEGY_ID, symbol)
            _drop_pending(
                state,
                symbol,
                reason=f"limit_{fill.status}",
                broker=broker,
                sleeve=sleeve,
            )
            continue

        # Still resting — if last already above chase ceiling, cancel (no chase).
        last = _last_price(symbol)
        lim, audit = _buy_limit_price(
            symbol, last=last, news_created_at=intent.get("news_created_at")
        )
        if lim is None and last > 0:
            _log(
                f"PREMARKET cancel {symbol}: ran away last={last} "
                f"ceiling={audit.get('chase_ceiling')}"
            )
            _drop_pending(
                state, symbol, reason="ran_away_chase", broker=broker, sleeve=sleeve
            )
            continue

        # Walk limit up toward ceiling when last rises (still no chase past ceiling).
        if (
            lim is not None
            and in_premarket()
            and PREMARKET_REPRICE_BPS > 0
            and last > 0
        ):
            old_lim = float(intent.get("limit_price") or 0)
            placed_at = intent.get("limit_placed_at") or intent.get("limit_repriced_at")
            age_placed = news_age_sec(placed_at) if placed_at else None
            min_up = old_lim * (1.0 + PREMARKET_REPRICE_BPS / 10000.0) if old_lim > 0 else 0.0
            if (
                old_lim > 0
                and lim >= min_up - 1e-9
                and (age_placed is None or age_placed >= PREMARKET_REPRICE_MIN_SEC)
            ):
                _log(
                    f"PREMARKET REPRICE {symbol}: {old_lim:.2f} → {lim:.2f} "
                    f"last={last} ceiling={audit.get('chase_ceiling')} "
                    f"aggress={audit.get('aggress_bps')}bps"
                )
                try:
                    if oid != "dry-run":
                        broker.cancel_open_orders(symbol)
                except Exception as exc:
                    _log(f"PREMARKET REPRICE cancel fail {symbol}: {exc}")
                    continue
                if sleeve is not None:
                    sleeve.release(STRATEGY_ID, symbol)
                pending = dict(state.data.get("pending_buys") or {})
                row = dict(pending.get(symbol) or intent)
                for k in (
                    "limit_order_id",
                    "limit_price",
                    "limit_qty",
                    "limit_placed_at",
                ):
                    row.pop(k, None)
                row["limit_repriced_at"] = datetime.now(timezone.utc).isoformat()
                pending[symbol] = row
                state.data["pending_buys"] = pending
                state.save()
                _arm_premarket_limit(
                    broker,
                    state,
                    sleeve,
                    symbol=symbol,
                    equity=equity,
                    positions=positions,
                    open_trades=open_trades,
                    intent=row,
                )
    return filled_n


def _try_buy(
    broker,
    state: State,
    sleeve,
    *,
    symbol: str,
    equity: float,
    positions: dict,
    open_trades: dict,
    n_open: int,
    buys: int,
    intent: dict[str, Any],
) -> str:
    """Place one buy from live score or queued intent.

    Returns: "bought" | "retry" (keep pending) | "drop" (discard intent).
    """
    if n_open + buys >= MAX_POS:
        _log(f"max positions reached — skip {symbol}")
        return "retry"
    if buys >= MAX_BUYS_PER_CYCLE:
        return "retry"
    if symbol in positions or symbol in open_trades:
        _log(f"ALREADY IN {symbol}")
        return "drop"
    owner = sleeve.owner_of(symbol) if sleeve is not None else None
    if owner is not None:
        _log(f"OWNED BY {owner} — skip {symbol}")
        return "drop"

    price = _last_price(symbol)
    qty = _qty_for(equity, price) if price else 0
    if qty <= 0:
        _log(f"SIZE skip {symbol} price={price} equity={equity:.0f}")
        return "retry"

    news_created = intent.get("news_created_at")
    received_at = intent.get("received_at")
    catalyst = intent.get("catalyst_type") or "other"
    thesis = intent.get("thesis") or ""
    nid = str(intent.get("news_id") or "")

    decision_ts = datetime.now(timezone.utc)
    lags = lag_breakdown(
        news_created_at=news_created,
        received_at=received_at,
        decision_at=decision_ts,
    )
    skip, chase_audit = chase_skip_reason(
        entry_price=price,
        news_created_at=news_created,
        symbol=symbol,
        age_sec=lags.get("news_age_sec"),
    )
    if skip:
        _log(f"CHASE/STALE skip {symbol}: {skip}")
        _append_jsonl(
            "signals.jsonl",
            {
                "ts": decision_ts.isoformat(),
                "news_id": nid,
                "symbol": symbol,
                "headline": intent.get("headline"),
                "catalyst_type": catalyst,
                "skipped": "chase_or_stale",
                "reason": skip,
                **lags,
                **{k: v for k, v in chase_audit.items() if k != "news_age_sec"},
            },
        )
        return "drop"

    exits = resolve_exits(
        symbol,
        ai_sl_pct=float(intent.get("ai_stop_loss_pct") or 0) or None,
        ai_tp_pct=float(intent.get("ai_take_profit_pct") or 0) or None,
    )
    _log(
        f"EXITS {symbol} sl={exits['stop_loss_pct']}% "
        f"tp={exits['take_profit_pct']}% "
        f"band=[{exits.get('sl_lo_pct')}-{exits.get('sl_hi_pct')}/"
        f"{exits.get('tp_lo_pct')}-{exits.get('tp_hi_pct')}] "
        f"({exits['exit_source']} atr={exits.get('atr_pct')} "
        f"rr={exits.get('intended_rr')} "
        f"clamp_sl={exits.get('clamped_sl')} clamp_tp={exits.get('clamped_tp')}) "
        f"vs_news={chase_audit.get('entry_vs_news_pct')}%"
    )

    if DRY_RUN:
        order = {"id": "dry-run", "status": "dry"}
        fill_status = "dry"
        filled_qty = float(qty)
    else:
        if sleeve is None or not sleeve.claim(STRATEGY_ID, symbol, qty, price):
            _log(f"CLAIM fail {symbol} — another strategy took it")
            return "retry"
        cap = check_combined_buy(
            broker, symbol, float(qty) * float(price), ref_price=float(price)
        )
        if not cap.allowed:
            sleeve.release(STRATEGY_ID, symbol)
            _log(f"COMBINED CAP skip {symbol} — {cap.reason}")
            return "drop"
        acct_cap = check_account_buy(broker, float(qty) * float(price))
        if not acct_cap.allowed:
            sleeve.release(STRATEGY_ID, symbol)
            _log(f"ACCOUNT CAP skip {symbol} — {acct_cap.reason}")
            return "drop"
        # Bracket: manage_exits is too slow alone for news names.
        sl_pct = float(exits["stop_loss_pct"])
        tp_pct = float(exits["take_profit_pct"])
        signal_px = price
        try:
            order = broker.bracket_order(
                symbol,
                qty,
                price,
                stop_loss_pct=-sl_pct / 100.0,
                take_profit_pct=tp_pct / 100.0,
            )
        except Exception as exc:
            sleeve.release(STRATEGY_ID, symbol)
            _log(f"BUY fail {symbol}: {exc}")
            return "retry"

        fill = await_fill(broker, order)
        _log(f"BUY {symbol} {fill.describe()}")
        if fill.dead:
            sleeve.release(STRATEGY_ID, symbol)
            return "retry"
        if fill.filled:
            qty = int(fill.filled_qty)
            price = fill.price_or(price)
            sleeve.claim(STRATEGY_ID, symbol, qty, price)
            note = reprice_protection(
                broker,
                symbol,
                qty,
                price,
                -sl_pct / 100.0,
                tp_pct / 100.0,
                signal_price=signal_px,
            )
            if note:
                _log(f"BUY {symbol} {note}")
        fill_status = fill.status
        filled_qty = fill.filled_qty

        record_entry(
            strategy_id=STRATEGY_ID,
            strategy_name=STRATEGY_NAME,
            symbol=symbol,
            qty=qty,
            price=price,
            order_id=order.get("id"),
            status=fill_status,
            reason=(thesis or "news signal")[:400],
            filled_qty=filled_qty,
            position_value_pct=round(qty * price / equity * 100, 2) if equity else None,
        )

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    lags = lag_breakdown(
        news_created_at=news_created,
        received_at=received_at,
        decision_at=now_dt,
    )
    # Recompute chase audit at fill price for the trade log.
    _, fill_audit = chase_skip_reason(
        entry_price=price,
        news_created_at=news_created,
        symbol=symbol,
        age_sec=lags.get("news_age_sec"),
        max_entry_vs_news_pct=1e9,  # audit only — already gated pre-order
        max_news_age_sec=1e12,
    )
    meta = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": price,
        "entered_at": now_iso,
        "news_id": nid,
        "headline": intent.get("headline"),
        "news_created_at": news_created,
        "received_at": received_at,
        "feed": intent.get("feed"),
        "news_source": intent.get("news_source"),
        "catalyst_type": catalyst,
        "confidence": intent.get("confidence"),
        "thesis": thesis,
        "hold_hours": intent.get("hold_hours"),
        "take_profit_pct": exits["take_profit_pct"],
        "stop_loss_pct": exits["stop_loss_pct"],
        "atr_pct": exits.get("atr_pct"),
        "atr_ref_pct": exits.get("atr_ref_pct"),
        "sl_lo_pct": exits.get("sl_lo_pct"),
        "sl_hi_pct": exits.get("sl_hi_pct"),
        "tp_lo_pct": exits.get("tp_lo_pct"),
        "tp_hi_pct": exits.get("tp_hi_pct"),
        "exit_source": exits.get("exit_source"),
        "clamped_sl": exits.get("clamped_sl"),
        "clamped_tp": exits.get("clamped_tp"),
        "intended_rr": exits.get("intended_rr"),
        "ai_take_profit_pct": exits.get("ai_take_profit_pct"),
        "ai_stop_loss_pct": exits.get("ai_stop_loss_pct"),
        "ingestion_lag_sec": lags.get("ingestion_lag_sec"),
        "decision_lag_sec": lags.get("decision_lag_sec"),
        "news_age_sec": lags.get("news_age_sec"),
        "price_at_news": fill_audit.get("price_at_news"),
        "entry_vs_news_pct": fill_audit.get("entry_vs_news_pct"),
        "ref_source": fill_audit.get("ref_source"),
        "order_id": order.get("id"),
        "dry_run": DRY_RUN,
        "from_pending": bool(intent.get("from_pending")),
    }
    open_trades[symbol] = meta
    state.data["open_trades"] = open_trades
    pending = dict(state.data.get("pending_buys") or {})
    pending.pop(symbol, None)
    state.data["pending_buys"] = pending
    state.save()
    _append_jsonl(
        "trades.jsonl",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": "buy",
            "symbol": symbol,
            "qty": qty,
            "order": order,
            "meta": meta,
        },
    )
    _log(f"BUY placed {symbol} qty={qty} order={order.get('id')}")
    return "bought"


def _flush_pending_buys(
    broker,
    state: State,
    sleeve,
    *,
    equity: float,
    positions: dict,
    open_trades: dict,
) -> int:
    """Handle queued buys without market-chasing the open.

    Premarket: arm DAY+extended_hours limits.
    Anytime: poll fills / cancel if price ran through the chase ceiling.
    RTH: never convert pending → market bracket; resting DAY limits may still fill.
    """
    dropped = _expire_pending_buys(
        state,
        positions=positions,
        open_trades=open_trades,
        broker=broker,
        sleeve=sleeve,
    )
    if dropped:
        _log(f"pending expired/dropped={dropped}")

    filled = _poll_pending_limits(
        broker,
        state,
        sleeve,
        equity=equity,
        positions=positions,
        open_trades=dict(state.data.get("open_trades") or {}),
    )
    if filled:
        _log(f"pending limit fills={filled}")
        open_trades = dict(state.data.get("open_trades") or {})
        positions = position_map(broker)

    pending = dict(state.data.get("pending_buys") or {})
    if not pending:
        return filled

    pre = in_premarket()
    _log(
        f"pending_buys={len(pending)} premarket={pre} "
        f"limits={PREMARKET_LIMITS} max_chase={MAX_ENTRY_VS_NEWS_PCT}% "
        f"(no market chase)"
    )
    ordered = sorted(
        pending.items(),
        key=lambda kv: (
            0 if not _is_earnings(kv[1].get("catalyst_type")) else 1,
            -float(kv[1].get("confidence") or 0),
        ),
    )
    for symbol, intent in ordered:
        eg = _earnings_gate(
            catalyst=str(intent.get("catalyst_type") or ""),
            confidence=float(intent.get("confidence") or 0),
            open_trades=open_trades,
            pending_buys=dict(state.data.get("pending_buys") or {}),
            symbol=symbol,
            for_queue=False,
        )
        if eg and not intent.get("limit_order_id"):
            _log(f"EARNINGS skip pending {symbol}: {eg}")
            _drop_pending(
                state, symbol, reason=eg, broker=broker, sleeve=sleeve
            )
            continue
        if intent.get("limit_order_id"):
            continue  # already armed; poll path handles it
        if pre and PREMARKET_LIMITS:
            _arm_premarket_limit(
                broker,
                state,
                sleeve,
                symbol=symbol,
                equity=equity,
                positions=positions,
                open_trades=open_trades,
                intent=dict(intent),
            )
        elif market_open() and not intent.get("limit_order_id"):
            # RTH with no resting limit: refuse market chase; drop stale leftovers.
            _drop_pending(
                state,
                symbol,
                reason="no_premarket_limit_no_market_chase",
                broker=broker,
                sleeve=sleeve,
            )
    return filled


def maybe_enter(broker, state: State, sleeve) -> None:
    is_open = market_open()
    if not is_open and not SCORE_WHEN_CLOSED:
        _log("market closed — skip news scoring (set NEWS_TRADER_SCORE_WHEN_CLOSED=1 to enable)")
        return

    acct = broker.account()
    equity = float(acct.get("equity") or 0)
    positions = position_map(broker)
    open_trades = dict(state.data.get("open_trades") or {})

    # Prune stale queue even when closed (avoid overnight flush spam).
    dropped = _expire_pending_buys(
        state,
        positions=positions,
        open_trades=open_trades,
        broker=broker,
        sleeve=sleeve,
    )
    if dropped:
        _log(f"pending expired/dropped={dropped}")

    if not is_open:
        _log(
            f"market closed — score + queue buys "
            f"(pending={len(state.data.get('pending_buys') or {})})"
        )

    buys = 0
    # Premarket or RTH: arm/poll limits; never market-chase queued intents.
    if is_open or in_premarket():
        buys += _flush_pending_buys(
            broker,
            state,
            sleeve,
            equity=equity,
            positions=positions,
            open_trades=open_trades,
        )
        open_trades = dict(state.data.get("open_trades") or {})
        positions = position_map(broker)

    n_open = len([s for s in open_trades if s in positions])
    # Prefer WS inbox; REST only to warm/fallback.
    if USE_NEWS_WS:
        from .news_stream import is_connected, last_msg_age_sec

        age = last_msg_age_sec()
        stale = (not is_connected()) or (age is not None and age > WS_STALE_SEC)
        if stale:
            n_rest = 0
            try:
                for it in fetch_news(TECH_UNIVERSE, limit=60, hours_back=HOURS_BACK):
                    it = dict(it)
                    it["feed"] = "rest_fallback"
                    if INBOX.push(it):
                        n_rest += 1
            except Exception as exc:
                _log(f"rest fallback fail: {exc}")
            _log(
                f"news-ws stale connected={is_connected()} "
                f"last_msg_age={age} — rest_fallback +{n_rest}"
            )
    else:
        for it in fetch_news(TECH_UNIVERSE, limit=60, hours_back=HOURS_BACK):
            INBOX.push(it)

    # Order matters: universe filter (inbox gate) → prioritize → SCORE_LIMIT.
    fresh = prioritize_news(INBOX.pending(state.seen))
    stats = INBOX.stats()
    _log(
        f"news inbox buffered={stats['buffered']} "
        f"universe_pending={len(fresh)} "
        f"score_limit={SCORE_LIMIT} workers={SCORE_WORKERS} "
        f"dupes={stats.get('dupes', 0)} "
        f"rej_univ={stats.get('rejected_universe', 0)} "
        f"market_open={is_open} ws={USE_NEWS_WS} "
        f"max_chase={MAX_ENTRY_VS_NEWS_PCT}% max_age={MAX_NEWS_AGE_SEC:.0f}s"
    )

    to_score: list[dict[str, Any]] = []
    for item in fresh:
        if len(to_score) >= SCORE_LIMIT:
            break
        nid = str(item["id"])
        catalyst = news_catalyst_type(item)
        decision_ts = datetime.now(timezone.utc)
        lags = lag_breakdown(
            news_created_at=item.get("created_at"),
            received_at=item.get("received_at"),
            decision_at=decision_ts,
        )
        is_xdupe, matched = XDUPE.is_duplicate(item)
        if is_xdupe:
            state.mark_seen(nid)
            _log(
                f"XDUPE skip [{item.get('feed')}] {nid[:24]}… "
                f"~{matched} cat={catalyst} (no score slot)"
            )
            _append_jsonl(
                "signals.jsonl",
                {
                    "ts": decision_ts.isoformat(),
                    "news_id": nid,
                    "headline": item.get("headline"),
                    "feed": item.get("feed"),
                    "catalyst_type": catalyst,
                    "sec_items": item.get("sec_items"),
                    "cross_dupe_of": matched,
                    **lags,
                    "skipped": "cross_source_dupe",
                },
            )
            continue
        state.mark_seen(nid)
        XDUPE.remember(item)
        to_score.append(item)

    if to_score:
        _log(
            f"ds score batch n={len(to_score)} workers={SCORE_WORKERS} "
            f"model={score_model()} pro={pro_model()} "
            f"escalate={int(escalate_pro())} timeout={score_timeout_sec():.0f}s"
        )

    scored = 0
    for item, score, err in score_news_batch(to_score, workers=SCORE_WORKERS):
        nid = str(item["id"])
        catalyst = news_catalyst_type(item)
        decision_ts = datetime.now(timezone.utc)
        lags = lag_breakdown(
            news_created_at=item.get("created_at"),
            received_at=item.get("received_at"),
            decision_at=decision_ts,
        )
        if err is not None:
            _log(f"score fail {nid}: {err}")
            _append_jsonl(
                "signals.jsonl",
                {
                    "ts": decision_ts.isoformat(),
                    "news": item,
                    "catalyst_type": catalyst,
                    "feed": item.get("feed"),
                    "received_at": item.get("received_at"),
                    **lags,
                    "error": str(err)[:300],
                },
            )
            continue
        if score is None:
            continue
        scored += 1

        row = {
            "ts": decision_ts.isoformat(),
            "news_id": nid,
            "headline": item.get("headline"),
            "symbols": item.get("symbols"),
            "created_at": item.get("created_at"),
            "received_at": item.get("received_at"),
            "feed": item.get("feed"),
            "source": item.get("source"),
            "catalyst_type": catalyst,
            "sec_items": item.get("sec_items"),
            "sec_accession": item.get("sec_accession"),
            **lags,
            "score": {k: v for k, v in score.items() if k != "raw"},
        }
        _append_jsonl("signals.jsonl", row)

        route = str(score.get("route") or "flash")
        if route.startswith("edge_pro") or route.startswith("sec_pro"):
            _log(
                f"PRO {route} {nid[:8]}… "
                f"flash={score.get('flash_action')}/"
                f"{score.get('flash_confidence')} "
                f"→ {score.get('action')}/{score.get('confidence')} "
                f"model={score.get('model')}"
            )

        if score.get("action") != "buy":
            _log(
                f"SKIP [{catalyst}/{route}] {nid[:8]}… "
                f"{(item.get('headline') or '')[:70]}"
            )
            continue
        conf = float(score.get("confidence") or 0)
        if conf < MIN_CONF:
            _log(
                f"LOW CONF {score.get('symbol')} conf={conf:.2f} "
                f"< {MIN_CONF} route={route}"
            )
            continue

        symbol = str(score["symbol"]).upper()
        thesis = score.get("thesis") or ""
        eg = _earnings_gate(
            catalyst=catalyst,
            confidence=conf,
            open_trades=open_trades,
            pending_buys=dict(state.data.get("pending_buys") or {}),
            symbol=symbol,
            for_queue=not is_open,
        )
        if eg:
            _log(f"EARNINGS skip {symbol}: {eg}")
            _append_jsonl(
                "signals.jsonl",
                {
                    "ts": decision_ts.isoformat(),
                    "news_id": nid,
                    "symbol": symbol,
                    "headline": item.get("headline"),
                    "catalyst_type": catalyst,
                    "confidence": conf,
                    "skipped": "earnings_throttle",
                    "reason": eg,
                    **lags,
                },
            )
            continue

        _log(
            f"BUY signal {symbol} conf={conf:.2f} "
            f"hold={score.get('hold_hours')}h "
            f"ai_sl={score.get('stop_loss_pct')}% "
            f"ai_tp={score.get('take_profit_pct')}% "
            f"[{catalyst}/{route}] | {thesis[:100]}"
        )

        intent = {
            "symbol": symbol,
            "news_id": nid,
            "headline": item.get("headline"),
            "news_created_at": item.get("created_at"),
            "received_at": item.get("received_at"),
            "feed": item.get("feed"),
            "news_source": item.get("source"),
            "catalyst_type": catalyst,
            "confidence": score.get("confidence"),
            "thesis": thesis,
            "hold_hours": score.get("hold_hours"),
            "ai_take_profit_pct": score.get("take_profit_pct"),
            "ai_stop_loss_pct": score.get("stop_loss_pct"),
            "from_pending": False,
        }

        if not is_open:
            _queue_pending_buy(
                state,
                symbol=symbol,
                item=item,
                score=score,
                catalyst=catalyst,
            )
            # Arm premarket limit immediately when the session allows.
            if in_premarket() and PREMARKET_LIMITS:
                _arm_premarket_limit(
                    broker,
                    state,
                    sleeve,
                    symbol=symbol,
                    equity=equity,
                    positions=positions,
                    open_trades=open_trades,
                    intent={
                        **intent,
                        "from_pending": True,
                    },
                )
            continue

        result = _try_buy(
            broker,
            state,
            sleeve,
            symbol=symbol,
            equity=equity,
            positions=positions,
            open_trades=open_trades,
            n_open=n_open,
            buys=buys,
            intent=intent,
        )
        if result == "bought":
            buys += 1
            open_trades = dict(state.data.get("open_trades") or {})
            positions = position_map(broker)
        elif result == "retry" and buys >= MAX_BUYS_PER_CYCLE:
            # Live signal hit cycle cap — queue so the next cycle can still fire.
            _queue_pending_buy(
                state,
                symbol=symbol,
                item=item,
                score=score,
                catalyst=catalyst,
            )

    if to_score:
        _log(f"ds scored ok={scored}/{len(to_score)}")
    state.save()


def _last_price(symbol: str) -> float:
    """Latest trade price via Alpaca stock snapshot HTTP."""
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    # IEX feed is typically available on paper keys
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest?feed=iex"
    import urllib.request

    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", secret)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        trade = body.get("trade") or {}
        px = float(trade.get("p") or 0)
        if px > 0:
            return px
    except Exception as exc:
        _log(f"price fail {symbol}: {exc}")
    # fallback: bars
    try:
        url2 = (
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars/latest?feed=iex"
        )
        req2 = urllib.request.Request(url2)
        req2.add_header("APCA-API-KEY-ID", key)
        req2.add_header("APCA-API-SECRET-KEY", secret)
        with urllib.request.urlopen(req2, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        bar = body.get("bar") or {}
        return float(bar.get("c") or 0)
    except Exception as exc:
        _log(f"bar fail {symbol}: {exc}")
        return 0.0


def run_cycle() -> None:
    state = State(STORE / "state.json")
    broker = get_broker()
    acct = None
    last_err = None
    for attempt in range(4):
        try:
            acct = broker.account()
            break
        except Exception as exc:
            last_err = exc
            _log(f"account retry {attempt + 1}/4: {exc}")
            time.sleep(1.5 * (2**attempt))
    if acct is None:
        raise RuntimeError(f"account unavailable after retries: {last_err}")
    sleeve = None
    try:
        ensure_sleeve_schema()
        ensure_strategy(STRATEGY_ID, name=STRATEGY_NAME, stype="news_ai")
        sleeve = SleeveBook.load()
        changes = sleeve.reconcile(
            broker_qty_map(broker.positions()),
            protected=open_order_symbols(broker),
        )
        for change in changes:
            _log(f"sleeve {change.kind}: {change}")
        for line in format_settlements(settle_broker_exits(broker, changes)):
            _log(f"settled: {line}")
        n_adopt = _adopt_orphans(sleeve, broker, state)
        if n_adopt:
            _log(f"adopted {n_adopt} orphan lot(s) back to {STRATEGY_ID}")
    except LedgerUnavailable as exc:
        _log(f"sleeve ledger unavailable ({exc}) — no buys, and no sells without ownership proof")

    _log(
        f"cycle equity={float(acct.get('equity') or 0):.2f} "
        f"cash={float(acct.get('cash') or 0):.2f} dry={DRY_RUN} "
        f"open={len(state.data.get('open_trades') or {})} "
        f"sleeve={len(sleeve.symbols_of(STRATEGY_ID)) if sleeve else 'n/a'} "
        f"pending={len(state.data.get('pending_buys') or {})}"
    )
    manage_exits(broker, state, sleeve)
    maybe_enter(broker, state, sleeve)


def _cycle_sleep_sec() -> float:
    """Max nap until next cycle; wake_scoring short-circuits this."""
    if market_open():
        return float(INTERVAL)
    pending_n = 0
    try:
        # Lightweight: only used for pre-open snappiness when queue is hot
        st = State(STORE / "state.json")
        pending_n = len(st.data.get("pending_buys") or {})
    except Exception:
        pending_n = 0
    until = seconds_until_open()
    if until is not None and pending_n > 0:
        # Wake near the open so queued earnings buys are not minutes late
        return max(1.0, min(float(OFF_HOURS_INTERVAL), until + 2.0, PREOPEN_POLL_SEC))
    if until is not None and until < float(OFF_HOURS_INTERVAL):
        return max(1.0, min(float(OFF_HOURS_INTERVAL), until + 2.0))
    return float(OFF_HOURS_INTERVAL)


def main() -> int:
    _log(
        f"news-trader start interval={INTERVAL}s off_hours={OFF_HOURS_INTERVAL}s "
        f"min_conf={MIN_CONF} max_pos={MAX_POS} pct={PCT_EQUITY} dry={DRY_RUN} "
        f"once={ONCE} news_ws={USE_NEWS_WS} score_closed={SCORE_WHEN_CLOSED} "
        f"ds_model={score_model()} pro={pro_model()} "
        f"escalate={int(escalate_pro())} edge={edge_band()[0]:.2f}-{edge_band()[1]:.2f} "
        f"score_workers={SCORE_WORKERS} "
        f"score_timeout={score_timeout_sec():.0f}s/"
        f"{score_timeout_sec(pro_model()):.0f}s score_limit={SCORE_LIMIT} "
        f"max_chase={MAX_ENTRY_VS_NEWS_PCT}% max_age={MAX_NEWS_AGE_SEC:.0f}s "
        f"earn_min_conf={EARNINGS_MIN_CONF} earn_max_open={EARNINGS_MAX_OPEN} "
        f"earn_queue_closed={EARNINGS_QUEUE_CLOSED} "
        f"pending_max_age={PENDING_MAX_AGE_SEC:.0f}s "
        f"premarket_limits={PREMARKET_LIMITS} "
        f"aggress_bps={PREMARKET_LIMIT_AGGRESS_BPS}/"
        f"near={PREMARKET_NEAR_OPEN_AGGRESS_BPS}@"
        f"{PREMARKET_NEAR_OPEN_MIN:.0f}m "
        f"reprice_bps={PREMARKET_REPRICE_BPS} shave_bps={PREMARKET_LIMIT_SHAVE_BPS}"
    )
    if USE_NEWS_WS:
        from .news_stream import start_news_stream

        start_news_stream(log=_log)
        try:
            n = bootstrap_inbox(TECH_UNIVERSE, limit=60, hours_back=HOURS_BACK)
            _log(f"news inbox bootstrap +{n} (rest seed)")
        except Exception as exc:
            _log(f"news inbox bootstrap fail: {exc}")
    from .sec_edgar import start_sec_edgar

    start_sec_edgar(log=_log)
    while True:
        try:
            run_cycle()
        except Exception as exc:
            _log(f"cycle error: {exc}")
            traceback.print_exc()
        if ONCE:
            return 0
        sleep_for = _cycle_sleep_sec()
        _log(f"sleep ≤{sleep_for:.0f}s (market_open={market_open()}, event-wake)")
        if wait_scoring_wake(sleep_for):
            _log("woke early on new news")


if __name__ == "__main__":
    sys.exit(main())
