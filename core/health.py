"""System health-check module for the quant trading platform.

Each ``check_*`` function is self-contained, times out after 5 seconds,
and returns a dict with ``status`` ("ok" | "error"), ``latency_ms``,
and ``message``.  Import failures for optional dependencies are handled
gracefully.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional imports — handled gracefully
# ---------------------------------------------------------------------------
try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore[assignment]

try:
    import redis as _redis_mod
except ImportError:
    _redis_mod = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Connection strings (from env or sensible defaults)
# ---------------------------------------------------------------------------
SYNC_DATABASE_URL: str = os.getenv(
    "SYNC_DATABASE_URL",
    "postgresql://quantdesk:quantdesk@quantdesk-postgres:5432/quantdesk",
)
REDIS_URL: str = os.getenv("REDIS_URL", "redis://quantdesk-redis:6379")

ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.getenv(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
)

_TIMEOUT = 5  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(latency_ms: float, msg: str = "ok") -> dict[str, Any]:
    return {"status": "ok", "latency_ms": round(latency_ms, 2), "message": msg}


def _err(latency_ms: float, msg: str) -> dict[str, Any]:
    return {"status": "error", "latency_ms": round(latency_ms, 2), "message": msg}


def _time_it():
    """Return (start_time, elapsed_ms_callable)."""
    t0 = time.monotonic()
    return t0, lambda: (time.monotonic() - t0) * 1000


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_postgres() -> dict[str, Any]:
    if psycopg2 is None:
        return _err(0, "psycopg2 not installed")
    _, elapsed = _time_it()
    try:
        conn = psycopg2.connect(SYNC_DATABASE_URL, connect_timeout=_TIMEOUT)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return _ok(elapsed())
    except Exception as exc:
        return _err(elapsed(), str(exc))


def check_redis() -> dict[str, Any]:
    if _redis_mod is None:
        return _err(0, "redis-py not installed")
    _, elapsed = _time_it()
    try:
        r = _redis_mod.from_url(REDIS_URL, socket_connect_timeout=_TIMEOUT, socket_timeout=_TIMEOUT)
        r.ping()
        r.close()
        return _ok(elapsed())
    except Exception as exc:
        return _err(elapsed(), str(exc))


def check_broker() -> dict[str, Any]:
    """Check Alpaca API connectivity."""
    if httpx is None:
        return _err(0, "httpx not installed")
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return _err(0, "Alpaca API keys not configured")
    _, elapsed = _time_it()
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                f"{ALPACA_BASE_URL}/v2/account",
                headers={
                    "APCA-API-KEY-ID": ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                },
            )
        if resp.status_code == 200:
            return _ok(elapsed())
        return _err(elapsed(), f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as exc:
        return _err(elapsed(), str(exc))


def check_market_data() -> dict[str, Any]:
    """Check if we can fetch a simple quote via yfinance."""
    if yf is None:
        return _err(0, "yfinance not installed")
    _, elapsed = _time_it()
    try:
        ticker = yf.Ticker("AAPL")
        info = ticker.fast_info  # lightweight call
        price = getattr(info, "last_price", None)
        if price and price > 0:
            return _ok(elapsed(), f"AAPL=${price:.2f}")
        # fallback: try .info
        price2 = ticker.info.get("regularMarketPrice")
        if price2:
            return _ok(elapsed(), f"AAPL=${price2:.2f}")
        return _err(elapsed(), "Could not retrieve price")
    except Exception as exc:
        return _err(elapsed(), str(exc))


# ---------------------------------------------------------------------------
# Full health check
# ---------------------------------------------------------------------------

def full_health_check() -> dict[str, Any]:
    """Run all checks and return a combined report."""
    pg = check_postgres()
    rd = check_redis()
    br = check_broker()
    md = check_market_data()

    # Determine overall status
    critical_fail = pg["status"] == "error" or rd["status"] == "error"
    any_fail = any(
        c["status"] == "error" for c in (pg, rd, br, md)
    )

    if critical_fail:
        overall = "error"
    elif any_fail:
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "postgres": pg,
        "redis": rd,
        "broker": br,
        "market_data": md,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
    }
