"""Data provider manager with auto-fallback."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from core.data.base import DataProvider

logger = logging.getLogger(__name__)


class DataManager:
    """Manages multiple data providers with fallback logic."""

    def __init__(self) -> None:
        self._providers: list[DataProvider] = []
        self._default: DataProvider | None = None

    def register(self, provider: DataProvider, default: bool = False) -> None:
        self._providers.append(provider)
        if default or self._default is None:
            self._default = provider

    def set_default(self, name: str) -> None:
        for p in self._providers:
            if p.name == name:
                self._default = p
                return
        raise ValueError(f"Provider '{name}' not registered")

    @property
    def default_provider(self) -> DataProvider | None:
        return self._default

    def get_bars(
        self,
        symbol: str,
        start: str = "2018-01-01",
        end: str | None = None,
        provider: str | None = None,
    ) -> pd.DataFrame:
        """Fetch bars using specified or default provider, with fallback."""
        if provider:
            # Use specific provider
            for p in self._providers:
                if p.name == provider:
                    return p.get_bars(symbol, start, end)
            raise ValueError(f"Provider '{provider}' not found")

        # Try default first, then fallback to others
        tried = set()
        candidates = [self._default] + [
            p for p in self._providers if p != self._default
        ]

        for p in candidates:
            if p is None or p.name in tried or not p.is_available():
                continue
            tried.add(p.name)
            try:
                return p.get_bars(symbol, start, end)
            except Exception:
                logger.warning("Provider %s failed for %s, trying next", p.name, symbol)
                continue

        raise RuntimeError(f"All data providers failed for {symbol}")

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {"name": p.name, "available": p.is_available(), "default": p == self._default}
            for p in self._providers
        ]
