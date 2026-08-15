# -*- coding: utf-8 -*-
"""Forward return labels with anti-leakage lag."""
from __future__ import annotations

import pandas as pd


def forward_return(
    close: pd.DataFrame,
    *,
    horizon: int = 5,
    entry_lag: int = 1,
) -> pd.DataFrame:
    """Label at date t = close[t+entry_lag+horizon-1?]/close[t+entry_lag]-1.

    Default: entry at t+1 close, exit at t+5 close relative to t feature date:
        label_t = close[t+5] / close[t+1] - 1

    This forbids trading on the same close used to compute features.
    """
    if entry_lag < 1:
        raise ValueError("entry_lag must be >= 1 to avoid same-bar leakage")
    entry = close.shift(-entry_lag)
    exit_ = close.shift(-(entry_lag + horizon - 1)) if horizon >= 1 else entry
    # For horizon=5, entry_lag=1: exit at t+5 => shift(-(1+5-1)) = shift(-5). Good.
    return exit_ / entry - 1.0


def align_xy(
    features: dict[str, pd.DataFrame],
    label: pd.DataFrame,
    eligible: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Stack panel to long format: date, symbol, features..., label."""
    # common index/columns
    idx = label.index
    cols = label.columns
    for fr in features.values():
        idx = idx.intersection(fr.index)
        cols = cols.intersection(fr.columns)
    if eligible is not None:
        idx = idx.intersection(eligible.index)
        cols = cols.intersection(eligible.columns)

    frames = []
    for name, fr in features.items():
        s = fr.loc[idx, cols].stack(future_stack=True)
        s.name = name
        frames.append(s)
    y = label.loc[idx, cols].stack(future_stack=True)
    y.name = "label"
    frames.append(y)
    if eligible is not None:
        e = eligible.loc[idx, cols].stack(future_stack=True).astype(bool)
        e.name = "eligible"
        frames.append(e)

    df = pd.concat(frames, axis=1)
    df = df.reset_index()
    df.columns = ["date", "symbol", *df.columns[2:]]
    if "eligible" in df.columns:
        df = df[df["eligible"]].drop(columns=["eligible"])
    df = df.dropna(subset=["label"])
    return df
