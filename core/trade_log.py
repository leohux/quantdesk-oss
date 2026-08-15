"""
Shared trade logging for every runner process.
==============================================
phase6_runner, intraday_runner and news_trader each used to write their own
subset of the audit tables: one wrote orders + transactions + trade_journal,
one wrote only trade_journal, one wrote only a local JSONL file. Per-strategy
attribution was impossible to reconstruct as a result.

All three now go through here, so `orders`, `transactions` and `trade_journal`
carry the same picture. Nothing in this module is allowed to raise — a failed
audit write must never take down a trading loop.

`trade_journal` rows are opened on entry and closed on exit, which is what
makes realized per-strategy P&L available. Previously every non-HOLD *signal*
inserted a row with status='open' and nothing ever closed it, so the table
filled with duplicates and phantom positions from dry runs.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session():
    from core.db import SyncSessionLocal

    return SyncSessionLocal()


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def insert(table: str, row: dict[str, Any], *, retries: int = 3) -> bool:
    """Best-effort INSERT. Returns False instead of raising.

    Retries transient DB failures. A broker order that cannot be audited is
    written to a dead-letter file so it is not silently lost.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            s = _session()
        except Exception as exc:
            last_exc = exc
            logger.warning("trade_log session failed (attempt %d): %s", attempt + 1, exc)
            continue
        try:
            cols = ", ".join(row.keys())
            vals = ", ".join(f":{k}" for k in row)
            s.execute(_text(f"INSERT INTO {table} ({cols}) VALUES ({vals})"), row)
            s.commit()
            return True
        except Exception as exc:
            s.rollback()
            # Re-logging a broker order we already recorded is how settlement stays
            # idempotent, not a failure worth shouting about.
            if _is_duplicate_key(exc):
                logger.debug("trade_log %s row already present: %s", table, row.get("id"))
                return True
            last_exc = exc
            logger.warning(
                "trade_log insert into %s failed (attempt %d): %s",
                table,
                attempt + 1,
                exc,
            )
        finally:
            s.close()
    _dead_letter(table, row, last_exc)
    return False


def _dead_letter(table: str, row: dict[str, Any], exc: Exception | None) -> None:
    """Persist a failed audit row so a broker order is not invisible forever."""
    try:
        from pathlib import Path
        import json

        path = Path("/app/data/store/trade_log_dead_letter.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": _now().isoformat(),
                        "table": table,
                        "row": row,
                        "error": str(exc) if exc else None,
                    },
                    default=str,
                )
                + "\n"
            )
        logger.error("trade_log %s write failed; dead-lettered to %s", table, path)
    except Exception as write_exc:
        logger.error("trade_log dead-letter also failed: %s", write_exc)


def _is_duplicate_key(exc: Exception) -> bool:
    try:
        from sqlalchemy.exc import IntegrityError
    except Exception:  # pragma: no cover
        return False
    return isinstance(exc, IntegrityError) and "duplicate key" in str(exc).lower()


def ensure_strategy(
    strategy_id: str,
    name: str | None = None,
    stype: str = "custom",
    enabled: bool = True,
    params: Any = None,
    metrics: Any = None,
) -> bool:
    """Upsert a strategies row so the orders.strategy_id foreign key holds."""
    import json

    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    try:
        s = _session()
    except Exception as exc:
        logger.warning("trade_log session failed: %s", exc)
        return False
    try:
        s.execute(
            _text(
                """
                INSERT INTO strategies (id, name, type, enabled, params, metrics, created_at, updated_at)
                VALUES (:id, :name, :type, :enabled, CAST(:params AS json), CAST(:metrics AS json), NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    type = EXCLUDED.type,
                    enabled = EXCLUDED.enabled,
                    params = EXCLUDED.params,
                    metrics = EXCLUDED.metrics,
                    updated_at = NOW()
                """
            ),
            {
                "id": sid,
                "name": str(name or sid)[:200],
                "type": str(stype)[:64],
                "enabled": bool(enabled),
                "params": json.dumps(params) if params is not None else None,
                "metrics": json.dumps(metrics) if metrics is not None else None,
            },
        )
        s.commit()
        return True
    except Exception as exc:
        s.rollback()
        logger.warning("trade_log strategy upsert %s failed: %s", sid, exc)
        return False
    finally:
        s.close()


def _log_order_and_txn(
    order_id: str,
    strategy_id: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    filled_qty: float,
    status: str,
    order_type: str,
) -> None:
    at = _now().isoformat()
    ok = insert(
        "orders",
        {
            "id": order_id,
            "symbol": symbol,
            "qty": float(qty),
            "filled_qty": float(filled_qty or 0),
            "side": side,
            "type": order_type,
            "status": status,
            "strategy_id": strategy_id,
            "submitted_at": at,
            "created_at": at,
        },
    )
    if not ok:
        # transactions.order_id references orders.id; skip rather than fail loudly
        return
    insert(
        "transactions",
        {
            "id": str(uuid.uuid4())[:12],
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "qty": float(qty),
            "price": float(price),
            "fee": float(price) * float(qty) * 0.0001,
            "strategy_id": strategy_id,
            "created_at": at,
        },
    )


def record_entry(
    strategy_id: str,
    strategy_name: str,
    symbol: str,
    qty: float,
    price: float,
    order_id: str | None = None,
    status: str = "submitted",
    reason: str = "",
    order_type: str = "market",
    filled_qty: float = 0.0,
    atr: float | None = None,
    risk_pct: float | None = None,
    position_value_pct: float | None = None,
) -> None:
    """Book a submitted BUY across orders, transactions and trade_journal."""
    order_id = order_id or str(uuid.uuid4())
    _log_order_and_txn(
        order_id, strategy_id, symbol, "buy", qty, price, filled_qty, status, order_type
    )
    at = _now().isoformat()
    insert(
        "trade_journal",
        {
            "trade_id": str(uuid.uuid4())[:12],
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "side": "buy",
            "signal_reason": reason,
            "entry_price": float(price),
            "qty": float(qty),
            "atr": atr,
            "risk_pct": risk_pct,
            "position_value_pct": position_value_pct,
            "status": "open",
            "opened_at": at,
            "created_at": at,
            # Trail state — peak starts at fill; columns added by ensure_trail_columns.
            "peak_price": float(price),
            "trail_active": False,
            "trail_stop_price": None,
        },
    )


def record_exit(
    strategy_id: str,
    strategy_name: str,
    symbol: str,
    qty: float,
    price: float,
    order_id: str | None = None,
    status: str = "submitted",
    reason: str = "",
    order_type: str = "market",
    filled_qty: float = 0.0,
) -> int:
    """Book a submitted SELL and close this sleeve's open journal rows.

    Returns the number of journal rows closed.
    """
    order_id = order_id or str(uuid.uuid4())
    _log_order_and_txn(
        order_id, strategy_id, symbol, "sell", qty, price, filled_qty, status, order_type
    )
    return close_journal(strategy_id, symbol, price, reason, qty=qty)


def close_journal(
    strategy_id: str,
    symbol: str,
    exit_price: float,
    reason: str = "",
    qty: float | None = None,
) -> int:
    """Settle the open journal rows a sleeve holds in one symbol.

    Pass `qty` for a partial exit. Selling 30 of 80 shares used to mark the
    whole position closed, so the journal showed flat while the ledger still
    held 50 — the open-risk and P&L views disagreed with reality.
    """
    try:
        s = _session()
    except Exception as exc:
        logger.warning("trade_log session failed: %s", exc)
        return 0
    try:
        if qty is not None and float(qty) > 0:
            partial = _close_journal_partial(s, strategy_id, symbol, exit_price, reason, float(qty))
            if partial is not None:
                return partial
        result = s.execute(
            _text(
                """
                UPDATE trade_journal
                SET status       = 'closed',
                    exit_price   = :px,
                    closed_at    = NOW(),
                    realized_pnl = (:px - entry_price) * qty,
                    return_pct   = CASE WHEN entry_price > 0
                                        THEN (:px / entry_price - 1) * 100 END,
                    holding_days = GREATEST(
                        0, EXTRACT(EPOCH FROM (NOW() - opened_at))::int / 86400
                    ),
                    signal_reason = COALESCE(signal_reason, '') ||
                                    CASE WHEN :reason = '' THEN '' ELSE ' | exit: ' || :reason END
                WHERE strategy_id = :sid
                  AND symbol      = :sym
                  AND side        = 'buy'
                  AND status      = 'open'
                """
            ),
            {"px": float(exit_price), "sid": strategy_id, "sym": symbol, "reason": reason},
        )
        s.commit()
        return int(result.rowcount or 0)
    except Exception as exc:
        s.rollback()
        logger.warning("trade_log close_journal %s/%s failed: %s", strategy_id, symbol, exc)
        return 0
    finally:
        s.close()


def _close_journal_partial(
    s, strategy_id: str, symbol: str, exit_price: float, reason: str, qty: float
) -> int | None:
    """Book a partial exit by splitting the open row.

    Returns None when the sale covers the whole open position, so the caller
    falls through to the simpler close-everything path.
    """
    rows = s.execute(
        _text(
            "SELECT trade_id, qty, entry_price, strategy_name, opened_at "
            "FROM trade_journal WHERE strategy_id = :sid AND symbol = :sym "
            "AND side = 'buy' AND status = 'open' ORDER BY opened_at"
        ),
        {"sid": strategy_id, "sym": symbol},
    ).fetchall()
    open_qty = sum(float(r[1] or 0) for r in rows)
    if not rows or qty >= open_qty - 1e-9:
        return None

    remaining = qty
    closed = 0
    for trade_id, row_qty, entry_price, strategy_name, opened_at in rows:
        if remaining <= 1e-9:
            break
        row_qty = float(row_qty or 0)
        take = min(row_qty, remaining)
        remaining -= take
        entry_price = float(entry_price or 0)

        # Shrink the position that is still held...
        s.execute(
            _text("UPDATE trade_journal SET qty = :q WHERE trade_id = :tid"),
            {"q": row_qty - take, "tid": trade_id},
        )
        # ...and record the slice that was sold as its own settled trade.
        s.execute(
            _text(
                """
                INSERT INTO trade_journal
                    (trade_id, strategy_id, strategy_name, symbol, side, signal_reason,
                     entry_price, exit_price, qty, status, opened_at, closed_at,
                     created_at, realized_pnl, return_pct, holding_days)
                VALUES
                    (:tid, :sid, :sname, :sym, 'buy', :reason,
                     :entry, :exit, :q, 'closed', :opened, NOW(),
                     NOW(), :pnl, :ret,
                     GREATEST(0, EXTRACT(EPOCH FROM (NOW() - :opened))::int / 86400))
                """
            ),
            {
                "tid": str(uuid.uuid4())[:12],
                "sid": strategy_id,
                "sname": strategy_name,
                "sym": symbol,
                "reason": f"partial exit: {reason}" if reason else "partial exit",
                "entry": entry_price,
                "exit": float(exit_price),
                "q": take,
                "opened": opened_at,
                "pnl": (float(exit_price) - entry_price) * take,
                "ret": (float(exit_price) / entry_price - 1) * 100 if entry_price > 0 else None,
            },
        )
        closed += 1
    s.commit()
    return closed


def mark_stale_journal(broker_held: set[str] | None = None) -> int:
    """Retire open journal rows with no matching position in the sleeve ledger.

    These are leftovers from before entries were only journalled on submit, or
    positions a broker bracket leg closed behind the runners. Their exit price
    is unknown, so they are marked 'stale' rather than given a fabricated P&L.

    Pass `broker_held` (symbols still at the broker) so we do not stale a row
    whose sleeve claim was briefly lost / orphaned to `__unassigned__` while
    shares are still live — runners should re-adopt those first.
    """
    try:
        s = _session()
    except Exception as exc:
        logger.warning("trade_log session failed: %s", exc)
        return 0
    try:
        held = {str(x).upper() for x in (broker_held or set()) if x}
        rows = s.execute(
            _text(
                """
                SELECT tj.id, UPPER(tj.symbol)
                FROM trade_journal tj
                WHERE tj.status = 'open'
                  AND NOT EXISTS (
                        SELECT 1 FROM strategy_positions sp
                        WHERE sp.strategy_id = tj.strategy_id
                          AND sp.symbol      = tj.symbol
                          AND sp.qty > 0
                  )
                  AND NOT EXISTS (
                        SELECT 1 FROM strategy_positions sp2
                        WHERE sp2.strategy_id = '__unassigned__'
                          AND sp2.symbol      = tj.symbol
                          AND sp2.qty > 0
                  )
                """
            )
        ).fetchall()
        ids = [int(r[0]) for r in rows if str(r[1]) not in held]
        if not ids:
            s.commit()
            return 0
        result = s.execute(
            _text(
                """
                UPDATE trade_journal
                SET status = 'stale', closed_at = NOW()
                WHERE id = ANY(:ids)
                """
            ),
            {"ids": ids},
        )
        s.commit()
        return int(result.rowcount or 0)
    except Exception as exc:
        s.rollback()
        logger.warning("trade_log mark_stale_journal failed: %s", exc)
        return 0
    finally:
        s.close()


def reopen_stale_journal(
    strategy_id: str,
    symbol: str,
    *,
    qty: float | None = None,
    entry_price: float | None = None,
) -> int:
    """Re-open the newest stale buy row after a sleeve re-adopt."""
    try:
        s = _session()
    except Exception as exc:
        logger.warning("trade_log session failed: %s", exc)
        return 0
    try:
        row = s.execute(
            _text(
                """
                SELECT id FROM trade_journal
                WHERE strategy_id = :sid AND symbol = :sym AND side = 'buy'
                  AND status = 'stale'
                ORDER BY COALESCE(closed_at, opened_at) DESC
                LIMIT 1
                """
            ),
            {"sid": strategy_id, "sym": symbol},
        ).fetchone()
        if not row:
            return 0
        params: dict[str, Any] = {"id": int(row[0])}
        sets = ["status = 'open'", "closed_at = NULL"]
        if qty is not None and float(qty) > 0:
            sets.append("qty = :qty")
            params["qty"] = float(qty)
        if entry_price is not None and float(entry_price) > 0:
            sets.append("entry_price = :px")
            params["px"] = float(entry_price)
        s.execute(
            _text(f"UPDATE trade_journal SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        s.commit()
        return 1
    except Exception as exc:
        s.rollback()
        logger.warning("trade_log reopen_stale_journal failed: %s", exc)
        return 0
    finally:
        s.close()


def recent_strategy_lots(
    strategy_id: str,
    *,
    lookback_days: int = 7,
    statuses: tuple[str, ...] = ("open", "stale"),
) -> list[dict[str, Any]]:
    """Recent buy lots for orphan re-adoption (newest first per symbol)."""
    try:
        s = _session()
    except Exception as exc:
        logger.warning("trade_log session failed: %s", exc)
        return []
    try:
        rows = s.execute(
            _text(
                """
                SELECT DISTINCT ON (symbol)
                       symbol, qty, entry_price, status, opened_at, signal_reason
                FROM trade_journal
                WHERE strategy_id = :sid
                  AND side = 'buy'
                  AND status = ANY(:statuses)
                  AND opened_at >= NOW() - (:days || ' days')::interval
                ORDER BY symbol, opened_at DESC
                """
            ),
            {
                "sid": strategy_id,
                "statuses": list(statuses),
                "days": int(lookback_days),
            },
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "symbol": str(r[0]),
                    "qty": float(r[1] or 0),
                    "entry_price": float(r[2] or 0),
                    "status": str(r[3] or ""),
                    "opened_at": r[4],
                    "signal_reason": r[5] or "",
                }
            )
        return out
    except Exception as exc:
        logger.warning("trade_log recent_strategy_lots failed: %s", exc)
        return []
    finally:
        s.close()
