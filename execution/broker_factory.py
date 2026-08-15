"""Broker client factory — paper today, live reserved for later.

Runners (intraday / phase6 / news) should obtain Alpaca clients through
``get_trading_client()`` instead of constructing ``AlpacaPaperClient``
directly. Live mode is intentionally not wired to a real-money client yet;
asking for it fails loudly so nobody can flip an env var and silently go live.
"""
from __future__ import annotations

import os
from typing import Any

from execution.alpaca_client import AlpacaPaperClient

# Future: "live" once AlpacaLiveClient + LiveTradingGuard hard-locks are ready.
SUPPORTED_MODES = frozenset({"paper"})
RESERVED_MODES = frozenset({"live", "real", "prod"})


class LiveBrokerNotReady(RuntimeError):
    """Raised when code asks for live Alpaca before the interface is implemented."""


def trading_mode(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("ALPACA_TRADING_MODE") or "paper").strip().lower()
    return raw or "paper"


def get_trading_client(*, mode: str | None = None) -> Any:
    """Return a paper Alpaca client. Live is reserved and refused."""
    m = trading_mode(mode)
    if m in RESERVED_MODES:
        raise LiveBrokerNotReady(
            f"ALPACA_TRADING_MODE={m!r} is reserved but not implemented. "
            "Keep ALPACA_TRADING_MODE=paper. Live requires a dedicated "
            "AlpacaLiveClient + LIVE_TRADING_ENABLED hard locks (not wired yet)."
        )
    if m not in SUPPORTED_MODES:
        raise ValueError(
            f"Unknown ALPACA_TRADING_MODE={m!r}. Supported: {sorted(SUPPORTED_MODES)}. "
            f"Reserved (not ready): {sorted(RESERVED_MODES)}."
        )
    return AlpacaPaperClient()
