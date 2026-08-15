# -*- coding: utf-8 -*-
"""Enumerate near-miss subset combos for Gate12-A."""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.features.extended import build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

OUT = ROOT / "data" / "research" / "alpha_v2_combo_enumerate.json"


def main() -> None:
    close = pd.read_parquet(ROOT / "data/cache/sp500_pit_close.parquet")
    eligible = pd.read_parquet(ROOT / "data/cache/sp500_pit_eligible.parquet").astype(bool)
    cols = [c for c in close.columns if c in eligible.columns]
    close = close[cols]
    eligible = eligible[cols].reindex(close.index).fillna(False)

    feats = build_extended_features(close)
    sm = load_sector_map()
    lab = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, lab, eligible)
    panel["date"] = pd.to_datetime(panel["date"])

    specs = [
        ("vol_adj_rev_5", 1.0, False),
        ("rev_5d", -1.0, False),
        ("near_high_252", 1.0, True),
        ("r_squared_proxy", 1.0, False),
    ]
    items: list[str] = []
    for f, s, n in specs:
        raw = panel[f].astype(float)
        if n:
            tmp = panel[["date", "symbol"]].copy()
            tmp[f] = raw
            raw = industry_neutralize_score(tmp, f, sm)
        col = f"{f}_n" if n else f
        panel[col] = panel.assign(_v=raw * s).groupby("date")["_v"].rank(pct=True)
        items.append(col)

    rows = []
    best = None
    for k in range(1, len(items) + 1):
        for combo in combinations(items, k):
            d = panel.dropna(subset=list(combo) + ["label"]).copy()
            d["score"] = d[list(combo)].mean(axis=1)
            recent = d[d["date"] >= "2024-01-01"]
            ric = daily_ic(recent, "score", method="spearman")
            m = float(summarize_ic(ric)["mean"])
            roll = float(rolling_positive_share(ric, window=126))
            gate = evaluate_gate12a(m, roll)
            name = "+".join(combo)
            rows.append(
                {
                    "name": name,
                    "k": k,
                    "rankic": m,
                    "roll": roll,
                    "gate12a": gate["pass"],
                }
            )
            mark = "PASS" if gate["pass"] else "fail"
            print(f"{mark} {m:+.4f} roll={roll:.3f} | {name}")
            if best is None or m > best["rankic"]:
                best = rows[-1]

    rows.sort(key=lambda r: r["rankic"], reverse=True)
    OUT.write_text(
        json.dumps({"best": best, "all": rows}, indent=2),
        encoding="utf-8",
    )
    print("BEST", best)
    print("any PASS", any(r["gate12a"] for r in rows))
    print("Saved", OUT)


if __name__ == "__main__":
    main()
