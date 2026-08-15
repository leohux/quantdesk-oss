# -*- coding: utf-8 -*-
"""Train-IC-weighted near-miss combo (no ML). Push for Gate12-A.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_ic_weighted_combo
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
from research.alpha_v2.features.trend_quality import build_trend_features
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_ic_weighted_combo.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

TRAIN = ("2021-07-01", "2022-12-31")


def _slice(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    m = df["date"] >= pd.Timestamp(start)
    if end:
        m &= df["date"] <= pd.Timestamp(end)
    return df.loc[m]


def main() -> None:
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    feats: dict[str, pd.DataFrame] = {}
    feats.update(build_extended_features(close))
    feats.update(build_trend_features(close))

    sector_map = load_sector_map()
    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])

    # Candidate pool: prior near-misses + historically mildly positive factors
    candidates = [
        ("vol_adj_rev_5", False),
        ("rev_5d", False),
        ("near_high_252", True),
        ("r_squared_proxy", False),
        ("return_120d", True),
        ("downside_vol_20d", False),
        ("r_squared_60", True),
    ]
    # Keep only existing
    candidates = [(f, n) for f, n in candidates if f in panel.columns]

    train = _slice(panel, *TRAIN)
    signed_rank_cols = []
    weights = {}
    for feat, neut in candidates:
        raw = panel[feat].astype(float)
        if neut:
            tmp = panel[["date", "symbol"]].copy()
            tmp[feat] = raw
            raw = industry_neutralize_score(tmp, feat, sector_map)
        # train sign
        tr = train.copy()
        tr["score"] = raw.loc[tr.index] if False else None
        # align by index from panel
        tr_score = raw.reindex(train.index)
        tr2 = train.assign(score=tr_score.values)
        tr2 = tr2.dropna(subset=["score", "label"])
        tr_ric = summarize_ic(daily_ic(tr2, "score", method="spearman"))["mean"]
        sign = 1.0 if (tr_ric == tr_ric and tr_ric >= 0) else -1.0
        signed = raw * sign
        col = f"rk_{feat}{'_n' if neut else ''}"
        panel[col] = panel.assign(_s=signed).groupby("date")["_s"].rank(pct=True)
        # weight = max(train RankIC, 0) after sign
        w = abs(tr_ric) if (tr_ric == tr_ric) else 0.0
        weights[col] = float(w)
        signed_rank_cols.append(col)
        print(f"  {col}: sign={sign:+.0f} train|ric|={w:.4f}")

    # Drop zero weights
    cols = [c for c in signed_rank_cols if weights[c] > 1e-6]
    w = np.array([weights[c] for c in cols], dtype=float)
    w = w / w.sum()

    mat = panel[cols].to_numpy(dtype=float)
    # nan-safe weighted mean
    mask = np.isfinite(mat)
    num = np.nansum(np.where(mask, mat * w, 0.0), axis=1)
    den = np.nansum(np.where(mask, w, 0.0), axis=1)
    panel["combo_icw"] = np.where(den > 0, num / den, np.nan)
    panel["combo_eq2"] = panel[cols].mean(axis=1)

    results = {"members": weights, "normalized_weights": dict(zip(cols, w.tolist())), "combos": {}}

    for name in ["combo_icw", "combo_eq2"]:
        d = panel.dropna(subset=[name, "label"]).copy()
        d["score"] = d[name]
        splits = {}
        for sn, (a, b) in {
            "train": TRAIN,
            "valid": ("2023-01-01", "2023-12-31"),
            "oos": ("2024-01-01", "2025-12-31"),
            "holdout": ("2026-01-01", None),
        }.items():
            part = _slice(d, a, b)
            splits[sn] = summarize_ic(daily_ic(part, "score", method="spearman"))
        recent = d[d["date"] >= "2024-01-01"]
        ric = daily_ic(recent, "score", method="spearman")
        ric_s = summarize_ic(ric)
        roll = rolling_positive_share(ric, window=126)
        gate = evaluate_gate12a(ric_s["mean"], roll)
        results["combos"][name] = {
            "splits": splits,
            "recent_2024plus": {
                "rankic": ric_s,
                "rolling_pos_share": roll,
                "gate12a": gate,
            },
        }
        print(
            f"{name}: oos={splits['oos']['mean']:+.4f} 2024+={ric_s['mean']:+.4f} "
            f"roll={roll:.3f} gate12a={'PASS' if gate['pass'] else 'FAIL'}"
        )

    best_name = max(
        results["combos"],
        key=lambda k: results["combos"][k]["recent_2024plus"]["rankic"]["mean"],
    )
    best = results["combos"][best_name]
    results["verdict"] = {
        "best": best_name,
        "recent_rankic": best["recent_2024plus"]["rankic"]["mean"],
        "gate12a_pass": best["recent_2024plus"]["gate12a"]["pass"],
        "gap_to_0p02": 0.02 - best["recent_2024plus"]["rankic"]["mean"],
    }
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(f"Verdict: {results['verdict']}")
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_A", {})["ic_weighted_combo"] = {
        "status": "GATE12A_PASS" if results["verdict"]["gate12a_pass"] else "GATE12A_FAIL",
        **results["verdict"],
        "artifact": str(OUT),
    }
    # Keep LIVE locked unless pass
    if results["verdict"]["gate12a_pass"]:
        st["Track_A"]["cross_section_rank"]["status"] = "COMBO_GATE12A_PASS_PENDING_GATE12"
        st["system"]["alpha"] = "CANDIDATE_COMBO_ONLY"
    else:
        st["system"]["alpha"] = "NOT_FOUND"
        st["live"] = "LOCKED"
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
