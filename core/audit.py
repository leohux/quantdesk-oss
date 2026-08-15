"""Unified audit-logging module for the quant trading platform.

All audit entries are written to the ``audit_log`` table via psycopg2
(synchronous).  Every public function is wrapped in a try/except so that
a logging failure **never** crashes the caller.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# connection helper
# ---------------------------------------------------------------------------
try:
    import psycopg2  # noqa: F811
except ImportError:  # pragma: no cover – gracefully degrade
    psycopg2 = None  # type: ignore[assignment]

SYNC_DATABASE_URL: str = os.getenv(
    "SYNC_DATABASE_URL",
    "postgresql://quantdesk:quantdesk@quantdesk-postgres:5432/quantdesk",
)


def _conn():
    """Return a new psycopg2 connection (caller must close)."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    return psycopg2.connect(SYNC_DATABASE_URL)


# ---------------------------------------------------------------------------
# Action-type constants
# ---------------------------------------------------------------------------
# Orders
ORDER_CREATED   = "ORDER_CREATED"
ORDER_FILLED    = "ORDER_FILLED"
ORDER_CANCELLED = "ORDER_CANCELLED"
ORDER_REJECTED  = "ORDER_REJECTED"

# Signals & risk
SIGNAL_GENERATED = "SIGNAL_GENERATED"
RISK_REJECTED    = "RISK_REJECTED"
BROKER_REJECTED  = "BROKER_REJECTED"

# Positions
POSITION_CLOSED = "POSITION_CLOSED"

# Strategy lifecycle
STRATEGY_STARTED = "STRATEGY_STARTED"
STRATEGY_STOPPED = "STRATEGY_STOPPED"

# Config / auth
SETTINGS_CHANGED = "SETTINGS_CHANGED"
USER_LOGIN       = "USER_LOGIN"
USER_LOGOUT      = "USER_LOGOUT"

# System
SYSTEM_STARTUP  = "SYSTEM_STARTUP"
SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_event(user: str, action: str, detail: str = "", ip: str = "") -> None:
    """Insert a single audit entry.  Never raises — errors are logged."""
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO audit_log ("user", action, detail, ip) '
                    "VALUES (%s, %s, %s, %s)",
                    (user, action, detail, ip),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.error("audit.log_event failed (action=%s): %s", action, exc)


def get_audit_log(limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    """Return the most recent audit entries, optionally filtered by *action*.

    Returns a list of dicts with keys: ``id``, ``user``, ``action``,
    ``detail``, ``ip``, ``created_at``.  On any error an empty list is
    returned (never raises).
    """
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                if action:
                    cur.execute(
                        'SELECT id, "user", action, detail, ip, created_at '
                        "FROM audit_log WHERE action = %s "
                        "ORDER BY id DESC LIMIT %s",
                        (action, limit),
                    )
                else:
                    cur.execute(
                        'SELECT id, "user", action, detail, ip, created_at '
                        "FROM audit_log ORDER BY id DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "user": r[1],
                    "action": r[2],
                    "detail": r[3] or "",
                    "ip": r[4] or "",
                    "created_at": r[5].isoformat() if isinstance(r[5], datetime) else str(r[5]),
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        log.error("audit.get_audit_log failed: %s", exc)
        return []
