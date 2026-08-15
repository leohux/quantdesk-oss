# -*- coding: utf-8 -*-
"""Deepen Nasdaq PEAD: T+20 primary, winsorized surprise, long-only top surprise.

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_nasdaq_t20
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
OUT = ROOT / "data" / "research" / "event_alpha_pead_nasdaq_t20.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def abn_forward(px: pd.Series, mkt: pd.Series, pos: int, horizon: int) -> float:
    entry, exit_ = pos + 1, pos + horizon
    if exit_ >= len(px) or entry >= len(px):
        return np.nan
    r_s = float(px.iloc[exit_] / px.iloc[entry] - 1.0)
    m = mkt.reindex(px.index).ffill()
    r_m = float(m.iloc[exit_] / m.iloc[entry] - 1.0)
    return r_s - r_m


def rank_ic(score: pd.Series, y: pd.Series) -> float:
    d = pd.DataFrame({"s": score, "y": y}).dropna()
    if len(d) < 30:
        return float("nan")
    return float(d["s"].rank().corr(d["y"].rank()))


def winsorize(s: pd.Series, p: float = 0.01) -> pd.Series:
    lo, hi = s.quantile(p), s.quantile(1 - p)
    return s.clip(lo, hi)


def main() -> None:
    close = pd.read_parquet(CACHE)
    events = pd.read_parquet(EVENTS)
    events["event_date"] = pd.to_datetime(events["event_date"])
    mkt = close["SPY"] if "SPY" in close.columns else close.pct_change().mean(axis=1).fillna(0).add(1).cumprod()

    rows = []
    for _, e in events.iterrows():
        sym = e["symbol"]
        if sym not in close.columns:
            continue
        px = close[sym].dropna()
        dt = pd.Timestamp(e["event_date"])
        if dt not in px.index:
            idx = px.index.searchsorted(dt)
            if idx >= len(px.index):
                continue
            dt = px.index[idx]
        pos = px.index.get_loc(dt)
        if not isinstance(pos, (int, np.integer)) or int(pos) < 1:
            continue
        surprise = e["surprise"]
        if pd.isna(surprise) and pd.notna(e.get("eps")) and pd.notna(e.get("eps_forecast")):
            den = abs(float(e["eps_forecast"])) or np.nan
            surprise = (float(e["eps"]) - float(e["eps_forecast"])) / den * 100.0
        rows.append(
            {
                "symbol": sym,
                "event_date": pd.Timestamp(dt),
                "surprise": surprise,
                "abn_t5": abn_forward(px, mkt, int(pos), 5),
                "abn_t20": abn_forward(px, mkt, int(pos), 20),
            }
        )
    df = pd.DataFrame(rows).dropna(subset=["surprise"])
    df["surprise_w"] = winsorize(df["surprise"])

    train = df[(df.event_date >= "2021-07-01") & (df.event_date <= "2022-12-31")]
    recent = df[df.event_date >= "2024-01-01"]
    oos = df[(df.event_date >= "2024-01-01") & (df.event_date <= "2025-12-31")]

    results = {"signals": {}}
    for sig in ("surprise", "surprise_w"):
        tr = rank_ic(train[sig], train["abn_t20"])
        sign = 1.0 if (tr == tr and tr >= 0) else -1.0
        block = {"train_ic_t20_raw": tr, "sign": sign, "splits": {}}
        for name, part in [
            ("train", train),
            ("valid", df[(df.event_date >= "2023-01-01") & (df.event_date <= "2023-12-31")]),
            ("oos", oos),
            ("holdout", df[df.event_date >= "2026-01-01"]),
            ("recent_2024plus", recent),
        ]:
            score = part[sig] * sign
            # long-short: top vs bottom quintile mean abn
            q = part.dropna(subset=[sig, "abn_t20"]).copy()
            q["score"] = q[sig] * sign
            if len(q) >= 50:
                top = q[q["score"] >= q["score"].quantile(0.8)]["abn_t20"].mean()
                bot = q[q["score"] <= q["score"].quantile(0.2)]["abn_t20"].mean()
                spread = float(top - bot)
            else:
                top = bot = spread = float("nan")
            block["splits"][name] = {
                "n": int(len(q)),
                "rankic_t20": rank_ic(score, part["abn_t20"]),
                "rankic_t5": rank_ic(score, part["abn_t5"]),
                "top_quintile_abn_t20": float(top) if top == top else None,
                "bot_quintile_abn_t20": float(bot) if bot == bot else None,
                "spread_t20": spread if spread == spread else None,
            }
            print(
                f"{sig}/{name}: RankIC20={block['splits'][name]['rankic_t20']:+.4f} "
                f"spread20={spread:+.4f}" if spread == spread else f"{sig}/{name}: n={len(q)}"
            )
        ric = block["splits"]["recent_2024plus"]["rankic_t20"]
        block["gate_hint_t20"] = {
            "pass_rankic_ge_0p03": bool(ric == ric and ric >= 0.03),
            "recent_rankic_t20": ric,
        }
        results["signals"][sig] = block

    best = max(
        results["signals"].items(),
        key=lambda kv: (kv[1]["splits"]["recent_2024plus"]["rankic_t20"] or -9),
    )
    results["best"] = best[0]
    results["best_recent_rankic_t20"] = best[1]["splits"]["recent_2024plus"]["rankic_t20"]
    results["recommendation"] = (
        "pead_t20_candidate - surprise predicts 20d abnormal; NOT Gate12-A daily CS yet; "
        "promote to event portfolio backtest next"
        if best[1]["gate_hint_t20"]["pass_rankic_ge_0p03"]
        else "pead_t20_weak"
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_B", {})["PEAD_nasdaq_t20"] = {
        "status": "CANDIDATE_EVENT" if best[1]["gate_hint_t20"]["pass_rankic_ge_0p03"] else "WEAK",
        "best_signal": results["best"],
        "recent_rankic_t20": results["best_recent_rankic_t20"],
        "oos_rankic_t20": best[1]["splits"]["oos"]["rankic_t20"],
        "oos_spread_t20": best[1]["splits"]["oos"]["spread_t20"],
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
