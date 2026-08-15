# -*- coding: utf-8 -*-
"""Momentum-style cross-sectional features (predictive ranks, not trade rules)."""
from __future__ import annotations

import pandas as pd


def return_nd(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close / close.shift(n) - 1.0


def build_momentum_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    r5 = return_nd(close, 5)
    r20 = return_nd(close, 20)
    r60 = return_nd(close, 60)
    r120 = return_nd(close, 120)
    return {
        "return_5d": r5,
        "return_20d": r20,
        "return_60d": r60,
        "return_120d": r120,
        "skip_mom_20_5": r20 - r5,
    }
