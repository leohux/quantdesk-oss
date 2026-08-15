# -*- coding: utf-8 -*-
"""Simple linear cross-sectional rank model (no DL)."""
from __future__ import annotations

import numpy as np
import pandas as pd


FEATURE_COLS_DEFAULT = [
    "return_5d",
    "return_20d",
    "return_60d",
    "return_120d",
    "skip_mom_20_5",
    "vol_20d",
    "downside_vol_20d",
    "atr_ratio_20d",
    "max_drawdown_60d",
    "avg_volume_20d",
    "turnover_20d",
    "volume_change_20d",
    "price_vs_ma20",
    "price_vs_ma60",
    "slope_60",
    "r_squared_60",
]


def _cs_zscore(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        mu = out.groupby("date")[c].transform("mean")
        sd = out.groupby("date")[c].transform("std").replace(0, np.nan)
        out[c] = (out[c] - mu) / sd
    return out


def fit_linear_rank(
    train: pd.DataFrame,
    feature_cols: list[str] | None = None,
) -> dict[str, float]:
    """Fit OLS on cross-sectionally z-scored features → label."""
    cols = feature_cols or [c for c in FEATURE_COLS_DEFAULT if c in train.columns]
    d = train.dropna(subset=cols + ["label"]).copy()
    d = _cs_zscore(d, cols)
    d = d.dropna(subset=cols + ["label"])
    X = d[cols].to_numpy(dtype=float)
    y = d["label"].to_numpy(dtype=float)
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    weights = {"intercept": float(beta[0])}
    for i, c in enumerate(cols):
        weights[c] = float(beta[i + 1])
    return weights


def score_linear_rank(
    panel: pd.DataFrame,
    weights: dict[str, float],
    feature_cols: list[str] | None = None,
) -> pd.DataFrame:
    cols = feature_cols or [c for c in weights if c != "intercept" and c in panel.columns]
    d = panel.copy()
    d = _cs_zscore(d, cols)
    score = float(weights.get("intercept", 0.0))
    for c in cols:
        score = score + d[c].fillna(0.0) * float(weights.get(c, 0.0))
    out = d[["date", "symbol", "label"]].copy() if "label" in d.columns else d[["date", "symbol"]].copy()
    out["score"] = score
    return out
