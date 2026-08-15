"""YFinance data provider."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

from core.data.base import DataProvider, normalize_ohlcv


class YFinanceProvider(DataProvider):
    @property
    def name(self) -> str:
        return "yfinance"

    def get_bars(
        self,
        symbol: str,
        start: str,
        end: str | None = None,
    ) -> pd.DataFrame:
        df = yf.download(
            symbol,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        return normalize_ohlcv(df, symbol)
