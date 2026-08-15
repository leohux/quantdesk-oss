# -*- coding: utf-8 -*-
"""Alpaca news feed (HTTP v1beta1) — bootstrap / WS fallback only."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from .news_inbox import INBOX, normalize_news


def _keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    return key, secret


def fetch_news(
    symbols: list[str],
    *,
    limit: int = 50,
    hours_back: float = 18.0,
) -> list[dict[str, Any]]:
    """Fetch recent news via REST; filter client-side to tech symbols."""
    key, secret = _keys()
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    received_at = datetime.now(timezone.utc).isoformat()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    chunk = 30
    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        qs = urllib.parse.urlencode(
            {
                "symbols": ",".join(batch),
                "start": start,
                "limit": min(limit, 50),
                "include_content": "true",
                "exclude_contentless": "true",
            }
        )
        url = f"https://data.alpaca.markets/v1beta1/news?{qs}"
        req = urllib.request.Request(url)
        req.add_header("APCA-API-KEY-ID", key)
        req.add_header("APCA-API-SECRET-KEY", secret)
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        for n in body.get("news") or []:
            nid = str(n.get("id") or "")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            item = normalize_news(
                {
                    "id": nid,
                    "headline": n.get("headline"),
                    "summary": n.get("summary"),
                    "symbols": n.get("symbols") or [],
                    "created_at": n.get("created_at"),
                    "url": n.get("url"),
                    "source": n.get("source"),
                    "received_at": received_at,
                },
                feed="rest",
            )
            if item:
                out.append(item)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out[:limit]


def bootstrap_inbox(
    symbols: list[str],
    *,
    limit: int = 60,
    hours_back: float = 18.0,
) -> int:
    """One-shot REST seed into the inbox (covers gap before WS is warm)."""
    items = fetch_news(symbols, limit=limit, hours_back=hours_back)
    n = 0
    for item in items:
        # re-tag so audit can tell bootstrap from live REST fallback
        item = dict(item)
        item["feed"] = "rest_bootstrap"
        if INBOX.push(item):
            n += 1
    return n
