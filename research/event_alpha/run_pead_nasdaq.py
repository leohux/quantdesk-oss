# -*- coding: utf-8 -*-
"""PEAD from Nasdaq earnings surprise calendar.

Signal: standardized surprise (and/or surprise * sign of reaction).
Label: T+5 abnormal return after event (entry lag 1).

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_nasdaq
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
EVENTS = ROOT / "data" / "cache" / "nasdaq_earnings_events.parquet"
OUT = ROOT / "data" / "research" / "event_alpha_pead_nasdaq.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def abn_forward(px: pd.Series, mkt: pd.Series, pos: int, horizon: int = 5) -> float:
    entry = pos + 1
    exit_ = pos + horizon
    if exit_ >= len(px) or entry >= len(px):
        return np.nan
    r_s = float(px.iloc[exit_] / px.iloc[entry] - 1.0)
    m = mkt.reindex(px.index).ffill()
    r_m = float(m.iloc[exit_] / m.iloc[entry] - 1.0)
    return r_s - r_m


def spearman_ic(score: pd.Series, y: pd.Series) -> float:
    d = pd.DataFrame({"s": score, "y": y}).dropna()
    if len(d) < 30:
        return float("nan")
    # avoid scipy dependency
    return float(d["s"].rank().corr(d["y"].rank()))


def main() -> None:
    if not EVENTS.exists():
        raise SystemExit(f"Missing {EVENTS}; run fetch_nasdaq_earnings first")

    close = pd.read_parquet(CACHE)
    events = pd.read_parquet(EVENTS)
    events["event_date"] = pd.to_datetime(events["event_date"])
    if "SPY" in close.columns:
        mkt = close["SPY"].astype(float)
    else:
        mkt = close.pct_change().mean(axis=1).fillna(0).add(1).cumprod()

    rows = []
    for _, e in events.iterrows():
        sym = e["symbol"]
        if sym not in close.columns:
            continue
        px = close[sym].dropna()
        dt = pd.Timestamp(e["event_date"])
        # map to nearest trading day on/after event
        if dt not in px.index:
            idx = px.index.searchsorted(dt)
            if idx >= len(px.index):
                continue
            dt = px.index[idx]
        pos = px.index.get_loc(dt)
        if not isinstance(pos, (int, np.integer)) or pos < 1:
            continue
        reaction = float(px.iloc[pos] / px.iloc[pos - 1] - 1.0)
        surprise = e["surprise"]
        if pd.isna(surprise) and pd.notna(e.get("eps")) and pd.notna(e.get("eps_forecast")):
            # percentage surprise proxy
            den = abs(float(e["eps_forecast"])) if float(e["eps_forecast"]) != 0 else np.nan
            surprise = (float(e["eps"]) - float(e["eps_forecast"])) / den * 100.0 if den == den else np.nan
        rows.append(
            {
                "symbol": sym,
                "event_date": pd.Timestamp(dt),
                "surprise": surprise,
                "reaction": reaction,
                "abn_t5": abn_forward(px, mkt, int(pos), 5),
                "abn_t20": abn_forward(px, mkt, int(pos), 20),
            }
        )
    df = pd.DataFrame(rows)
    print(f"events_usable={len(df)} with_surprise={df['surprise'].notna().sum()}")

    # Train-only sign for surprise → abn_t5
    train = df[(df.event_date >= "2021-07-01") & (df.event_date <= "2022-12-31")]
    valid = df[(df.event_date >= "2023-01-01") & (df.event_date <= "2023-12-31")]
    oos = df[(df.event_date >= "2024-01-01") & (df.event_date <= "2025-12-31")]
    hold = df[df.event_date >= "2026-01-01"]
    recent = df[df.event_date >= "2024-01-01"]

    tr_ic = spearman_ic(train["surprise"], train["abn_t5"])
    sign = 1.0 if (tr_ic == tr_ic and tr_ic >= 0) else -1.0

    results = {
        "n_events": int(len(df)),
        "train_surprise_ic_raw": tr_ic,
        "sign": sign,
        "splits": {},
        "alt_signals": {},
    }
    for name, part in [
        ("train", train),
        ("valid", valid),
        ("oos", oos),
        ("holdout", hold),
        ("recent_2024plus", recent),
    ]:
        score = part["surprise"] * sign
        results["splits"][name] = {
            "n": int(len(part.dropna(subset=["surprise", "abn_t5"]))),
            "rankic_t5": spearman_ic(score, part["abn_t5"]),
            "rankic_t20": spearman_ic(score, part["abn_t20"]),
            "mean_abn_t5_top": float(
                part.dropna(subset=["surprise", "abn_t5"])
                .assign(s=score)
                .sort_values("s", ascending=False)
                .head(max(20, len(part) // 10))["abn_t5"]
                .mean()
            )
            if len(part.dropna(subset=["surprise", "abn_t5"])) >= 30
            else None,
        }
        print(
            f"{name}: n={results['splits'][name]['n']} "
            f"RankIC_t5={results['splits'][name]['rankic_t5']:+.4f} "
            f"t20={results['splits'][name]['rankic_t20']:+.4f}"
        )

    # alt: reaction continuation vs reversal
    for sig_name, col in [("reaction", "reaction"), ("neg_reaction", "reaction")]:
        s_sign = -1.0 if sig_name.startswith("neg_") else 1.0
        # lock on train
        tr = spearman_ic(train[col] * s_sign, train["abn_t5"])
        # if we forced neg_, already negative reaction
        results["alt_signals"][sig_name] = {
            "forced_sign": s_sign,
            "train_ic": tr,
            "recent_rankic_t5": spearman_ic(recent[col] * s_sign, recent["abn_t5"]),
            "recent_rankic_t20": spearman_ic(recent[col] * s_sign, recent["abn_t20"]),
        }

    ric = results["splits"]["recent_2024plus"]["rankic_t5"]
    results["gate_hint"] = {
        "pass_rankic_ge_0p03": bool(ric == ric and ric >= 0.03),
        "recent_rankic_t5": ric,
    }
    results["recommendation"] = (
        "pead_nasdaq_continue"
        if results["gate_hint"]["pass_rankic_ge_0p03"]
        else "pead_nasdaq_weak_or_reversal - inspect alt reaction signals"
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_B", {})["PEAD_nasdaq"] = {
        "status": "SIGNAL_HINT" if results["gate_hint"]["pass_rankic_ge_0p03"] else "WEAK",
        "n_events": results["n_events"],
        "recent_rankic_t5": ric,
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
