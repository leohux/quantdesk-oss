# -*- coding: utf-8 -*-
"""Paper broker helpers + market clock."""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any

from execution.alpaca_client import AlpacaPaperClient
from execution.broker_factory import get_trading_client


def market_clock() -> dict[str, Any]:
    """Alpaca clock: is_open + next_open/next_close ISO timestamps."""
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    url = "https://paper-api.alpaca.markets/v2/clock"
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", secret)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {
        "is_open": bool(body.get("is_open")),
        "next_open": body.get("next_open"),
        "next_close": body.get("next_close"),
        "timestamp": body.get("timestamp"),
    }


def market_open() -> bool:
    return bool(market_clock().get("is_open"))


def seconds_until_open() -> float | None:
    """Seconds until next RTH open when closed; 0 if already open; None on parse fail."""
    clk = market_clock()
    if clk.get("is_open"):
        return 0.0
    nxt = clk.get("next_open")
    if not nxt:
        return None
    try:
        t0 = datetime.fromisoformat(str(nxt).replace("Z", "+00:00"))
        return max(0.0, (t0 - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback approx EDT
        return datetime.now(timezone.utc).astimezone(timezone.utc)


def in_premarket() -> bool:
    """True during 04:00–09:30 ET on a session that will open today.

    Uses Alpaca next_open so weekends/holidays do not arm premarket limits.
    """
    clk = market_clock()
    if clk.get("is_open"):
        return False
    et = _et_now()
    mins = et.hour * 60 + et.minute
    if not (4 * 60 <= mins < 9 * 60 + 30):
        return False
    nxt = clk.get("next_open")
    if not nxt:
        return False
    try:
        t0 = datetime.fromisoformat(str(nxt).replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo

            open_et = t0.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            open_et = t0
        return open_et.date() == et.date()
    except Exception:
        return False


def get_broker() -> AlpacaPaperClient:
    # Paper today; live reserved via broker_factory (refuses until implemented).
    return get_trading_client()


def position_map(broker: AlpacaPaperClient) -> dict[str, dict[str, Any]]:
    return {p["symbol"].upper(): p for p in broker.positions()}
