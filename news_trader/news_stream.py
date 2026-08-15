# -*- coding: utf-8 -*-
"""Alpaca Benzinga news WebSocket → NewsInbox (background thread).

URL: wss://stream.data.alpaca.markets/v1beta1/news
Pushes wake the main scoring loop (event-driven); INTERVAL is only a max sleep.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .news_inbox import INBOX
from .universe import TECH_SET

WS_URL = os.environ.get(
    "NEWS_TRADER_NEWS_WS_URL",
    "wss://stream.data.alpaca.markets/v1beta1/news",
)
# "*" = all news, filter to TECH_SET client-side (simplest; plan-dependent)
SUBSCRIBE_ALL = os.environ.get("NEWS_TRADER_NEWS_WS_ALL", "1") == "1"

_log: Callable[[str], None] = print
_thread: threading.Thread | None = None
_stop = threading.Event()
_state_lock = threading.Lock()
_connected = False
_last_msg_ts = 0.0
_auth_ok = False


def _keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    return key, secret


def is_connected() -> bool:
    with _state_lock:
        return _connected and _auth_ok


def last_msg_age_sec() -> float | None:
    with _state_lock:
        if _last_msg_ts <= 0:
            return None
        return max(0.0, time.time() - _last_msg_ts)


def _touch(connected: bool | None = None, auth: bool | None = None, msg: bool = False) -> None:
    global _connected, _auth_ok, _last_msg_ts
    with _state_lock:
        if connected is not None:
            _connected = connected
        if auth is not None:
            _auth_ok = auth
        if msg:
            _last_msg_ts = time.time()


def _relevant(symbols: list[str]) -> bool:
    if not symbols:
        return False
    return any(s in TECH_SET for s in symbols)


def _handle_messages(msgs: list[Any]) -> None:
    for m in msgs:
        if not isinstance(m, dict):
            continue
        t = m.get("T")
        if t == "success":
            msg = (m.get("msg") or "").lower()
            if msg == "authenticated":
                _touch(auth=True)
                _log("news-ws authenticated")
            elif msg == "connected":
                _touch(connected=True)
            continue
        if t == "error":
            _log(f"news-ws error: {m}")
            continue
        if t == "subscription":
            _log(f"news-ws subscribed news={m.get('news')}")
            continue
        if t != "n":
            continue
        _touch(msg=True)
        symbols = [str(s).upper() for s in (m.get("symbols") or [])]
        if not _relevant(symbols):
            continue
        raw = {
            "id": m.get("id"),
            "headline": m.get("headline"),
            "summary": m.get("summary"),
            "symbols": symbols,
            "created_at": m.get("created_at"),
            "url": m.get("url"),
            "source": m.get("source"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        if INBOX.push_raw(raw, feed="ws"):
            _log(
                f"news-ws + {raw['id']} "
                f"{(raw.get('headline') or '')[:60]}"
            )


async def _session() -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    key, secret = _keys()
    backoff = 1.0
    while not _stop.is_set():
        try:
            _touch(connected=False, auth=False)
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            ) as ws:
                await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                # Wait for authenticated before subscribe
                authed = False
                for _ in range(20):
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    data = json.loads(raw)
                    batch = data if isinstance(data, list) else [data]
                    _handle_messages(batch)
                    if any(
                        isinstance(m, dict)
                        and m.get("T") == "success"
                        and (m.get("msg") or "").lower() == "authenticated"
                        for m in batch
                    ):
                        authed = True
                        break
                if not authed:
                    raise RuntimeError("news-ws auth timeout")
                if SUBSCRIBE_ALL:
                    news_filter: list[str] = ["*"]
                else:
                    news_filter = sorted(TECH_SET)
                await ws.send(json.dumps({"action": "subscribe", "news": news_filter}))
                _log(f"news-ws subscribed filter={news_filter[0] if len(news_filter)==1 else len(news_filter)}")
                backoff = 1.0
                while not _stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(data, list):
                        _handle_messages(data)
                    elif isinstance(data, dict):
                        _handle_messages([data])
        except ConnectionClosed as exc:
            _touch(connected=False, auth=False)
            _log(f"news-ws closed: {exc}; retry in {backoff:.0f}s")
        except Exception as exc:
            _touch(connected=False, auth=False)
            _log(f"news-ws fail: {exc}; retry in {backoff:.0f}s")
        if _stop.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 1.7)


def _thread_main() -> None:
    try:
        asyncio.run(_session())
    except Exception as exc:
        _log(f"news-ws thread died: {exc}")


def start_news_stream(log: Callable[[str], None] | None = None) -> None:
    """Start background WS consumer once per process."""
    global _thread, _log
    if log is not None:
        _log = log
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_thread_main, name="alpaca-news-ws", daemon=True)
    _thread.start()
    _log("news-ws thread started")


def stop_news_stream() -> None:
    _stop.set()
