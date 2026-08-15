# -*- coding: utf-8 -*-
"""IC / RankIC evaluation engine."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _spearman(a: pd.Series, b: pd.Series) -> float:
    mask = a.notna() & b.notna()
    if int(mask.sum()) < 5:
        return np.nan
    x = a[mask].rank().to_numpy(dtype=float)
    y = b[mask].rank().to_numpy(dtype=float)
    x = x - x.mean()
    y = y - y.mean()
    denom = np.sqrt((x * x).sum() * (y * y).sum())
    if denom <= 1e-18:
        return np.nan
    return float((x * y).sum() / denom)


def _pearson(a: pd.Series, b: pd.Series) -> float:
    if a.notna().sum() < 5 or b.notna().sum() < 5:
        return np.nan
    return float(a.corr(b, method="pearson"))


def daily_ic(
    panel: pd.DataFrame,
    score_col: str,
    label_col: str = "label",
    method: str = "spearman",
) -> pd.Series:
    """Cross-sectional IC by date."""
    corr_fn = _spearman if method == "spearman" else _pearson
    out = {}
    for dt, g in panel.groupby("date"):
        out[dt] = corr_fn(g[score_col], g[label_col])
    s = pd.Series(out).sort_index()
    s.name = f"ic_{method}"
    return s


def summarize_ic(ic: pd.Series) -> dict:
    ic = ic.dropna()
    if ic.empty:
        return {"n": 0, "mean": np.nan, "std": np.nan, "ir": np.nan, "hit": np.nan}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if len(ic) > 1 else np.nan
    ir = mean / std if std and std > 1e-12 else np.nan
    return {
        "n": int(len(ic)),
        "mean": mean,
        "std": std,
        "ir": float(ir) if ir == ir else np.nan,
        "hit": float((ic > 0).mean()),
    }


def rolling_positive_share(ic: pd.Series, window: int = 252) -> float:
    """Share of rolling windows whose mean IC > 0 (approx 12m if window=252)."""
    ic = ic.dropna()
    if len(ic) < window:
        return float((ic.mean() > 0)) if len(ic) else np.nan
    roll = ic.rolling(window).mean().dropna()
    return float((roll > 0).mean())
