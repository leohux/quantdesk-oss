"""Abstract base for market data providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import pandas as pd


OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalize any DataFrame to standard OHLCV format."""
    if df.empty:
        raise ValueError(f"No data returned for {symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    rename = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume", "timestamp": "Date",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns for {symbol}: {missing}")

    out = df[OHLCV_COLUMNS].copy()
    out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    out = out.sort_index()
    out["Symbol"] = symbol.upper()
    return out


class DataProvider(ABC):
    """Abstract market data provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'yfinance', 'alpaca')."""
        ...

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars. Returns normalized DataFrame."""
        ...

    def is_available(self) -> bool:
        """Check if provider is configured and ready."""
        return True
