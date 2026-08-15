"""
Profit-protecting trailing stop helpers.
=======================================
Shared by intraday_runner (live) and trail_stop_replay (offline).

State lives on trade_journal open rows:
  peak_price, trail_active, trail_stop_price
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_ACTIVATE_PCT = 0.13
DEFAULT_TRAIL_PCT = 0.075
DEFAULT_DISABLE_HARD_TP_AFTER_TRAIL = False
DEFAULT_EXEMPT_TRAIL_ACTIVE_FROM_TIME_EXIT = True


def ensure_trail_columns() -> None:
    """Idempotent ALTER for trade_journal trail fields."""
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    stmts = [
        "ALTER TABLE trade_journal ADD COLUMN IF NOT EXISTS peak_price DOUBLE PRECISION",
        "ALTER TABLE trade_journal ADD COLUMN IF NOT EXISTS trail_active BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE trade_journal ADD COLUMN IF NOT EXISTS trail_stop_price DOUBLE PRECISION",
    ]
    s = SyncSessionLocal()
    try:
        for sql in stmts:
            s.execute(text(sql))
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def trail_params(params: dict[str, Any] | None) -> dict[str, Any]:
    p = params or {}
    return {
        "activate_pct": float(p.get("trail_activate_pct", DEFAULT_TRAIL_ACTIVATE_PCT)),
        "trail_pct": float(p.get("trail_pct", DEFAULT_TRAIL_PCT)),
        "disable_hard_tp_after_trail": bool(
            p.get("disable_hard_tp_after_trail", DEFAULT_DISABLE_HARD_TP_AFTER_TRAIL)
        ),
        "exempt_trail_from_time_exit": bool(
            p.get(
                "exempt_trail_active_from_time_exit",
                DEFAULT_EXEMPT_TRAIL_ACTIVE_FROM_TIME_EXIT,
            )
        ),
        # Live broker replaces only when trail_execute=True (dry-run default).
        "execute": bool(p.get("trail_execute", False)),
    }


def step_trail(
    *,
    entry_price: float,
    current_price: float,
    peak_price: float | None,
    trail_active: bool,
    trail_stop_price: float | None,
    activate_pct: float,
    trail_pct: float,
) -> dict[str, Any]:
    """Pure state transition for one price tick. Does not touch the broker."""
    entry = float(entry_price)
    px = float(current_price)
    peak = max(float(peak_price or entry), px, entry)
    active = bool(trail_active)
    stop = float(trail_stop_price) if trail_stop_price is not None else None
    events: list[str] = []
    gain = px / entry - 1.0 if entry > 0 else 0.0

    if not active and gain >= activate_pct:
        active = True
        stop = peak * (1.0 - trail_pct)
        events.append("TRAIL_ACTIVATE")

    if active:
        cand = peak * (1.0 - trail_pct)
        if stop is None or cand > stop + 1e-9:
            stop = cand
            if "TRAIL_ACTIVATE" not in events:
                events.append("TRAIL_UPDATE")

    return {
        "peak_price": peak,
        "trail_active": active,
        "trail_stop_price": stop,
        "gain_pct": gain,
        "events": events,
    }


def load_open_trail_rows(strategy_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        rows = s.execute(
            text(
                """
                SELECT id, trade_id, symbol, qty, entry_price, peak_price,
                       trail_active, trail_stop_price, opened_at
                FROM trade_journal
                WHERE strategy_id = :sid AND side = 'buy' AND status = 'open'
                ORDER BY opened_at
                """
            ),
            {"sid": strategy_id},
        ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        s.close()


def save_trail_state(
    row_id: int,
    *,
    peak_price: float,
    trail_active: bool,
    trail_stop_price: float | None,
) -> None:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        s.execute(
            text(
                """
                UPDATE trade_journal
                SET peak_price = :peak,
                    trail_active = :active,
                    trail_stop_price = :stop
                WHERE id = :id AND status = 'open'
                """
            ),
            {
                "id": int(row_id),
                "peak": float(peak_price),
                "active": bool(trail_active),
                "stop": float(trail_stop_price) if trail_stop_price is not None else None,
            },
        )
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def seed_peak_from_entry(strategy_id: str) -> int:
    """Backfill peak_price = entry_price for open rows missing a peak."""
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        result = s.execute(
            text(
                """
                UPDATE trade_journal
                SET peak_price = entry_price
                WHERE strategy_id = :sid AND status = 'open'
                  AND (peak_price IS NULL OR peak_price <= 0)
                """
            ),
            {"sid": strategy_id},
        )
        s.commit()
        return int(result.rowcount or 0)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def trail_active_symbols(strategy_id: str) -> set[str]:
    return {
        str(r["symbol"]).upper()
        for r in load_open_trail_rows(strategy_id)
        if r.get("trail_active")
    }
