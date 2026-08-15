# -*- coding: utf-8 -*-
"""Phase 12.1: Factor IC decay + excess-return label + industry neutralization.

Priorities:
1) Per-factor RankIC decay by bucket (train/valid/oos/holdout + rolling)
2) Gate12-A persistence filter
3) Absolute vs SPY-excess labels
4) Industry-neutralized factor ranks

No ML complexity. Does not touch Round1 archived strategies.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_factor_decay
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

from research.alpha_v2.features.liquidity import build_liquidity_features
from research.alpha_v2.features.momentum import build_momentum_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.features.trend_quality import build_trend_features
from research.alpha_v2.features.volatility import build_volatility_features
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_factor_decay.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

BUCKETS = {
    "momentum": [
        "return_5d",
        "return_20d",
        "return_60d",
        "return_120d",
        "skip_mom_20_5",
    ],
    "volatility": [
        "vol_20d",
        "downside_vol_20d",
        "atr_ratio_20d",
        "max_drawdown_60d",
    ],
    "liquidity": [
        "avg_volume_20d",
        "turnover_20d",
        "volume_change_20d",
    ],
    "trend_quality": [
        "price_vs_ma20",
        "price_vs_ma60",
        "slope_60",
        "r_squared_60",
    ],
}

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


def build_features(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    feats: dict[str, pd.DataFrame] = {}
    feats.update(build_momentum_features(close))
    feats.update(build_volatility_features(close))
    feats.update(build_liquidity_features(close, volume=None))
    feats.update(build_trend_features(close))
    return feats


def excess_label(close: pd.DataFrame, spy: pd.Series, horizon: int = 5, entry_lag: int = 1) -> pd.DataFrame:
    """stock forward return minus SPY forward return (same horizon/lag)."""
    stock = forward_return(close, horizon=horizon, entry_lag=entry_lag)
    spy_df = pd.DataFrame({c: spy for c in close.columns})
    spy_fwd = forward_return(spy_df, horizon=horizon, entry_lag=entry_lag)
    return stock - spy_fwd


def eval_factor(
    panel: pd.DataFrame,
    feat: str,
    *,
    neutralize: bool = False,
    sector_map: pd.Series | None = None,
) -> dict:
    d = panel.dropna(subset=[feat, "label"]).copy()
    score_col = feat
    if neutralize and sector_map is not None:
        d["_score"] = industry_neutralize_score(d, feat, sector_map)
        score_col = "_score"
    else:
        d["_score"] = d[feat]
        score_col = "_score"

    out = {"factor": feat, "neutralized": neutralize, "splits": {}}
    for name, (start, end) in SPLITS.items():
        part = _slice(d, start, end)
        if len(part) < 500:
            out["splits"][name] = {"note": "too_few_rows", "n": len(part)}
            continue
        ric = daily_ic(part.rename(columns={score_col: "score"}), "score", method="spearman")
        ic = daily_ic(part.rename(columns={score_col: "score"}), "score", method="pearson")
        ric_s = summarize_ic(ric)
        ic_s = summarize_ic(ic)
        # full-span rolling on this split
        roll = rolling_positive_share(ric, window=min(126, max(20, len(ric) // 3)))
        # Gate12-A on OOS-like persistence: use combined oos+holdout when evaluating survivors
        out["splits"][name] = {
            "ic": ic_s,
            "rankic": ric_s,
            "rolling_pos_share": roll,
        }

    # persistence on 2024+ window
    recent = d[d["date"] >= pd.Timestamp("2024-01-01")]
    if len(recent) >= 500:
        ric_r = daily_ic(recent.rename(columns={score_col: "score"}), "score", method="spearman")
        ric_rs = summarize_ic(ric_r)
        roll_r = rolling_positive_share(ric_r, window=126)
        gate = evaluate_gate12a(ric_rs["mean"], roll_r)
    else:
        ric_rs = {"mean": np.nan}
        roll_r = np.nan
        gate = {"pass": False, "checks": [], "note": "insufficient recent sample"}
    out["recent_2024plus"] = {
        "rankic": ric_rs,
        "rolling_pos_share": roll_r,
        "gate12a": gate,
    }
    return out


def load_spy(close: pd.DataFrame) -> pd.Series:
    if "SPY" in close.columns:
        return close["SPY"].astype(float)
    # equal-weight panel proxy if SPY missing (avoids Yahoo rate limits)
    return close.pct_change().mean(axis=1).fillna(0.0).add(1.0).cumprod()


def main() -> None:
    print("Phase12.1 factor decay analysis...")
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    spy = load_spy(close)
    feats = build_features(close)
    try:
        sector_map = load_sector_map()
        print(f"sector map symbols={len(sector_map)}")
    except Exception as exc:
        print(f"sector map failed: {exc}")
        sector_map = None

    label_abs = forward_return(close, horizon=5, entry_lag=1)
    label_xs = excess_label(close, spy, horizon=5, entry_lag=1)

    panels = {
        "absolute_t5": align_xy(feats, label_abs, eligible),
        "excess_vs_spy_t5": align_xy(feats, label_xs, eligible),
    }
    for k in panels:
        panels[k]["date"] = pd.to_datetime(panels[k]["date"])

    report: dict = {
        "splits": SPLITS,
        "note_excess_label": (
            "Subtracting the same market return from all names on date t does NOT change "
            "cross-sectional RankIC. Excess label is kept for portfolio/attribution diagnostics; "
            "industry neutralization is the CS-relevant residualization."
        ),
        "buckets": {},
        "survivors_gate12a": {"absolute_t5": [], "excess_vs_spy_t5": []},
        "neutralized_survivors": [],
    }

    for label_name, panel in panels.items():
        print(f"\n=== Label: {label_name} ===")
        report["buckets"][label_name] = {}
        for bucket, feat_list in BUCKETS.items():
            report["buckets"][label_name][bucket] = {}
            print(f"[{bucket}]")
            for feat in feat_list:
                if feat not in panel.columns:
                    continue
                res = eval_factor(panel, feat, neutralize=False)
                report["buckets"][label_name][bucket][feat] = res
                oos = res["splits"].get("oos", {}).get("rankic", {}).get("mean", np.nan)
                recent = res["recent_2024plus"]["rankic"].get("mean", np.nan)
                g = res["recent_2024plus"]["gate12a"]["pass"]
                print(
                    f"  {feat:<22} oos_RankIC={oos:+.4f} "
                    f"2024+_RankIC={recent:+.4f} gate12a={'PASS' if g else 'FAIL'}"
                )
                if g:
                    report["survivors_gate12a"][label_name].append(
                        {
                            "bucket": bucket,
                            "factor": feat,
                            "oos_rankic": oos,
                            "recent_rankic": recent,
                            "rolling_pos_share": res["recent_2024plus"]["rolling_pos_share"],
                        }
                    )

                if sector_map is not None and label_name == "absolute_t5":
                    res_n = eval_factor(panel, feat, neutralize=True, sector_map=sector_map)
                    report["buckets"][label_name][bucket][feat]["industry_neutral"] = {
                        "splits": {
                            k: v for k, v in res_n["splits"].items() if isinstance(v, dict)
                        },
                        "recent_2024plus": res_n["recent_2024plus"],
                    }
                    oos_n = (
                        res_n["splits"].get("oos", {}).get("rankic", {}).get("mean", float("nan"))
                    )
                    recent_n = res_n["recent_2024plus"]["rankic"].get("mean", float("nan"))
                    g_n = res_n["recent_2024plus"]["gate12a"]["pass"]
                    print(
                        f"  {feat:<22} NEUT oos_RankIC={oos_n:+.4f} "
                        f"2024+_RankIC={recent_n:+.4f} gate12a={'PASS' if g_n else 'FAIL'}"
                    )
                    if g_n:
                        report["neutralized_survivors"].append(
                            {
                                "factor": feat,
                                "bucket": bucket,
                                "oos_rankic": oos_n,
                                "recent_rankic": recent_n,
                                "rolling_pos_share": res_n["recent_2024plus"]["rolling_pos_share"],
                            }
                        )

    # Summary recommendation
    abs_s = report["survivors_gate12a"]["absolute_t5"]
    xs_s = report["survivors_gate12a"]["excess_vs_spy_t5"]
    neu_s = report["neutralized_survivors"]
    if neu_s:
        rec = (
            f"feature_research_continue - {len(neu_s)} industry-neutral excess factors "
            "pass Gate12-A on 2024+; promote those buckets only"
        )
    elif xs_s:
        rec = (
            f"feature_research_continue - {len(xs_s)} excess-return factors pass Gate12-A; "
            "prefer excess label over absolute"
        )
    elif abs_s:
        rec = (
            f"feature_research_continue - {len(abs_s)} absolute factors pass Gate12-A; "
            "re-check after excess/neutralization"
        )
    else:
        rec = (
            "no_persistent_single_factor_2024plus - expand feature set / start PEAD Track B; "
            "do not add ML complexity yet"
        )
    report["recommendation"] = rec
    print(f"\nSurvivors abs={len(abs_s)} excess={len(xs_s)} neutral_excess={len(neu_s)}")
    print(f"RECOMMENDATION: {rec}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")

    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["phase"] = "phase12_true_alpha_search"
        st["Track_A"] = {
            "cross_section_rank": {
                "status": "FEATURE_RESEARCH",
                "gate12": "FAIL",
                "gate12a": "RUNNING",
            }
        }
        st["Track_B"] = {"PEAD": {"status": "INITIAL_RESEARCH"}}
        st["alpha_v2_phase121"] = {
            "survivors_abs": abs_s,
            "survivors_excess": xs_s,
            "survivors_neutral_excess": neu_s,
            "recommendation": rec,
            "artifact": str(OUT),
        }
        st["live_candidates"] = {"count": 0}
        st["live"] = "LOCKED"
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
