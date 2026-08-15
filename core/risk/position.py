"""Position sizing algorithms."""
from __future__ import annotations

import math
from enum import Enum
from typing import Any

import pandas as pd


class SizingMethod(str, Enum):
    FIXED = "fixed"           # Fixed percentage of portfolio
    RISK_PCT = "risk_pct"     # Risk X% per trade
    KELLY = "kelly"           # Kelly Criterion
    ATR = "atr"               # ATR-based sizing


def size_fixed(
    equity: float,
    pct: float = 10.0,
    price: float = 1.0,
    **kwargs: Any,
) -> float:
    """Fixed percentage of equity."""
    allocation = equity * (pct / 100.0)
    return math.floor(allocation / price)


def size_risk_pct(
    equity: float,
    risk_pct: float = 1.0,
    entry_price: float = 1.0,
    stop_price: float = 1.0,
    **kwargs: Any,
) -> float:
    """Risk X% of equity per trade.

    qty = (equity * risk_pct%) / |entry - stop|
    """
    risk_amount = equity * (risk_pct / 100.0)
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return 0
    return math.floor(risk_amount / risk_per_share)


def size_kelly(
    equity: float,
    win_rate: float = 0.5,
    avg_win: float = 1.0,
    avg_loss: float = 1.0,
    price: float = 1.0,
    fraction: float = 0.5,
    **kwargs: Any,
) -> float:
    """Kelly Criterion position sizing.

    Kelly % = W - (1-W)/R where W=win_rate, R=avg_win/avg_loss
    Uses half-Kelly by default for safety.
    """
    if avg_loss == 0 or win_rate <= 0 or win_rate >= 1:
        return 0

    b = avg_win / avg_loss
    kelly_pct = win_rate - (1 - win_rate) / b
    kelly_pct = max(0, min(kelly_pct, 1))  # clamp to [0, 1]
    kelly_pct *= fraction  # half-kelly for safety

    allocation = equity * kelly_pct
    return math.floor(allocation / price)


def size_atr(
    equity: float,
    atr: float,
    risk_pct: float = 1.0,
    atr_multiplier: float = 2.0,
    price: float = 1.0,
    **kwargs: Any,
) -> float:
    """ATR-based position sizing.

    Uses ATR * multiplier as the stop distance.
    qty = (equity * risk_pct%) / (ATR * multiplier)
    """
    if atr <= 0:
        return 0
    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = atr * atr_multiplier
    return math.floor(risk_amount / stop_distance)


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


SIZERS = {
    SizingMethod.FIXED: size_fixed,
    SizingMethod.RISK_PCT: size_risk_pct,
    SizingMethod.KELLY: size_kelly,
    SizingMethod.ATR: size_atr,
}


def calculate_position_size(
    method: SizingMethod,
    **kwargs: Any,
) -> float:
    """Calculate position size using the specified method."""
    fn = SIZERS.get(method)
    if fn is None:
        raise ValueError(f"Unknown sizing method: {method}")
    return fn(**kwargs)
