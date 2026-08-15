"""Live Data Adapter abstraction for real-time market data feeds.

Provides a common interface (DataAdapter) that concrete adapters implement
to supply live OHLCV bars to the trading engine. Ships with a MockDataAdapter
for testing / development and stubs for Alpaca and IBKR.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

import numpy as np

from core.trading.market_data import Bar

logger = logging.getLogger(__name__)

BarCallback = Callable[[Bar], None]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class DataAdapter(ABC):
    """Base class every live-data adapter must implement."""

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """Begin receiving data (background work starts here)."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Tear down connections / threads."""
        ...

    # -- subscriptions ------------------------------------------------------

    @abstractmethod
    def subscribe(self, symbol: str, on_bar_callback: BarCallback) -> None:
        """Register *on_bar_callback* to be called for every new bar on *symbol*."""
        ...

    @abstractmethod
    def unsubscribe(self, symbol: str) -> None:
        """Remove all callbacks for *symbol*."""
        ...

    # -- price access -------------------------------------------------------

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float | None:
        """Return the most recent close price for *symbol*, or ``None``."""
        ...

    @abstractmethod
    def get_all_prices(self) -> dict[str, float]:
        """Return ``{symbol: latest_price}`` for every subscribed symbol."""
        ...

    # -- status -------------------------------------------------------------

    @abstractmethod
    def is_connected(self) -> bool:
        """``True`` when the adapter is actively delivering data."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name (e.g. ``'MockDataAdapter'``)."""
        ...


# ---------------------------------------------------------------------------
# Mock adapter – random-walk prices for development / testing
# ---------------------------------------------------------------------------

class MockDataAdapter(DataAdapter):
    """Generates synthetic OHLCV bars using a geometric random walk.

    Parameters
    ----------
    tick_interval:
        Seconds between bar emissions (default ``1.0``).
    volatility:
        Per-bar standard deviation of log-returns (default ``0.02``).
    base_price:
        Starting price for every newly subscribed symbol (default ``100``).
    """

    def __init__(
        self,
        tick_interval: float = 1.0,
        volatility: float = 0.02,
        base_price: float = 100.0,
    ) -> None:
        self._tick_interval = tick_interval
        self._volatility = volatility
        self._base_price = base_price

        self._callbacks: dict[str, list[BarCallback]] = {}
        self._latest_prices: dict[str, float] = {}
        self._current_prices: dict[str, float] = {}

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = False
        self._lock = threading.Lock()

    # -- name ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return "MockDataAdapter"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MockDataAdapter already running")
            return
        self._stop_event.clear()
        self._connected = True
        self._thread = threading.Thread(
            target=self._run_loop, name="mock-data-adapter", daemon=True
        )
        self._thread.start()
        logger.info(
            "MockDataAdapter started (interval=%.2fs, vol=%.4f, base=%.2f)",
            self._tick_interval,
            self._volatility,
            self._base_price,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_interval * 3)
            self._thread = None
        self._connected = False
        logger.info("MockDataAdapter stopped")

    # -- subscriptions ------------------------------------------------------

    def subscribe(self, symbol: str, on_bar_callback: BarCallback) -> None:
        sym = symbol.upper()
        with self._lock:
            self._callbacks.setdefault(sym, []).append(on_bar_callback)
            if sym not in self._current_prices:
                self._current_prices[sym] = self._base_price
                self._latest_prices[sym] = self._base_price
        logger.debug("MockDataAdapter subscribed to %s", sym)

    def unsubscribe(self, symbol: str) -> None:
        sym = symbol.upper()
        with self._lock:
            self._callbacks.pop(sym, None)
            self._current_prices.pop(sym, None)
            self._latest_prices.pop(sym, None)
        logger.debug("MockDataAdapter unsubscribed from %s", sym)

    # -- price access -------------------------------------------------------

    def get_latest_price(self, symbol: str) -> float | None:
        return self._latest_prices.get(symbol.upper())

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._latest_prices)

    # -- status -------------------------------------------------------------

    def is_connected(self) -> bool:
        return self._connected

    # -- internal -----------------------------------------------------------

    def _run_loop(self) -> None:
        """Background loop: emit one bar per symbol every *tick_interval*."""
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(timeout=self._tick_interval)

    def _tick(self) -> None:
        """Generate a single bar for every subscribed symbol and dispatch."""
        now = datetime.utcnow()
        with self._lock:
            symbols = list(self._callbacks.keys())

        for symbol in symbols:
            with self._lock:
                price = self._current_prices.get(symbol, self._base_price)
                callbacks = list(self._callbacks.get(symbol, []))

            if not callbacks:
                continue

            # geometric random walk
            ret = np.random.normal(0, self._volatility)
            new_close = price * np.exp(ret)

            # build an intra-tick OHLCV bar
            open_ = price
            close = new_close
            high = max(open_, close) * (1 + abs(np.random.normal(0, self._volatility / 4)))
            low = min(open_, close) * (1 - abs(np.random.normal(0, self._volatility / 4)))
            volume = float(np.random.randint(1_000, 100_000))

            bar = Bar(
                symbol=symbol,
                timestamp=now,
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=volume,
            )

            with self._lock:
                self._current_prices[symbol] = close
                self._latest_prices[symbol] = close

            for cb in callbacks:
                try:
                    cb(bar)
                except Exception:
                    logger.exception("Callback error for %s", symbol)


# ---------------------------------------------------------------------------
# Alpaca stub
# ---------------------------------------------------------------------------

class AlpacaDataAdapter(DataAdapter):
    """Live adapter for Alpaca Markets API.  **Not yet implemented.**"""

    @property
    def name(self) -> str:
        return "AlpacaDataAdapter"

    def start(self) -> None:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def stop(self) -> None:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def subscribe(self, symbol: str, on_bar_callback: BarCallback) -> None:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def unsubscribe(self, symbol: str) -> None:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def get_latest_price(self, symbol: str) -> float | None:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def get_all_prices(self) -> dict[str, float]:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")

    def is_connected(self) -> bool:
        raise NotImplementedError("AlpacaDataAdapter is not yet implemented")


# ---------------------------------------------------------------------------
# IBKR stub
# ---------------------------------------------------------------------------

class IBKRDataAdapter(DataAdapter):
    """Live adapter for Interactive Brokers TWS / Gateway.  **Not yet implemented.**"""

    @property
    def name(self) -> str:
        return "IBKRDataAdapter"

    def start(self) -> None:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def stop(self) -> None:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def subscribe(self, symbol: str, on_bar_callback: BarCallback) -> None:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def unsubscribe(self, symbol: str) -> None:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def get_latest_price(self, symbol: str) -> float | None:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def get_all_prices(self) -> dict[str, float]:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")

    def is_connected(self) -> bool:
        raise NotImplementedError("IBKRDataAdapter is not yet implemented")
