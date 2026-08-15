"""Stop loss strategies."""
from __future__ import annotations

import math
from enum import Enum
from typing import Any

import pandas as pd


class StopLossType(str, Enum):
    NONE = "none"
    FIXED_PCT = "fixed_pct"
    TRAILING_PCT = "trailing_pct"
    ATR = "atr"


def stop_fixed_pct(entry_price: float, pct: float = 5.0, **kwargs: Any) -> float:
    """Fixed percentage stop loss below entry."""
    return entry_price * (1 - pct / 100.0)


def stop_trailing_pct(
    current_price: float,
    highest_since_entry: float,
    pct: float = 5.0,
    **kwargs: Any,
) -> float:
    """Trailing stop loss: X% below highest price since entry."""
    return highest_since_entry * (1 - pct / 100.0)


def stop_atr(
    entry_price: float,
    atr: float,
    multiplier: float = 2.0,
    **kwargs: Any,
) -> float:
    """ATR-based stop loss."""
    return entry_price - (atr * multiplier)


def should_stop_out(
    current_price: float,
    stop_price: float,
) -> bool:
    """Check if current price has hit the stop loss."""
    return current_price <= stop_price


class StopLossManager:
    """Manages stop loss for a position."""

    def __init__(
        self,
        stop_type: StopLossType = StopLossType.FIXED_PCT,
        pct: float = 5.0,
        atr_multiplier: float = 2.0,
    ) -> None:
        self.stop_type = stop_type
        self.pct = pct
        self.atr_multiplier = atr_multiplier
        self._stop_price: float = 0.0
        self._highest: float = 0.0

    def init_stop(self, entry_price: float, atr: float = 0.0) -> float:
        """Initialize stop loss after entry."""
        if self.stop_type == StopLossType.FIXED_PCT:
            self._stop_price = stop_fixed_pct(entry_price, self.pct)
        elif self.stop_type == StopLossType.ATR:
            self._stop_price = stop_atr(entry_price, atr, self.atr_multiplier)
        elif self.stop_type == StopLossType.TRAILING_PCT:
            self._highest = entry_price
            self._stop_price = stop_trailing_pct(entry_price, entry_price, self.pct)
        else:
            self._stop_price = 0.0

        self._highest = entry_price
        return self._stop_price

    def update(self, current_price: float) -> float:
        """Update stop price (for trailing stops). Returns current stop."""
        if current_price > self._highest:
            self._highest = current_price
            if self.stop_type == StopLossType.TRAILING_PCT:
                self._stop_price = stop_trailing_pct(
                    current_price, self._highest, self.pct
                )
        return self._stop_price

    def is_stopped(self, current_price: float) -> bool:
        """Check if stop loss is triggered."""
        return should_stop_out(current_price, self._stop_price)

    @property
    def stop_price(self) -> float:
        return self._stop_price
