"""
Settle exits the runners never placed.
======================================
Every entry is submitted as a bracket, so most positions leave the account when
a stop-loss or take-profit leg fires at the broker. No runner is involved, so
until the next reconcile nobody knows the position is gone — and reconcile only
knows *that* it went, not at what price.

This asks the broker for the sell orders that actually filled and uses their
fill price to close the matching trade_journal rows, which is what turns the
journal into real per-strategy realized P&L instead of a pile of 'stale' rows.

Fills are matched by symbol and time window: any sell that filled after the
ledger row was last touched. Exit price is the quantity-weighted average across
those fills, so a partially-filled bracket still settles correctly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.portfolio.sleeve import UNASSIGNED, LedgerChange
from core.trade_log import record_exit

logger = logging.getLogger(__name__)

# How far back to ask the broker for fills. Generous, because a position may
# have closed while a runner was down; the time filter below is the real guard.
LOOKBACK_DAYS = 7


def _filled_sells(client: Any, symbols: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """{symbol: [filled sell orders, oldest first]}."""
    symbols = sorted({s.upper() for s in symbols})
    if not symbols:
        return {}
    after = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    try:
        orders = client.orders(status="closed", limit=500, after=after, symbols=symbols)
    except Exception as exc:
        logger.warning("fill lookup failed: %s", exc)
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        if str(o.get("side", "")).lower() != "sell":
            continue
        if not o.get("filled_at") or not (o.get("filled_qty") or 0):
            continue
        if o.get("filled_avg_price") in (None, 0):
            continue
        out.setdefault(str(o["symbol"]).upper(), []).append(o)
    for rows in out.values():
        rows.sort(key=lambda r: r["filled_at"])
    return out


def _as_utc(ts: datetime | None) -> datetime | None:
    """Postgres hands back naive timestamps; comparing those to the broker's
    tz-aware fill times raises."""
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def settle_broker_exits(
    client: Any,
    changes: Iterable[LedgerChange],
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Close journal rows for positions a broker bracket leg exited.

    Returns one summary per settled symbol. Positions that vanished with no
    matching fill are left for mark_stale_journal(); inventing a price would
    corrupt the P&L this is meant to produce.
    """
    exits = [
        c
        for c in changes
        if c.kind in ("closed", "reduced")
        and c.qty_gone > 0
        and c.strategy_id != UNASSIGNED
    ]
    if not exits:
        return []

    fills = _filled_sells(client, [c.symbol for c in exits])
    floor = since or (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    settled: list[dict[str, Any]] = []

    for change in exits:
        # Only fills after the ledger last saw this position can be its exit.
        # Without this a symbol traded twice in a week would price a 30-share
        # exit using an earlier 100-share one.
        cutoff = max(_as_utc(change.since) or floor, floor)
        rows = [
            o
            for o in fills.get(change.symbol.upper(), [])
            if (_parse(o.get("filled_at")) or cutoff) >= cutoff
        ]
        if not rows:
            logger.info("no broker fill found for %s/%s", change.strategy_id, change.symbol)
            continue

        # Weighted average across however many legs filled.
        qty = sum(float(o["filled_qty"]) for o in rows)
        if qty <= 0:
            continue
        price = sum(float(o["filled_qty"]) * float(o["filled_avg_price"]) for o in rows) / qty

        matched_qty = min(qty, change.qty_gone)
        order_id = rows[-1]["id"]

        # Attribute the broker's own order to the sleeve, then settle the
        # journal. Re-inserting an order id we already logged is rejected by the
        # primary key and skipped, so this is safe to re-run.
        closed = record_exit(
            strategy_id=change.strategy_id,
            strategy_name=change.strategy_id,
            symbol=change.symbol,
            qty=matched_qty,
            price=price,
            order_id=order_id,
            status="filled",
            reason=f"broker bracket exit ({len(rows)} fill(s))",
            order_type="bracket",
            filled_qty=matched_qty,
        )
        realized = (price - change.avg_price) * matched_qty if change.avg_price else None
        settled.append(
            {
                "strategy_id": change.strategy_id,
                "symbol": change.symbol,
                "qty": matched_qty,
                "exit_price": price,
                "order_id": order_id,
                "journal_rows": closed,
                "realized_pnl": realized,
            }
        )

    return settled


def format_settlements(settled: Iterable[dict[str, Any]]) -> list[str]:
    lines = []
    for s in settled:
        pnl = s.get("realized_pnl")
        pnl_txt = f" pnl=${pnl:,.0f}" if pnl is not None else ""
        lines.append(
            f"{s['symbol']} x{s['qty']:g} @ ${s['exit_price']:.2f}{pnl_txt} "
            f"-> {s['strategy_id']} ({s['journal_rows']} row(s))"
        )
    return lines
