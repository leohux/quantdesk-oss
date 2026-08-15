"""Strategy base class and signal types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class SignalResult:
    """Result from strategy signal generation."""
    entries: pd.Series   # Boolean Series - True on entry bars
    exits: pd.Series     # Boolean Series - True on exit bars
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.entries = self.entries.fillna(False).astype(bool)
        self.exits = self.exits.fillna(False).astype(bool)

    @property
    def latest_signal(self) -> Signal:
        """Get the most recent signal."""
        if self.entries.iloc[-1]:
            return Signal.BUY
        if self.exits.iloc[-1]:
            return Signal.SELL
        return Signal.HOLD

    @property
    def total_entries(self) -> int:
        return int(self.entries.sum())

    @property
    def total_exits(self) -> int:
        return int(self.exits.sum())


class Strategy(ABC):
    """Abstract base strategy.

    Subclass this for custom strategies, or use the code-based
    strategy engine for user-defined strategies.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate_signals(
        self,
        close: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> SignalResult:
        """Generate entry/exit signals from close prices."""
        ...

    def validate_params(self, params: dict[str, Any]) -> None:
        """Override to add custom param validation."""
        pass


class CodeStrategy(Strategy):
    """Strategy defined by user Python code (compiled from string)."""

    def __init__(self, code: str, strategy_name: str = "custom") -> None:
        self._code = code
        self._name = strategy_name
        self._fn = None

    @property
    def name(self) -> str:
        return self._name

    def generate_signals(
        self,
        close: pd.Series,
        params: dict[str, Any] | None = None,
    ) -> SignalResult:
        from core.strategy.engine import compile_strategy

        if self._fn is None:
            self._fn = compile_strategy(self._code)

        entries, exits = self._fn(close.astype(float), params or {})
        return SignalResult(
            entries=pd.Series(entries, index=close.index),
            exits=pd.Series(exits, index=close.index),
        )
