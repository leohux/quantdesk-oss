# -*- coding: utf-8 -*-
"""Phase 12 Track A baseline: T+5 RankIC linear model on PIT S&P panel."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.alpha_v2.features.liquidity import build_liquidity_features
from research.alpha_v2.features.momentum import build_momentum_features
from research.alpha_v2.features.trend_quality import build_trend_features
from research.alpha_v2.features.volatility import build_volatility_features
from research.alpha_v2.gates.hard_gate12 import evaluate_gate12
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return
from research.alpha_v2.models.linear_rank import fit_linear_rank, score_linear_rank
from research.alpha_v2.models.lgbm_rank import fit_lgbm_rank, score_lgbm_rank

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_baseline.json"
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


def build_panel() -> pd.DataFrame:
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    # align columns
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    feats: dict[str, pd.DataFrame] = {}
    feats.update(build_momentum_features(close))
    feats.update(build_volatility_features(close))
    feats.update(build_liquidity_features(close, volume=None))
    feats.update(build_trend_features(close))
    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])
    return panel


def eval_scores(scored: pd.DataFrame, name: str) -> dict:
    ic = daily_ic(scored, "score", method="pearson")
    ric = daily_ic(scored, "score", method="spearman")
    ic_s = summarize_ic(ic)
    ric_s = summarize_ic(ric)
    roll = rolling_positive_share(ric, window=252)
    gate = evaluate_gate12(ic_s, ric_s, roll)
    print(
        f"{name}: IC={ic_s['mean']:.4f} RankIC={ric_s['mean']:.4f} "
        f"IR={ric_s['ir']:.2f} roll_hit={roll:.1%} gate12={'PASS' if gate['pass'] else 'FAIL'}"
    )
    return {
        "ic": ic_s,
        "rankic": ric_s,
        "rolling_rankic_pos_share_12m": roll,
        "gate12": gate,
    }


def main() -> None:
    print("Building Phase12 feature/label panel (PIT S&P cache)...")
    panel = build_panel()
    print(f"panel rows={len(panel):,} features={panel.shape[1]-3}")

    train = _slice(panel, *SPLITS["train"])
    valid = _slice(panel, *SPLITS["valid"])
    oos = _slice(panel, *SPLITS["oos"])
    holdout = _slice(panel, SPLITS["holdout"][0], None)

    weights = fit_linear_rank(train)
    results = {"splits": SPLITS, "n_features": len([k for k in weights if k != "intercept"])}
    results["linear"] = {
        "weights_top": dict(
            sorted(
                ((k, v) for k, v in weights.items() if k != "intercept"),
                key=lambda kv: abs(kv[1]),
                reverse=True,
            )[:10]
        ),
        "train": eval_scores(score_linear_rank(train, weights), "linear/train"),
        "valid": eval_scores(score_linear_rank(valid, weights), "linear/valid"),
        "oos": eval_scores(score_linear_rank(oos, weights), "linear/oos"),
        "holdout_2026": eval_scores(score_linear_rank(holdout, weights), "linear/holdout2026")
        if len(holdout) > 1000
        else {"note": "insufficient_holdout"},
    }

    model = fit_lgbm_rank(train)
    if model is not None:
        results["lgbm"] = {
            "valid": eval_scores(score_lgbm_rank(valid, model), "lgbm/valid"),
            "oos": eval_scores(score_lgbm_rank(oos, model), "lgbm/oos"),
        }
    else:
        results["lgbm"] = {"note": "lightgbm not installed; skipped"}
        print("lgbm: skipped (package not installed)")

    oos_pass = bool(results["linear"]["oos"]["gate12"]["pass"])
    results["recommendation"] = (
        "gate12_pass - proceed to long/short decile + Gate13 cost reality"
        if oos_pass
        else (
            "gate12_fail - keep researching features/labels; "
            "do not touch archived Round1 strategies"
        )
    )
    print(f"\nRECOMMENDATION: {results['recommendation']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")

    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st["phase"] = "phase12_true_alpha_search"
        st["hard_gates"]["gate12_rankic"] = "PASS" if oos_pass else "FAIL"
        st["alpha_v2"] = {
            "linear_oos_rankic": results["linear"]["oos"]["rankic"]["mean"],
            "linear_oos_ic": results["linear"]["oos"]["ic"]["mean"],
            "gate12_pass": oos_pass,
            "artifact": str(OUT),
        }
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
