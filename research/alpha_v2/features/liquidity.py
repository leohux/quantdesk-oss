# -*- coding: utf-8 -*-
"""Liquidity features. Volume optional; falls back to abs-return activity proxy."""
from __future__ import annotations

import pandas as pd


def build_liquidity_features(
    close: pd.DataFrame,
    volume: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    ret = close.pct_change().abs()
    if volume is not None:
        avg_vol = volume.rolling(20).mean()
        turnover = (volume / avg_vol.replace(0, pd.NA)).fillna(0)
        vol_chg = volume / volume.shift(20) - 1.0
        return {
            "avg_volume_20d": avg_vol,
            "turnover_20d": turnover,
            "volume_change_20d": vol_chg,
        }
    # proxy when volume panel unavailable
    activity = ret.rolling(20).mean()
    activity_chg = activity / activity.shift(20) - 1.0
    return {
        "avg_volume_20d": activity,  # proxy
        "turnover_20d": activity / activity.rolling(60).mean(),
        "volume_change_20d": activity_chg,
    }
