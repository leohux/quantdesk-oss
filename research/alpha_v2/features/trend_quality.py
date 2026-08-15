# -*- coding: utf-8 -*-
"""Trend-quality features (as predictors, not MA cross trade rules)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_trend_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    logp = np.log(close.astype(float))
    # Fast slope proxy: average daily log-return over 60d (not full OLS).
    slope60 = (logp - logp.shift(60)) / 60.0
    # Trend quality proxy: |corr|-like smoothness = 1 - noise/signal
    # noise = residual vol around MA60 path; signal = |slope|
    resid = logp - np.log(ma60.replace(0, np.nan))
    noise = resid.rolling(60).std()
    r2_proxy = 1.0 - (noise / (slope60.abs() * 60.0 + 1e-8)).clip(0, 1)
    return {
        "price_vs_ma20": close / ma20 - 1.0,
        "price_vs_ma60": close / ma60 - 1.0,
        "slope_60": slope60,
        "r_squared_60": r2_proxy,
    }
