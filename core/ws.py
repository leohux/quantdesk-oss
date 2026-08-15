from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from core.auth import authenticate_token

logger = logging.getLogger(__name__)

# Valid channels clients may subscribe to
CHANNELS: set[str] = {"orders", "positions", "pnl", "health", "logs", "strategies"}


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Manages WebSocket connections grouped by channel name."""

    def __init__(self) -> None:
        self.active_connections: dict[str, set[WebSocket]] = {
            ch: set() for ch in CHANNELS
        }

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = set()
        self.active_connections[channel].add(websocket)
        logger.info("WS connected to channel=%s  total=%d", channel, len(self.active_connections[channel]))

    def disconnect(self, websocket: WebSocket, channel: str) -> None:
        conns = self.active_connections.get(channel)
        if conns:
            conns.discard(websocket)
            logger.info("WS disconnected from channel=%s  total=%d", channel, len(conns))

    async def broadcast(self, channel: str, data: dict[str, Any]) -> None:
        """Send *data* as JSON to every client on *channel*, removing dead sockets."""
        conns = self.active_connections.get(channel, set()).copy()
        message = json.dumps(data, default=str)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, channel)

    async def broadcast_all(self, data: dict[str, Any]) -> None:
        """Send *data* to every channel."""
        for channel in self.active_connections:
            await self.broadcast(channel, data)


# Singleton used throughout the application
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

async def ws_endpoint(websocket: WebSocket, channel: str) -> None:
    """FastAPI WebSocket endpoint.

    Validates ``?token=`` on connect (JWT access *or* legacy ACCESS_TOKEN),
    then keeps the socket alive with ping/pong until the client disconnects.
    """
    if channel not in CHANNELS:
        await websocket.close(code=4004, reason=f"Unknown channel: {channel}")
        return

    # Authenticate via query param (same rules as HTTP AccessTokenMiddleware)
    token: str | None = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    auth = authenticate_token(token)
    if auth is None:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await manager.connect(websocket, channel)
    try:
        while True:
            # Wait for client messages; pings are handled automatically by Starlette.
            # We also accept text frames (e.g. heartbeat / subscription changes).
            data = await websocket.receive_text()
            # Echo back a pong-style acknowledgement so the client knows we're alive
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WS error on channel=%s", channel, exc_info=True)
    finally:
        manager.disconnect(websocket, channel)


# ---------------------------------------------------------------------------
# Convenience helper for other modules
# ---------------------------------------------------------------------------

async def broadcast_event(channel: str, data: dict[str, Any]) -> None:
    """Push an event to all subscribers of *channel*. Safe to call from anywhere."""
    await manager.broadcast(channel, data)


# ---------------------------------------------------------------------------
# Background broadcaster
# ---------------------------------------------------------------------------

# Broker snapshots older than this are refetched. Keep above the 2s broadcast
# interval so a slow Alpaca response cannot spiral into rate limiting.
PORTFOLIO_TTL_SEC = 5.0


def _fetch_portfolio() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from execution.alpaca_client import AlpacaPaperClient

    client = AlpacaPaperClient()
    return client.positions(), client.account()


def _enrich_positions(
    positions: list[dict[str, Any]],
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach weight_pct + sleeve ownership (same shape as /api/dashboard)."""
    if not positions:
        return positions
    try:
        from core.portfolio.pnl import portfolio_pnl

        equity = max(float(account.get("equity") or 0), 1e-9)
        ownership = (portfolio_pnl(positions) or {}).get("ownership") or {}
        for p in positions:
            mv = float(p.get("market_value") or 0)
            p["weight_pct"] = round(mv / equity * 100, 2)
            owner = ownership.get(str(p.get("symbol", "")).upper())
            p["strategy_id"] = owner["strategy_id"] if owner else None
            p["strategy_name"] = owner["strategy_name"] if owner else None
    except Exception as exc:
        logger.debug("WS position enrich skipped: %s", exc)
    return positions


async def _portfolio_snapshot(app: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (positions, account), refetching once the cached copy expires.

    On broker failure the previous snapshot is reused so the UI degrades to
    stale-but-labelled data instead of blanking out.
    """
    positions = getattr(app.state, "positions", None)
    account = getattr(app.state, "account", None)
    fetched_at = getattr(app.state, "portfolio_fetched_at", None)

    now = time.monotonic()
    cached_ok = isinstance(positions, list) and isinstance(account, dict) and bool(account)
    if cached_ok and fetched_at is not None and (now - fetched_at) < PORTFOLIO_TTL_SEC:
        return positions, account

    try:
        # Alpaca's SDK is blocking; keep it off the event loop.
        positions, account = await asyncio.to_thread(_fetch_portfolio)
        positions = _enrich_positions(positions or [], account or {})
        app.state.positions = positions
        app.state.account = account
        app.state.portfolio_fetched_at = now
        app.state.portfolio_stale = False
    except Exception as exc:
        logger.warning("WS portfolio refresh failed: %s", exc)
        app.state.portfolio_stale = True
        positions = positions if isinstance(positions, list) else []
        account = account if isinstance(account, dict) else {}

    return positions, account


def start_broadcaster(app: Any) -> None:
    """Attach a background task to *app* (a FastAPI instance) that periodically
    fetches live data and broadcasts it over WebSocket channels.

    Call once during application startup::

        @app.on_event("startup")
        async def on_startup():
            start_broadcaster(app)
    """

    async def _broadcaster_loop() -> None:
        tick = 0
        while True:
            try:
                now = datetime.now(timezone.utc).isoformat()

                # --- Every 2 seconds: positions & P&L ---
                if tick % 2 == 0:
                    positions, account = await _portfolio_snapshot(app)
                    fetched_at = getattr(app.state, "portfolio_fetched_at", None)
                    positions_data: dict[str, Any] = {
                        "event": "positions_update",
                        "timestamp": now,
                        "positions": positions or [],
                        "account": account or {},
                        "stale": bool(getattr(app.state, "portfolio_stale", False)),
                        "age_sec": round(time.monotonic() - fetched_at, 1)
                        if fetched_at is not None
                        else None,
                    }
                    await manager.broadcast("positions", positions_data)
                    await manager.broadcast("pnl", positions_data)
                    # Throttled inside record_equity_snapshot (~5 min).
                    if account and tick % 60 == 0:
                        try:
                            from core.portfolio.pnl import record_equity_snapshot

                            record_equity_snapshot(
                                float(account.get("equity") or 0),
                                float(account.get("cash") or 0),
                                float(account.get("last_equity") or 0),
                                source="ws",
                            )
                        except Exception:
                            pass

                # --- Every 5 seconds: health ---
                if tick % 5 == 0:
                    health_data: dict[str, Any] = {
                        "event": "health_update",
                        "timestamp": now,
                        "status": "ok",
                        "details": getattr(app.state, "health", {}),
                    }
                    await manager.broadcast("health", health_data)

                tick += 1
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Broadcaster task cancelled")
                break
            except Exception:
                logger.exception("Broadcaster loop error")
                await asyncio.sleep(2)

    @app.on_event("startup")
    async def _start() -> None:  # type: ignore[misc]
        app.state.broadcaster_task = asyncio.create_task(_broadcaster_loop())
        logger.info("WebSocket broadcaster started")

    @app.on_event("shutdown")
    async def _stop() -> None:  # type: ignore[misc]
        task: asyncio.Task | None = getattr(app.state, "broadcaster_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket broadcaster stopped")
