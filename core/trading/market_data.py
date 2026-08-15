"""Market Data Stream - historical replay and live data interface."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Bar:
    """Single OHLCV bar."""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume,
        }


BarHandler = Callable[[Bar], None]


class MarketDataStream(ABC):
    """Abstract market data stream."""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def subscribe(self, symbol: str, handler: BarHandler) -> None:
        ...

    @abstractmethod
    def unsubscribe(self, symbol: str) -> None:
        ...

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float | None:
        ...


class HistoricalReplayStream(MarketDataStream):
    """Replay historical OHLCV data bar-by-bar.

    Used for paper trading with historical data.
    """

    def __init__(self, speed: float = 0.0) -> None:
        """
        Args:
            speed: Seconds between bars. 0 = as fast as possible.
        """
        self._speed = speed
        self._handlers: dict[str, list[BarHandler]] = {}
        self._data: dict[str, pd.DataFrame] = {}
        self._latest_prices: dict[str, float] = {}
        self._running = False
        self._current_idx: dict[str, int] = {}

    def load_data(self, symbol: str, ohlcv: pd.DataFrame) -> None:
        """Load OHLCV DataFrame for replay."""
        self._data[symbol.upper()] = ohlcv
        self._current_idx[symbol.upper()] = 0
        logger.info("Loaded %d bars for %s", len(ohlcv), symbol)

    def load_from_provider(
        self,
        symbol: str,
        provider: Any,
        start: str = "2020-01-01",
        end: str | None = None,
    ) -> None:
        """Load data using a DataProvider."""
        ohlcv = provider.get_bars(symbol, start, end)
        self.load_data(symbol, ohlcv)

    def start(self) -> None:
        """Start replaying data."""
        self._running = True

    def stop(self) -> None:
        self._running = False

    def subscribe(self, symbol: str, handler: BarHandler) -> None:
        self._handlers.setdefault(symbol.upper(), []).append(handler)

    def unsubscribe(self, symbol: str) -> None:
        self._handlers.pop(symbol.upper(), None)

    def get_latest_price(self, symbol: str) -> float | None:
        return self._latest_prices.get(symbol.upper())

    def get_all_latest_prices(self) -> dict[str, float]:
        return dict(self._latest_prices)

    def replay_next(self) -> bool:
        """Replay the next bar for all symbols. Returns False when exhausted."""
        if not self._running:
            return False

        any_data = False
        for symbol, df in self._data.items():
            idx = self._current_idx.get(symbol, 0)
            if idx >= len(df):
                continue

            any_data = True
            row = df.iloc[idx]
            ts = df.index[idx]
            if isinstance(ts, pd.Timestamp):
                ts = ts.to_pydatetime()

            bar = Bar(
                symbol=symbol,
                timestamp=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )

            self._latest_prices[symbol] = bar.close
            self._current_idx[symbol] = idx + 1

            for handler in self._handlers.get(symbol, []):
                try:
                    handler(bar)
                except Exception:
                    logger.exception("Bar handler error for %s", symbol)

        return any_data

    def replay_all(self) -> int:
        """Replay all remaining bars. Returns total bars replayed."""
        count = 0
        while self.replay_next():
            count += 1
        return count

    @property
    def is_finished(self) -> bool:
        for symbol, df in self._data.items():
            if self._current_idx.get(symbol, 0) < len(df):
                return False
        return True

    @property
    def progress(self) -> dict[str, dict[str, int]]:
        return {
            symbol: {"current": self._current_idx.get(symbol, 0), "total": len(df)}
            for symbol, df in self._data.items()
        }


class LiveDataStream(MarketDataStream):
    """Live market data stream (interface for Phase 4).

    Placeholder for real-time data from IBKR, Alpaca, etc.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[BarHandler]] = {}
        self._latest_prices: dict[str, float] = {}

    def start(self) -> None:
        raise NotImplementedError("Live data stream not yet implemented (Phase 4)")

    def stop(self) -> None:
        pass

    def subscribe(self, symbol: str, handler: BarHandler) -> None:
        self._handlers.setdefault(symbol.upper(), []).append(handler)

    def unsubscribe(self, symbol: str) -> None:
        self._handlers.pop(symbol.upper(), None)

    def get_latest_price(self, symbol: str) -> float | None:
        return self._latest_prices.get(symbol.upper())
