# -*- coding: utf-8 -*-
"""CS factor from post-shock reversal (Track B bridge into Track A).

Daily factor: after large |shock_z|, score = -shock_z (expect reversal).
Evaluate RankIC vs T+5 label with train-only sign lock.

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_shock_reversal_factor
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

from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT = ROOT / "data" / "research" / "event_shock_reversal_factor.json"


def main() -> None:
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    ret = close.pct_change()
    vol = ret.rolling(20).std()
    shock = ret / vol.replace(0, np.nan)
    # only keep large shocks; others NaN so they drop from CS that day sparsely
    # Better continuous factor: -shock everywhere (reversal)
    feats = {
        "neg_shock_z": -shock,
        "neg_shock_z_large_only": -shock.where(shock.abs() >= 1.5),
        "neg_ret5_after_vol": -(close / close.shift(5) - 1.0) / vol.replace(0, np.nan),
    }
    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])

    results = {}
    for feat in feats:
        d = panel.dropna(subset=[feat, "label"]).copy()
        train = d[(d.date >= "2021-07-01") & (d.date <= "2022-12-31")]
        # sign lock
        tr = train.copy()
        tr["score"] = tr[feat]
        tr_ric = summarize_ic(daily_ic(tr, "score", method="spearman"))["mean"]
        sign = 1.0 if tr_ric >= 0 else -1.0
        d["score"] = d[feat] * sign

        out = {"sign": sign, "train_rankic_raw": tr_ric, "splits": {}}
        for name, start, end in [
            ("train", "2021-07-01", "2022-12-31"),
            ("valid", "2023-01-01", "2023-12-31"),
            ("oos", "2024-01-01", "2025-12-31"),
            ("holdout", "2026-01-01", None),
        ]:
            part = d[d.date >= start]
            if end:
                part = part[part.date <= end]
            ric = daily_ic(part, "score", method="spearman")
            out["splits"][name] = summarize_ic(ric)

        recent = d[d.date >= "2024-01-01"]
        ric_r = daily_ic(recent, "score", method="spearman")
        ric_s = summarize_ic(ric_r)
        roll = rolling_positive_share(ric_r, window=126)
        out["recent_2024plus"] = {
            "rankic": ric_s,
            "rolling_pos_share": roll,
            "gate12a": evaluate_gate12a(ric_s["mean"], roll),
        }
        results[feat] = out
        print(
            f"{feat}: sign={sign:+.0f} oos={out['splits']['oos']['mean']:+.4f} "
            f"2024+={ric_s['mean']:+.4f} gate12a="
            f"{'PASS' if out['recent_2024plus']['gate12a']['pass'] else 'FAIL'}"
        )

    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
