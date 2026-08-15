# -*- coding: utf-8 -*-
"""Volatility / path-risk features."""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_volatility_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ret = close.pct_change()
    vol20 = ret.rolling(20).std()
    down20 = ret.clip(upper=0).pow(2).rolling(20).mean().pow(0.5)
    # ATR proxy from close-to-close range
    tr = close.pct_change().abs()
    atr20 = tr.rolling(20).mean()
    atr_ratio = atr20 / vol20.replace(0, np.nan)
    # rolling max drawdown over 60d (negative)
    roll_max = close.rolling(60).max()
    dd60 = close / roll_max - 1.0
    return {
        "vol_20d": vol20,
        "downside_vol_20d": down20,
        "atr_ratio_20d": atr_ratio,
        "max_drawdown_60d": dd60,
    }
