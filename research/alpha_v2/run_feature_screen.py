# -*- coding: utf-8 -*-
"""Phase12.1 continued: signed-factor persistence + extended feature screen.

- Expand price-only feature set (reversal / residual mom / lottery / shocks)
- Fix factor sign on TRAIN only, then evaluate Valid/OOS/Holdout
- Gate12-A on signed 2024+ RankIC
- No ML

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_feature_screen
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

from research.alpha_v2.features.extended import BUCKETS_EXT, build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_feature_screen.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

SPLITS = {
    "train": ("2021-07-01", "2022-12-31"),
    "valid": ("2023-01-01", "2023-12-31"),
    "oos": ("2024-01-01", "2025-12-31"),
    "holdout": ("2026-01-01", None),
}


def _slice(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    m = df["date"] >= pd.Timestamp(start)
    if end:
        m &= df["date"] <= pd.Timestamp(end)
    return df.loc[m]


def signed_score(panel: pd.DataFrame, feat: str, sign: float) -> pd.Series:
    return panel[feat] * sign


def mean_rankic(panel: pd.DataFrame, score: pd.Series) -> float:
    d = panel[["date", "symbol", "label"]].copy()
    d["score"] = score
    d = d.dropna(subset=["score", "label"])
    ric = daily_ic(d, "score", method="spearman")
    return float(summarize_ic(ric)["mean"])


def eval_signed(
    panel: pd.DataFrame,
    feat: str,
    *,
    neutralize: bool = False,
    sector_map: pd.Series | None = None,
) -> dict:
    d = panel.dropna(subset=[feat, "label"]).copy()
    raw = d[feat].astype(float)
    if neutralize and sector_map is not None:
        tmp = d[["date", "symbol"]].copy()
        tmp[feat] = raw
        raw = industry_neutralize_score(tmp, feat, sector_map)

    train = _slice(d.assign(score=raw), *SPLITS["train"])
    # sign from train RankIC only
    train_ric = mean_rankic(train, train["score"])
    sign = 1.0 if (train_ric == train_ric and train_ric >= 0) else -1.0
    d = d.assign(score=raw * sign)

    out = {
        "factor": feat,
        "neutralized": neutralize,
        "train_rankic_raw": train_ric,
        "sign": sign,
        "splits": {},
    }
    for name, (start, end) in SPLITS.items():
        part = _slice(d, start, end)
        if len(part) < 500:
            out["splits"][name] = {"note": "too_few", "n": len(part)}
            continue
        ric = daily_ic(part, "score", method="spearman")
        ic = daily_ic(part, "score", method="pearson")
        out["splits"][name] = {
            "ic": summarize_ic(ic),
            "rankic": summarize_ic(ric),
            "rolling_pos_share": rolling_positive_share(
                ric, window=min(126, max(20, len(ric) // 3))
            ),
        }

    recent = d[d["date"] >= pd.Timestamp("2024-01-01")]
    ric_r = daily_ic(recent, "score", method="spearman")
    ric_s = summarize_ic(ric_r)
    roll = rolling_positive_share(ric_r, window=126)
    gate = evaluate_gate12a(ric_s["mean"], roll)
    out["recent_2024plus"] = {
        "rankic": ric_s,
        "rolling_pos_share": roll,
        "gate12a": gate,
    }

    # diagnostic secondary horizons on OOS only (not for gate pass)
    close_idx = None  # filled by caller optional
    out["diagnostics"] = {}
    return out


def decile_spread(panel: pd.DataFrame, score_col: str = "score") -> dict:
    """Gross long top-decile / short bottom-decile of next label (diagnostic)."""
    rows = []
    for dt, g in panel.dropna(subset=[score_col, "label"]).groupby("date"):
        if len(g) < 40:
            continue
        q = g[score_col].quantile([0.1, 0.9])
        top = g[g[score_col] >= q.loc[0.9]]["label"].mean()
        bot = g[g[score_col] <= q.loc[0.1]]["label"].mean()
        rows.append(top - bot)
    if not rows:
        return {"mean_spread": np.nan, "n_days": 0}
    arr = np.array(rows, dtype=float)
    return {
        "mean_spread": float(arr.mean()),
        "ann_spread_approx": float(arr.mean() * 252 / 5),  # T+5 label spacing rough
        "hit": float((arr > 0).mean()),
        "n_days": int(len(arr)),
    }


def main() -> None:
    print("Phase12.1 feature screen (signed factors)...")
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    feats = build_extended_features(close)
    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])
    try:
        sector_map = load_sector_map()
    except Exception as exc:
        print(f"sector map failed: {exc}")
        sector_map = None

    report = {"buckets": {}, "survivors": [], "near_misses": []}
    for bucket, feats_list in BUCKETS_EXT.items():
        report["buckets"][bucket] = {}
        print(f"\n[{bucket}]")
        for feat in feats_list:
            if feat not in panel.columns:
                continue
            res = eval_signed(panel, feat, neutralize=False)
            report["buckets"][bucket][feat] = res
            oos = res["splits"].get("oos", {}).get("rankic", {}).get("mean", np.nan)
            recent = res["recent_2024plus"]["rankic"].get("mean", np.nan)
            g = res["recent_2024plus"]["gate12a"]["pass"]
            print(
                f"  {feat:<18} sign={res['sign']:+.0f} "
                f"oos={oos:+.4f} 2024+={recent:+.4f} "
                f"gate12a={'PASS' if g else 'FAIL'}"
            )

            # OOS decile diagnostic with signed score
            oos_panel = _slice(panel.dropna(subset=[feat, "label"]), *SPLITS["oos"]).copy()
            raw = oos_panel[feat]
            if False:
                pass
            oos_panel["score"] = raw * res["sign"]
            spread = decile_spread(oos_panel)
            res["oos_decile_spread"] = spread
            report["buckets"][bucket][feat]["oos_decile_spread"] = spread

            item = {
                "bucket": bucket,
                "factor": feat,
                "sign": res["sign"],
                "oos_rankic": oos,
                "recent_rankic": recent,
                "rolling_pos": res["recent_2024plus"]["rolling_pos_share"],
                "oos_decile_spread": spread.get("mean_spread"),
                "neutralized": False,
            }
            if g:
                report["survivors"].append(item)
            elif recent == recent and recent > 0.01:
                report["near_misses"].append(item)

            if sector_map is not None:
                res_n = eval_signed(panel, feat, neutralize=True, sector_map=sector_map)
                report["buckets"][bucket][feat]["industry_neutral"] = {
                    "sign": res_n["sign"],
                    "splits": res_n["splits"],
                    "recent_2024plus": res_n["recent_2024plus"],
                }
                rn = res_n["recent_2024plus"]["rankic"].get("mean", np.nan)
                gn = res_n["recent_2024plus"]["gate12a"]["pass"]
                print(
                    f"  {feat:<18} NEUT sign={res_n['sign']:+.0f} "
                    f"2024+={rn:+.4f} gate12a={'PASS' if gn else 'FAIL'}"
                )
                if gn:
                    report["survivors"].append(
                        {
                            "bucket": bucket,
                            "factor": feat,
                            "sign": res_n["sign"],
                            "oos_rankic": res_n["splits"].get("oos", {}).get("rankic", {}).get("mean"),
                            "recent_rankic": rn,
                            "rolling_pos": res_n["recent_2024plus"]["rolling_pos_share"],
                            "neutralized": True,
                        }
                    )
                elif rn == rn and rn > 0.01:
                    report["near_misses"].append(
                        {
                            "bucket": bucket,
                            "factor": feat,
                            "sign": res_n["sign"],
                            "recent_rankic": rn,
                            "rolling_pos": res_n["recent_2024plus"]["rolling_pos_share"],
                            "neutralized": True,
                        }
                    )

    # rank near misses
    report["near_misses"] = sorted(
        report["near_misses"],
        key=lambda x: -(x.get("recent_rankic") or -1),
    )[:15]
    report["survivors"] = sorted(
        report["survivors"],
        key=lambda x: -(x.get("recent_rankic") or -1),
    )

    if report["survivors"]:
        rec = (
            f"promote_{len(report['survivors'])}_factors_to_combo - "
            "Gate12-A survivors found; next: equal-weight combo + Gate12/13"
        )
    elif report["near_misses"]:
        top = report["near_misses"][0]
        rec = (
            f"feature_research_continue - no Gate12-A pass; best near-miss "
            f"{top['factor']} recent_RankIC={top['recent_rankic']:.4f}; "
            "try combo of near-misses / better event data"
        )
    else:
        rec = "feature_set_weak_2024plus - expand alt data / PEAD; still no ML"

    report["recommendation"] = rec
    print(f"\nSurvivors={len(report['survivors'])} near_misses={len(report['near_misses'])}")
    print(f"RECOMMENDATION: {rec}")

    OUT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["Track_A"] = {
            "cross_section_rank": {
                "status": "FEATURE_RESEARCH",
                "gate12": "FAIL",
                "gate12a": "PASS" if report["survivors"] else "FAIL",
                "survivors": report["survivors"][:10],
                "near_misses": report["near_misses"][:10],
                "recommendation": rec,
                "artifact": str(OUT),
            }
        }
        st["live"] = "LOCKED"
        st["live_candidates"] = {"count": 0}
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
