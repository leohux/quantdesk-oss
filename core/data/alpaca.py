"""Alpaca data provider."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from core.data.base import DataProvider, normalize_ohlcv


class AlpacaProvider(DataProvider):
    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key

    @property
    def name(self) -> str:
        return "alpaca"

    def is_available(self) -> bool:
        return bool(self._api_key and self._secret_key)

    def get_bars(
        self,
        symbol: str,
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(self._api_key, self._secret_key)
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = (
            datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            if end
            else datetime.now(timezone.utc)
        )
        req = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )
        bars = client.get_stock_bars(req).df
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol.upper(), level=0)
        return normalize_ohlcv(bars, symbol)
