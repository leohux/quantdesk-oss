# -*- coding: utf-8 -*-
"""Equal-weight CS-rank combo of near-miss factors + multi-horizon diagnostics.

Signs locked on TRAIN only. No ML.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_near_miss_combo
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

from research.alpha_v2.features.extended import build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_near_miss_combo.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

SPLITS = {
    "train": ("2021-07-01", "2022-12-31"),
    "valid": ("2023-01-01", "2023-12-31"),
    "oos": ("2024-01-01", "2025-12-31"),
    "holdout": ("2026-01-01", None),
}

# Near-misses from feature screen (train signs)
COMBO_SPECS = [
    {"factor": "vol_adj_rev_5", "sign": 1.0, "neutralize": False},
    {"factor": "rev_5d", "sign": -1.0, "neutralize": False},
    {"factor": "near_high_252", "sign": 1.0, "neutralize": True},
    {"factor": "r_squared_proxy", "sign": 1.0, "neutralize": False},
]


def _slice(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    m = df["date"] >= pd.Timestamp(start)
    if end:
        m &= df["date"] <= pd.Timestamp(end)
    return df.loc[m]


def cs_rank(s: pd.Series) -> pd.Series:
    return s.groupby(level=0).rank(pct=True)


def eval_panel(d: pd.DataFrame, score_col: str = "score") -> dict:
    out = {"splits": {}}
    for name, (start, end) in SPLITS.items():
        part = _slice(d, start, end)
        ric = daily_ic(part, score_col, method="spearman")
        out["splits"][name] = summarize_ic(ric)

    recent = d[d["date"] >= pd.Timestamp("2024-01-01")]
    ric_r = daily_ic(recent, score_col, method="spearman")
    ric_s = summarize_ic(ric_r)
    roll = rolling_positive_share(ric_r, window=126)
    out["recent_2024plus"] = {
        "rankic": ric_s,
        "rolling_pos_share": roll,
        "gate12a": evaluate_gate12a(ric_s["mean"], roll),
    }
    return out


def main() -> None:
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    feats = build_extended_features(close)
    sector_map = load_sector_map()
    label5 = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label5, eligible)
    panel["date"] = pd.to_datetime(panel["date"])

    # Build signed CS-rank columns
    rank_cols = []
    for spec in COMBO_SPECS:
        f = spec["factor"]
        raw = panel[f].astype(float)
        if spec["neutralize"]:
            tmp = panel[["date", "symbol"]].copy()
            tmp[f] = raw
            raw = industry_neutralize_score(tmp, f, sector_map)
        signed = raw * float(spec["sign"])
        col = f"rk_{f}{'_n' if spec['neutralize'] else ''}"
        # daily CS percentile rank
        tmp2 = panel[["date"]].copy()
        tmp2["v"] = signed
        panel[col] = tmp2.groupby("date")["v"].rank(pct=True)
        rank_cols.append(col)

    panel["combo_eq"] = panel[rank_cols].mean(axis=1)
    # Reversal-only combo (drop trend)
    rev_cols = [c for c in rank_cols if "rev" in c or "vol_adj" in c]
    panel["combo_rev"] = panel[rev_cols].mean(axis=1)
    # Trend/quality combo
    tq_cols = [c for c in rank_cols if "near_high" in c or "r_squared" in c]
    panel["combo_tq"] = panel[tq_cols].mean(axis=1)

    results: dict = {"combos": {}, "horizons": {}, "weekly_sample": {}}

    for name in ["combo_eq", "combo_rev", "combo_tq"]:
        d = panel.dropna(subset=[name, "label"]).copy()
        d["score"] = d[name]
        ev = eval_panel(d)
        results["combos"][name] = ev
        g = "PASS" if ev["recent_2024plus"]["gate12a"]["pass"] else "FAIL"
        print(
            f"{name}: oos={ev['splits']['oos']['mean']:+.4f} "
            f"2024+={ev['recent_2024plus']['rankic']['mean']:+.4f} "
            f"roll={ev['recent_2024plus']['rolling_pos_share']:.3f} gate12a={g}"
        )

    # Multi-horizon diagnostic on best single near-miss + combo_eq
    for h in (1, 5, 10, 21):
        lab = forward_return(close, horizon=h, entry_lag=1)
        p = align_xy({"vol_adj_rev_5": feats["vol_adj_rev_5"]}, lab, eligible)
        p["date"] = pd.to_datetime(p["date"])
        # train sign for this horizon
        train = _slice(p.dropna(subset=["vol_adj_rev_5", "label"]), *SPLITS["train"])
        train = train.assign(score=train["vol_adj_rev_5"])
        tr = summarize_ic(daily_ic(train, "score", method="spearman"))["mean"]
        sign = 1.0 if tr >= 0 else -1.0
        p = p.dropna(subset=["vol_adj_rev_5", "label"]).assign(
            score=lambda x: x["vol_adj_rev_5"] * sign
        )
        recent = p[p["date"] >= pd.Timestamp("2024-01-01")]
        ric = summarize_ic(daily_ic(recent, "score", method="spearman"))
        results["horizons"][f"vol_adj_rev_5_h{h}"] = {
            "sign": sign,
            "train_raw": tr,
            "recent_rankic": ric,
        }
        print(f"horizon T+{h} vol_adj_rev_5 sign={sign:+.0f} 2024+ RankIC={ric['mean']:+.4f}")

    # Weekly subsample RankIC (Fridays) — less overlapping noise
    d = panel.dropna(subset=["combo_eq", "label"]).copy()
    d["score"] = d["combo_eq"]
    d["dow"] = d["date"].dt.dayofweek
    fri = d[d["dow"] == 4]
    ric_w = daily_ic(fri[fri["date"] >= "2024-01-01"], "score", method="spearman")
    results["weekly_sample"]["combo_eq_friday"] = {
        "rankic": summarize_ic(ric_w),
        "n_days": int(ric_w.notna().sum()),
    }
    print(
        f"combo_eq Friday-only 2024+ RankIC="
        f"{results['weekly_sample']['combo_eq_friday']['rankic']['mean']:+.4f} "
        f"n={results['weekly_sample']['combo_eq_friday']['n_days']}"
    )

    best = max(
        results["combos"].items(),
        key=lambda kv: kv[1]["recent_2024plus"]["rankic"]["mean"],
    )
    results["best_combo"] = best[0]
    results["best_recent_rankic"] = best[1]["recent_2024plus"]["rankic"]["mean"]
    results["any_gate12a_pass"] = any(
        v["recent_2024plus"]["gate12a"]["pass"] for v in results["combos"].values()
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(f"Saved {OUT}")

    # Update status
    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
    else:
        st = {}
    st.setdefault("Track_A", {})["near_miss_combo"] = {
        "status": "GATE12A_FAIL" if not results["any_gate12a_pass"] else "GATE12A_PASS",
        "best_combo": results["best_combo"],
        "best_recent_rankic": results["best_recent_rankic"],
        "artifact": str(OUT),
    }
    st["phase121_findings"] = st.get("phase121_findings", {})
    st["phase121_findings"]["combo_note"] = (
        f"Near-miss combo best={results['best_combo']} "
        f"RankIC={results['best_recent_rankic']:.4f}; still below Gate12-A 0.02"
    )
    st["phase121_findings"]["artifacts"] = list(
        dict.fromkeys(
            st["phase121_findings"].get("artifacts", [])
            + [
                "data/research/alpha_v2_feature_screen.json",
                "data/research/alpha_v2_near_miss_combo.json",
                "data/research/event_shock_reversal_factor.json",
            ]
        )
    )
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
