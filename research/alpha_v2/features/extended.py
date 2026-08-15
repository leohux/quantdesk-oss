# -*- coding: utf-8 -*-
"""Extended price-only CS features for Phase12.1 feature research."""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_extended_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    ret = close.pct_change()
    r1 = ret
    r5 = close / close.shift(5) - 1.0
    r20 = close / close.shift(20) - 1.0
    r60 = close / close.shift(60) - 1.0
    r120 = close / close.shift(120) - 1.0

    # Short-term reversal candidates (raw; sign fixed later on train)
    rev_1d = r1
    rev_5d = r5
    rev_21d = close / close.shift(21) - 1.0

    # Residual momentum: stock return minus cross-sectional mean (market)
    mkt = ret.mean(axis=1)
    resid = ret.sub(mkt, axis=0)
    resid_mom_20 = resid.rolling(20).sum()
    resid_mom_60 = resid.rolling(60).sum()

    # Acceleration
    accel_20_60 = r20 - r60
    accel_5_20 = r5 - r20

    # Amihud-like illiquidity proxy (no volume): |ret| / (|ret| rolling mean) spike
    amihud_proxy = ret.abs() / ret.abs().rolling(20).mean().replace(0, np.nan)

    # Lottery: max daily return over 20d
    lottery_20 = ret.rolling(20).max()

    # Idiosyncratic vol proxy: residual std
    idiovol_20 = resid.rolling(20).std()

    # 52w proximity
    high_252 = close.rolling(252, min_periods=120).max()
    low_252 = close.rolling(252, min_periods=120).min()
    near_high = close / high_252 - 1.0
    near_low = close / low_252 - 1.0

    # Shock score: today's |z(ret)| 
    vol20 = ret.rolling(20).std()
    shock_z = ret / vol20.replace(0, np.nan)

    # Reversal-after-shock: -sign(shock) * |shock| on large moves only encoded as continuous
    # Use negative of recent 5d return scaled by vol (vol-adj reversal)
    vol_adj_rev_5 = -r5 / vol20.replace(0, np.nan)

    # Trend broken: price below ma60 while 20d mom still positive (conflict)
    ma60 = close.rolling(60).mean()
    conflict = ((close < ma60) & (r20 > 0)).astype(float) - ((close > ma60) & (r20 < 0)).astype(float)

    return {
        "rev_1d": rev_1d,
        "rev_5d": rev_5d,
        "rev_21d": rev_21d,
        "resid_mom_20": resid_mom_20,
        "resid_mom_60": resid_mom_60,
        "accel_20_60": accel_20_60,
        "accel_5_20": accel_5_20,
        "amihud_proxy": amihud_proxy,
        "lottery_20": lottery_20,
        "idiovol_20": idiovol_20,
        "near_high_252": near_high,
        "near_low_252": near_low,
        "shock_z": shock_z,
        "vol_adj_rev_5": vol_adj_rev_5,
        "trend_conflict": conflict,
        # keep a few originals for comparison
        "return_120d": r120,
        "downside_vol_20d": ret.clip(upper=0).pow(2).rolling(20).mean().pow(0.5),
        "r_squared_proxy": 1.0
        - (
            (np.log(close) - np.log(ma60.replace(0, np.nan))).rolling(60).std()
            / (((np.log(close) - np.log(close.shift(60))) / 60.0).abs() * 60.0 + 1e-8)
        ).clip(0, 1),
    }


BUCKETS_EXT = {
    "reversal": ["rev_1d", "rev_5d", "rev_21d", "vol_adj_rev_5", "shock_z"],
    "resid_momentum": ["resid_mom_20", "resid_mom_60", "accel_20_60", "accel_5_20", "return_120d"],
    "risk_liquidity": ["amihud_proxy", "lottery_20", "idiovol_20", "downside_vol_20d"],
    "range_trend": ["near_high_252", "near_low_252", "trend_conflict", "r_squared_proxy"],
}
