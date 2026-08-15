# -*- coding: utf-8 -*-
"""Optional LightGBM ranker (graceful if package missing)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.alpha_v2.models.linear_rank import FEATURE_COLS_DEFAULT, _cs_zscore


def fit_lgbm_rank(
    train: pd.DataFrame,
    feature_cols: list[str] | None = None,
) -> Any | None:
    try:
        import lightgbm as lgb
    except ImportError:
        return None

    cols = feature_cols or [c for c in FEATURE_COLS_DEFAULT if c in train.columns]
    d = train.dropna(subset=cols + ["label"]).copy()
    d = _cs_zscore(d, cols).dropna(subset=cols + ["label"])
    # group by date for ranking objective
    d = d.sort_values("date")
    group = d.groupby("date").size().tolist()
    model = lgb.LGBMRegressor(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
    )
    # regression on forward return as ranking proxy (simple baseline)
    model.fit(d[cols], d["label"])
    model._feature_cols_ = cols  # type: ignore[attr-defined]
    model._group_hint_ = group  # type: ignore[attr-defined]
    return model


def score_lgbm_rank(panel: pd.DataFrame, model: Any) -> pd.DataFrame:
    cols = list(getattr(model, "_feature_cols_", []))
    d = panel.copy()
    d = _cs_zscore(d, cols)
    out = d[["date", "symbol"]].copy()
    if "label" in d.columns:
        out["label"] = d["label"]
    out["score"] = model.predict(d[cols].fillna(0.0))
    return out
