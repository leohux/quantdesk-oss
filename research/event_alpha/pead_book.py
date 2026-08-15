# -*- coding: utf-8 -*-
"""Shared PEAD event-book construction (reality + attribution).

Same rules as run_pead_reality:
  - train sign lock on T+20 abnormal return vs SPY
  - monthly top-quintile surprise
  - entry T+1, hold HOLD sessions
  - non-overlapping per symbol, max concurrent MAX_NAMES
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

HOLD = 20
MAX_NAMES = 40


def build_event_rows(close: pd.DataFrame, events: pd.DataFrame, hold: int = HOLD) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, e in events.iterrows():
        sym = e["symbol"]
        if sym not in close.columns or pd.isna(e["surprise"]):
            continue
        dt = pd.Timestamp(e["event_date"])
        if dt not in close.index:
            idx = close.index.searchsorted(dt)
            if idx >= len(close.index):
                continue
            dt = close.index[idx]
        pos = close.index.get_loc(dt)
        if not isinstance(pos, (int, np.integer)) or int(pos) + 1 + hold >= len(close.index):
            continue
        rows.append(
            {
                "symbol": sym,
                "event_date": pd.Timestamp(dt),
                "surprise": float(e["surprise"]),
                "entry_i": int(pos) + 1,
                "exit_i": int(pos) + hold,
            }
        )
    return pd.DataFrame(rows)


def select_accepted_events(
    close: pd.DataFrame,
    events: pd.DataFrame,
    hold: int = HOLD,
    max_names: int = MAX_NAMES,
) -> tuple[pd.DataFrame, float, float]:
    """Return (accepted events, sign, train_ic)."""
    ev = build_event_rows(close, events, hold=hold)
    if ev.empty:
        return ev, 1.0, 0.0

    spy_px = close["SPY"] if "SPY" in close.columns else close.mean(axis=1)
    train = ev[(ev.event_date >= "2021-07-01") & (ev.event_date <= "2022-12-31")].copy()

    def abn20(row: pd.Series) -> float:
        sym, i0, i1 = row["symbol"], int(row["entry_i"]), int(row["exit_i"])
        r_s = float(close[sym].iloc[i1] / close[sym].iloc[i0] - 1.0)
        r_m = float(spy_px.iloc[i1] / spy_px.iloc[i0] - 1.0)
        return r_s - r_m

    train["fwd"] = train.apply(abn20, axis=1)
    d = train.dropna(subset=["surprise", "fwd"])
    tr_ic = float(d["surprise"].rank().corr(d["fwd"].rank())) if len(d) > 30 else 0.0
    sign = 1.0 if (tr_ic != tr_ic or tr_ic >= 0) else -1.0
    ev["score"] = ev["surprise"] * sign

    ev["ym"] = ev["event_date"].dt.to_period("M")
    picks_parts = []
    for _, g in ev.groupby("ym"):
        thr = g["score"].quantile(0.8)
        picks_parts.append(g[g["score"] >= thr])
    picks = pd.concat(picks_parts).sort_values("entry_i") if picks_parts else pd.DataFrame()

    active_until: dict[str, int] = {}
    accepted: list[pd.Series] = []
    for _, row in picks.iterrows():
        sym = row["symbol"]
        ei, xi = int(row["entry_i"]), int(row["exit_i"])
        if sym in active_until and ei <= active_until[sym]:
            continue
        concurrent = sum(1 for _, u in active_until.items() if u >= ei)
        if concurrent >= max_names:
            continue
        active_until[sym] = xi
        accepted.append(row)

    acc = pd.DataFrame(accepted) if accepted else pd.DataFrame()
    return acc, sign, tr_ic
