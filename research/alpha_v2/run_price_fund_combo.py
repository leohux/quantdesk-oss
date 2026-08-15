# -*- coding: utf-8 -*-
"""Combo price near-misses + SEC earnings_yield for Gate12-A.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_price_fund_combo
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.features.extended import build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return
from research.alpha_v2.run_fundamental_screen import build_fundamental_features

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_price_fund_combo.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def main() -> None:
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in eligible.columns]
    close = close[cols]
    eligible = eligible[cols].reindex(close.index).fillna(False)

    feats = {}
    feats.update(build_extended_features(close))
    fund = pd.read_parquet(FUND)
    feats.update(build_fundamental_features(close, fund))

    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])
    sm = load_sector_map()

    # train-locked signs from prior research
    members = [
        ("vol_adj_rev_5", 1.0, False),
        ("near_high_252", 1.0, True),
        ("earnings_yield", 1.0, False),
        ("rev_5d", -1.0, False),
    ]
    rank_cols = []
    for f, sign, neut in members:
        raw = panel[f].astype(float)
        if neut:
            tmp = panel[["date", "symbol"]].copy()
            tmp[f] = raw
            raw = industry_neutralize_score(tmp, f, sm)
        col = f"rk_{f}{'_n' if neut else ''}"
        panel[col] = panel.assign(_v=raw * sign).groupby("date")["_v"].rank(pct=True)
        rank_cols.append(col)

    # enumerate useful subsets
    from itertools import combinations

    results = {"combos": []}
    best = None
    for k in range(1, len(rank_cols) + 1):
        for combo in combinations(rank_cols, k):
            d = panel.dropna(subset=list(combo) + ["label"]).copy()
            d["score"] = d[list(combo)].mean(axis=1)
            recent = d[d["date"] >= "2024-01-01"]
            ric = daily_ic(recent, "score", method="spearman")
            m = float(summarize_ic(ric)["mean"])
            roll = float(rolling_positive_share(ric, window=126))
            gate = evaluate_gate12a(m, roll)
            row = {
                "name": "+".join(combo),
                "k": k,
                "rankic": m,
                "roll": roll,
                "gate12a": gate["pass"],
            }
            results["combos"].append(row)
            mark = "PASS" if gate["pass"] else "fail"
            print(f"{mark} {m:+.4f} roll={roll:.3f} | {row['name']}")
            if best is None or m > best["rankic"]:
                best = row

    results["best"] = best
    results["any_pass"] = any(r["gate12a"] for r in results["combos"])
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("BEST", best)
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8"))
    st.setdefault("Track_A", {})["price_fund_combo"] = {
        "status": "GATE12A_PASS" if results["any_pass"] else "GATE12A_FAIL",
        "best": best,
        "artifact": str(OUT),
    }
    if results["any_pass"]:
        st["system"]["alpha"] = "CANDIDATE_COMBO"
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
