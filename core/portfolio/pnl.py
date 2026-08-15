"""
Portfolio P&L summaries for the dashboard.
==========================================
Combines the sleeve ledger (who owns what) with broker marks (unrealized)
and trade_journal (realized) so the UI can show live attribution instead of
backtest Sharpe numbers.

Multiple strategies may share a symbol (coexist lots); each lot's unrealized
P&L uses that sleeve's avg_price × qty vs the broker mark.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _session():
    from core.db import SyncSessionLocal

    return SyncSessionLocal()


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def portfolio_pnl(
    positions: list[dict[str, Any]],
    strategies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return realized totals + per-strategy open/closed P&L.

    Never raises: a DB hiccup returns zeros so the dashboard still loads.
    """
    by_symbol = {str(p.get("symbol", "")).upper(): p for p in positions}
    name_by_id: dict[str, str] = {}
    for s in strategies or []:
        sid = str(s.get("id") or "")
        if sid:
            name_by_id[sid] = str(s.get("name") or sid)

    sleeves: list[tuple[str, str, float, float]] = []
    realized_rows: list[tuple[str, str | None, float, float, int]] = []
    try:
        s = _session()
        try:
            sleeves = list(
                s.execute(
                    _text(
                        "SELECT strategy_id, symbol, qty, avg_price "
                        "FROM strategy_positions WHERE qty > 0 "
                        "ORDER BY strategy_id, symbol"
                    )
                ).fetchall()
            )
            realized_rows = list(
                s.execute(
                    _text(
                        """
                        SELECT strategy_id,
                               MAX(strategy_name) AS strategy_name,
                               COALESCE(SUM(realized_pnl), 0) AS realized,
                               COALESCE(SUM(CASE
                                   WHEN (closed_at AT TIME ZONE 'America/New_York')::date
                                        = (NOW() AT TIME ZONE 'America/New_York')::date
                                   THEN realized_pnl ELSE 0 END), 0) AS today_realized,
                               COUNT(*) FILTER (
                                   WHERE status = 'closed' AND exit_price IS NOT NULL
                               ) AS n_closed
                        FROM trade_journal
                        WHERE status = 'closed' AND exit_price IS NOT NULL
                        GROUP BY strategy_id
                        """
                    )
                ).fetchall()
            )
        finally:
            s.close()
    except Exception as exc:
        logger.warning("portfolio_pnl query failed: %s", exc)

    # strategy_id -> bucket
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(sid: str, name: str | None = None) -> dict[str, Any]:
        if sid not in buckets:
            buckets[sid] = {
                "strategy_id": sid,
                "strategy_name": name or name_by_id.get(sid) or sid,
                "symbols": [],
                "qty_positions": 0,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "today_realized_pnl": 0.0,
                "closed_trades": 0,
            }
        elif name and buckets[sid]["strategy_name"] in (sid, ""):
            buckets[sid]["strategy_name"] = name
        return buckets[sid]

    # Count how many sleeve lots share each symbol (shared lots need cost-basis UPL).
    lots_per_symbol: dict[str, int] = {}
    for _sid, symbol, _qty, _avg in sleeves:
        sym = str(symbol).upper()
        lots_per_symbol[sym] = lots_per_symbol.get(sym, 0) + 1

    for sid, symbol, qty, avg_price in sleeves:
        sid, symbol = str(sid), str(symbol).upper()
        b = bucket(sid)
        pos = by_symbol.get(symbol) or {}
        px = float(pos.get("current_price") or 0)
        q = float(qty)
        avg = float(avg_price or 0)
        # Each lot's market value is its own notional (not the full broker MV).
        mv = px * q if px else 0.0
        broker_qty = float(pos.get("qty") or 0)
        broker_upl = float(pos.get("unrealized_pl") or 0)
        shared = lots_per_symbol.get(symbol, 0) > 1
        if shared and px and avg:
            # Restored / coexist lots: attribute by sleeve cost basis.
            upl = (px - avg) * q
        elif broker_qty > 0 and abs(broker_qty - q) > 1e-6:
            upl = broker_upl * (q / broker_qty)
        elif broker_qty > 0:
            upl = broker_upl
        elif px and avg:
            upl = (px - avg) * q
        else:
            upl = 0.0
        b["symbols"].append(symbol)
        b["qty_positions"] += 1
        b["market_value"] += mv
        b["unrealized_pnl"] += upl

    for sid, sname, realized, today_realized, n_closed in realized_rows:
        sid = str(sid)
        b = bucket(sid, str(sname) if sname else None)
        b["realized_pnl"] = float(realized or 0)
        b["today_realized_pnl"] = float(today_realized or 0)
        b["closed_trades"] = int(n_closed or 0)

    # Enabled strategies with neither open nor closed still show as zeros so
    # the operator can see they are armed but idle.
    for s in strategies or []:
        if not s.get("enabled"):
            continue
        bucket(str(s["id"]), str(s.get("name") or s["id"]))

    rows = sorted(
        buckets.values(),
        key=lambda r: -(abs(r["unrealized_pnl"]) + abs(r["realized_pnl"]) + r["market_value"]),
    )
    for r in rows:
        r["total_pnl"] = r["unrealized_pnl"] + r["realized_pnl"]
        r["symbols"] = sorted(set(r["symbols"]))

    realized_total = sum(r["realized_pnl"] for r in rows)
    today_realized = sum(r["today_realized_pnl"] for r in rows)
    unrealized_total = sum(r["unrealized_pnl"] for r in rows)

    # ownership[symbol] = primary (first) + lots[] for shared holdings.
    ownership: dict[str, dict[str, Any]] = {}
    for sid, symbol, qty, avg_price in sleeves:
        sym = str(symbol).upper()
        lot = {
            "strategy_id": str(sid),
            "strategy_name": name_by_id.get(str(sid), str(sid)),
            "qty": float(qty),
            "avg_price": float(avg_price or 0),
        }
        if sym not in ownership:
            ownership[sym] = {
                **lot,
                "lots": [lot],
            }
        else:
            ownership[sym]["lots"].append(lot)
            # Surface combined label for the positions table.
            names = [x["strategy_name"] for x in ownership[sym]["lots"]]
            ownership[sym]["strategy_name"] = " + ".join(names)
            ownership[sym]["qty"] = sum(x["qty"] for x in ownership[sym]["lots"])

    return {
        "realized_pnl": realized_total,
        "today_realized_pnl": today_realized,
        "unrealized_pnl": unrealized_total,
        "by_strategy": rows,
        "ownership": ownership,
    }


def journal_summary(strategy_id: str | None = None) -> dict[str, Any]:
    """Lifetime realized totals — same closed-trade definition as portfolio_pnl.

    Independent of list pagination so Journal header matches Dashboard.
    """
    sql = """
        SELECT
            COALESCE(SUM(CASE
                WHEN status = 'closed' AND exit_price IS NOT NULL
                THEN realized_pnl ELSE 0 END), 0) AS realized_pnl,
            COALESCE(SUM(CASE
                WHEN status = 'closed' AND exit_price IS NOT NULL
                 AND (closed_at AT TIME ZONE 'America/New_York')::date
                     = (NOW() AT TIME ZONE 'America/New_York')::date
                THEN realized_pnl ELSE 0 END), 0) AS today_realized_pnl,
            COUNT(*) FILTER (
                WHERE status = 'closed' AND exit_price IS NOT NULL
            ) AS closed_trades,
            COUNT(*) FILTER (WHERE status = 'open') AS open_trades
        FROM trade_journal
    """
    params: dict[str, Any] = {}
    if strategy_id:
        sql += " WHERE strategy_id = :sid"
        params["sid"] = strategy_id
    try:
        s = _session()
        try:
            row = s.execute(_text(sql), params).mappings().first()
        finally:
            s.close()
    except Exception as exc:
        logger.warning("journal_summary failed: %s", exc)
        return {
            "realized_pnl": 0.0,
            "today_realized_pnl": 0.0,
            "closed_trades": 0,
            "open_trades": 0,
        }
    if not row:
        return {
            "realized_pnl": 0.0,
            "today_realized_pnl": 0.0,
            "closed_trades": 0,
            "open_trades": 0,
        }
    return {
        "realized_pnl": float(row["realized_pnl"] or 0),
        "today_realized_pnl": float(row["today_realized_pnl"] or 0),
        "closed_trades": int(row["closed_trades"] or 0),
        "open_trades": int(row["open_trades"] or 0),
    }


def list_journal(
    status: str | None = None,
    limit: int = 100,
    strategy_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recent trade_journal rows for the Journal page.

    Default (status unset / ``all``) excludes ``signal_noise`` and
    ``superseded`` so the UI only shows real open/closed/stale lots.
    Pass ``status=audit`` to fetch those non-trade rows explicitly.
    """
    limit = max(1, min(int(limit), 500))
    sql = (
        "SELECT trade_id, strategy_id, strategy_name, symbol, side, status, "
        "qty, entry_price, exit_price, realized_pnl, return_pct, holding_days, "
        "signal_reason, opened_at, closed_at "
        "FROM trade_journal "
    )
    params: dict[str, Any] = {"lim": limit}
    clauses: list[str] = []
    status_key = (status or "").strip().lower()
    if status_key in ("", "all"):
        clauses.append("status NOT IN ('signal_noise', 'superseded')")
    elif status_key == "audit":
        clauses.append("status IN ('signal_noise', 'superseded')")
    else:
        clauses.append("status = :status")
        params["status"] = status_key
    if strategy_id:
        clauses.append("strategy_id = :sid")
        params["sid"] = strategy_id
    if clauses:
        sql += "WHERE " + " AND ".join(clauses) + " "
    sql += "ORDER BY COALESCE(closed_at, opened_at) DESC NULLS LAST LIMIT :lim"
    try:
        s = _session()
        try:
            rows = s.execute(_text(sql), params).mappings().fetchall()
        finally:
            s.close()
    except Exception as exc:
        logger.warning("list_journal failed: %s", exc)
        return []

    out = []
    for r in rows:
        out.append(
            {
                "trade_id": r["trade_id"],
                "strategy_id": r["strategy_id"],
                "strategy_name": r["strategy_name"] or r["strategy_id"],
                "symbol": r["symbol"],
                "side": r["side"],
                "status": r["status"],
                "qty": float(r["qty"] or 0),
                "entry_price": float(r["entry_price"]) if r["entry_price"] is not None else None,
                "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
                "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else None,
                "return_pct": float(r["return_pct"]) if r["return_pct"] is not None else None,
                "holding_days": int(r["holding_days"]) if r["holding_days"] is not None else None,
                "signal_reason": r["signal_reason"] or "",
                "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                "current_price": None,
                "unrealized_pnl": None,
            }
        )
    return out


def enrich_open_journal(
    rows: list[dict[str, Any]],
    positions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Attach floating P&L for open journal rows from broker marks.

    Uses each lot's own entry/qty (not the broker position aggregate) so
    coexist sleeves stay attributable. Safe no-op if marks are missing.
    """
    if not rows or not positions:
        return rows

    marks: dict[str, dict[str, Any]] = {}
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        if sym:
            marks[sym] = p

    for row in rows:
        if str(row.get("status") or "").lower() != "open":
            continue
        pos = marks.get(str(row.get("symbol") or "").upper())
        if not pos:
            continue
        try:
            px = float(pos.get("current_price") or 0)
        except (TypeError, ValueError):
            continue
        entry = row.get("entry_price")
        qty = abs(float(row.get("qty") or 0))
        if not px or entry is None or entry <= 0 or qty <= 0:
            continue
        side = str(row.get("side") or "buy").lower()
        if side in ("sell", "short"):
            upl = (float(entry) - px) * qty
        else:
            upl = (px - float(entry)) * qty
        cost = float(entry) * qty
        row["current_price"] = round(px, 4)
        row["unrealized_pnl"] = round(upl, 2)
        row["return_pct"] = round(upl / cost * 100, 2) if cost else None
    return rows


def ensure_equity_schema() -> None:
    try:
        s = _session()
        try:
            s.execute(
                _text(
                    """
                    CREATE TABLE IF NOT EXISTS equity_snapshots (
                        ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        equity    DOUBLE PRECISION NOT NULL,
                        cash      DOUBLE PRECISION,
                        last_equity DOUBLE PRECISION,
                        source    VARCHAR(32) NOT NULL DEFAULT 'poll',
                        PRIMARY KEY (ts)
                    )
                    """
                )
            )
            s.execute(
                _text(
                    "CREATE INDEX IF NOT EXISTS ix_equity_snapshots_ts "
                    "ON equity_snapshots (ts DESC)"
                )
            )
            s.commit()
        finally:
            s.close()
    except Exception as exc:
        logger.warning("ensure_equity_schema failed: %s", exc)


def record_equity_snapshot(
    equity: float,
    cash: float | None = None,
    last_equity: float | None = None,
    source: str = "poll",
    min_interval_sec: int = 300,
) -> bool:
    """Persist an equity point, throttled so the WS loop does not flood the table."""
    if equity <= 0:
        return False
    try:
        ensure_equity_schema()
        s = _session()
        try:
            recent = s.execute(
                _text(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - ts)) "
                    "FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
                )
            ).scalar()
            if recent is not None and float(recent) < min_interval_sec:
                return False
            s.execute(
                _text(
                    "INSERT INTO equity_snapshots (ts, equity, cash, last_equity, source) "
                    "VALUES (NOW(), :eq, :cash, :last, :src) "
                    "ON CONFLICT (ts) DO NOTHING"
                ),
                {
                    "eq": float(equity),
                    "cash": float(cash) if cash is not None else None,
                    "last": float(last_equity) if last_equity is not None else None,
                    "src": source,
                },
            )
            s.commit()
            return True
        finally:
            s.close()
    except Exception as exc:
        logger.warning("record_equity_snapshot failed: %s", exc)
        return False


def equity_curve(days: int = 30) -> list[dict[str, Any]]:
    """Points for the paper equity chart, oldest first."""
    days = max(1, min(int(days), 365))
    try:
        ensure_equity_schema()
        s = _session()
        try:
            rows = s.execute(
                _text(
                    "SELECT ts, equity, cash FROM equity_snapshots "
                    "WHERE ts >= NOW() - (:days * INTERVAL '1 day') "
                    "ORDER BY ts ASC"
                ),
                {"days": days},
            ).fetchall()
        finally:
            s.close()
    except Exception as exc:
        logger.warning("equity_curve failed: %s", exc)
        return []
    return [
        {
            "ts": r[0].isoformat() if r[0] else None,
            "equity": float(r[1] or 0),
            "cash": float(r[2]) if r[2] is not None else None,
        }
        for r in rows
    ]
