# -*- coding: utf-8 -*-
"""Simple PEAD event portfolio backtest (long top-surprise names for 20d).

Not Gate12-A. Reality-light: equal weight, entry T+1, hold 20 sessions,
cost 10bps round-trip, no leverage. Benchmark: SPY buy&hold over same dates.

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_event_bt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
EVENTS = ROOT / "data" / "cache" / "nasdaq_earnings_events.parquet"
OUT = ROOT / "data" / "research" / "event_alpha_pead_event_bt.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

COST = 0.001  # 10bps round trip


def abn_path(px: pd.Series, mkt: pd.Series, pos: int, horizon: int = 20) -> float:
    entry, exit_ = pos + 1, pos + horizon
    if exit_ >= len(px) or entry >= len(px):
        return np.nan
    r_s = float(px.iloc[exit_] / px.iloc[entry] - 1.0)
    m = mkt.reindex(px.index).ffill()
    r_m = float(m.iloc[exit_] / m.iloc[entry] - 1.0)
    return r_s - r_m - COST


def stats(rets: pd.Series) -> dict:
    r = rets.dropna()
    if len(r) < 20:
        return {"n": int(len(r))}
    # event returns are overlapping if many events; treat as independent samples for IC-style
    # For portfolio we build daily overlay below
    return {
        "n": int(len(r)),
        "mean": float(r.mean()),
        "std": float(r.std()),
        "hit": float((r > 0).mean()),
        "sharpe_like": float(r.mean() / r.std() * np.sqrt(252 / 20)) if r.std() > 0 else None,
    }


def main() -> None:
    close = pd.read_parquet(CACHE)
    events = pd.read_parquet(EVENTS)
    events["event_date"] = pd.to_datetime(events["event_date"])
    mkt = close["SPY"] if "SPY" in close.columns else close.mean(axis=1)

    rows = []
    for _, e in events.iterrows():
        sym = e["symbol"]
        if sym not in close.columns or pd.isna(e["surprise"]):
            continue
        px = close[sym].dropna()
        dt = pd.Timestamp(e["event_date"])
        if dt not in px.index:
            idx = px.index.searchsorted(dt)
            if idx >= len(px):
                continue
            dt = px.index[idx]
        pos = px.index.get_loc(dt)
        if not isinstance(pos, (int, np.integer)) or int(pos) < 1:
            continue
        rows.append(
            {
                "symbol": sym,
                "event_date": pd.Timestamp(dt),
                "surprise": float(e["surprise"]),
                "net_abn_t20": abn_path(px, mkt, int(pos), 20),
            }
        )
    df = pd.DataFrame(rows).dropna()

    # Within each month, long top 30% surprise events (cross-sectional among events)
    df["ym"] = df["event_date"].dt.to_period("M")
    picked = []
    for _, g in df.groupby("ym"):
        thr = g["surprise"].quantile(0.7)
        picked.append(g[g["surprise"] >= thr])
    long = pd.concat(picked) if picked else df.iloc[0:0]

    # Daily overlapping portfolio: equal-weight active names in holding window
    hold = 20
    daily = pd.Series(0.0, index=close.index, dtype=float)
    counts = pd.Series(0.0, index=close.index, dtype=float)
    mkt_ret = mkt.pct_change()
    for _, e in long.iterrows():
        sym = e["symbol"]
        dt = e["event_date"]
        if dt not in close.index or sym not in close.columns:
            continue
        pos = close.index.get_loc(dt)
        if not isinstance(pos, (int, np.integer)):
            continue
        entry = int(pos) + 1
        exit_ = min(int(pos) + hold, len(close.index) - 1)
        if entry >= len(close.index):
            continue
        px = close[sym]
        rets = px.pct_change().iloc[entry : exit_ + 1]
        # excess vs market
        ex = rets - mkt_ret.reindex(rets.index)
        daily.loc[ex.index] = daily.loc[ex.index].add(ex.fillna(0.0), fill_value=0.0)
        counts.loc[ex.index] = counts.loc[ex.index].add(1.0)
        # cost on entry day
        if entry < len(close.index):
            daily.iloc[entry] -= COST / 2
        if exit_ < len(close.index):
            daily.iloc[exit_] -= COST / 2

    port = daily / counts.replace(0, np.nan)
    port = port.fillna(0.0)

    def period_stats(s: pd.Series, start: str, end: str | None = None) -> dict:
        p = s[s.index >= start]
        if end:
            p = p[p.index <= end]
        # only days with positions
        active = p[counts.reindex(p.index).fillna(0) > 0]
        if len(active) < 30:
            return {"n_active_days": int(len(active))}
        ann = float(active.mean() * 252)
        vol = float(active.std() * np.sqrt(252))
        sharpe = ann / vol if vol > 0 else None
        eq = (1 + active).cumprod()
        dd = float((eq / eq.cummax() - 1).min())
        return {
            "n_active_days": int(len(active)),
            "ann_excess": ann,
            "vol": vol,
            "sharpe": sharpe,
            "maxdd": dd,
            "hit_daily": float((active > 0).mean()),
            "avg_names": float(counts.reindex(active.index).mean()),
        }

    results = {
        "event_stats_all_long_top30pct": stats(long["net_abn_t20"]),
        "portfolio": {
            "train": period_stats(port, "2021-07-01", "2022-12-31"),
            "valid": period_stats(port, "2023-01-01", "2023-12-31"),
            "oos": period_stats(port, "2024-01-01", "2025-12-31"),
            "holdout": period_stats(port, "2026-01-01", None),
            "recent_2024plus": period_stats(port, "2024-01-01", None),
        },
        "notes": "Long top-30% surprise within month; hold 20d excess vs SPY; 10bps RT cost.",
    }
    for k, v in results["portfolio"].items():
        print(f"{k}: {v}")

    oos = results["portfolio"]["oos"]
    results["verdict"] = (
        "event_alpha_promising"
        if (oos.get("sharpe") or 0) > 0.5 and (oos.get("ann_excess") or 0) > 0
        else "event_alpha_weak_after_costs"
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["verdict"], f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8"))
    st.setdefault("Track_B", {})["PEAD_event_bt"] = {
        "status": results["verdict"],
        "oos": oos,
        "recent": results["portfolio"]["recent_2024plus"],
        "artifact": str(OUT),
    }
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
