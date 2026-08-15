# -*- coding: utf-8 -*-
"""Build PIT fundamental CS factors from SEC panels + Gate12-A screen.

Factors (availability = SEC filed date, forward-filled daily):
  - book_to_price  (equity / (price * shares))
  - earnings_yield (EPS / price)  [latest quarterly EPS — noisy]
  - roe            (ni / equity)
  - asset_growth   (assets / assets_lag4q - 1)

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_fundamental_screen
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

from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_fundamental_screen.json"
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


def wide_from_fund(fund: pd.DataFrame, close: pd.DataFrame, col: str) -> pd.DataFrame:
    """Pivot filed-level fund[col] to daily wide panel aligned to close index (asof ffill)."""
    f = fund.dropna(subset=[col, "filed", "symbol"]).copy()
    f["filed"] = pd.to_datetime(f["filed"])
    # pivot last value per symbol-filed
    piv = f.pivot_table(index="filed", columns="symbol", values=col, aggfunc="last")
    piv = piv.sort_index()
    # reindex to daily calendar then ffill (available after filed)
    daily = piv.reindex(close.index.union(piv.index)).sort_index().ffill()
    daily = daily.reindex(close.index)
    # keep columns in close
    cols = [c for c in close.columns if c in daily.columns]
    return daily[cols]


def build_fundamental_features(close: pd.DataFrame, fund: pd.DataFrame) -> dict[str, pd.DataFrame]:
    equity = wide_from_fund(fund, close, "equity")
    assets = wide_from_fund(fund, close, "assets")
    ni = wide_from_fund(fund, close, "ni")
    eps = wide_from_fund(fund, close, "eps")
    shares = wide_from_fund(fund, close, "shares")

    # align
    common = sorted(
        set(equity.columns) & set(close.columns) & set(shares.columns)
    )
    px = close[common]
    eq = equity.reindex(columns=common)
    sh = shares.reindex(columns=common)
    mcap = px * sh.replace(0, np.nan)
    book_to_price = eq / mcap.replace(0, np.nan)

    common2 = sorted(set(eps.columns) & set(close.columns))
    earnings_yield = eps[common2] / close[common2].replace(0, np.nan)

    common3 = sorted(set(ni.columns) & set(eq.columns))
    roe = ni[common3] / eq[common3].replace(0, np.nan)

    # asset growth ~ YoY using 252 trading day lag of assets level
    common4 = sorted(set(assets.columns) & set(close.columns))
    a = assets[common4]
    asset_growth = a / a.shift(252) - 1.0

    # quality-ish: inverse accruals proxy not available; use roe + negative asset growth
    return {
        "book_to_price": book_to_price,
        "earnings_yield": earnings_yield,
        "roe": roe,
        "asset_growth": asset_growth,
        "neg_asset_growth": -asset_growth,
    }


def eval_factor(panel: pd.DataFrame, feat: str, neutralize: bool, sector_map) -> dict:
    d = panel.dropna(subset=[feat, "label"]).copy()
    raw = d[feat].astype(float)
    if neutralize:
        tmp = d[["date", "symbol"]].copy()
        tmp[feat] = raw
        raw = industry_neutralize_score(tmp, feat, sector_map)
    train = _slice(d.assign(score=raw), *SPLITS["train"])
    tr_ric = summarize_ic(daily_ic(train.dropna(subset=["score", "label"]), "score", method="spearman"))[
        "mean"
    ]
    sign = 1.0 if (tr_ric == tr_ric and tr_ric >= 0) else -1.0
    d = d.assign(score=raw * sign)
    out = {
        "factor": feat,
        "neutralized": neutralize,
        "train_rankic_raw": tr_ric,
        "sign": sign,
        "splits": {},
    }
    for name, (a, b) in SPLITS.items():
        part = _slice(d, a, b)
        if len(part) < 300:
            out["splits"][name] = {"note": "too_few", "n": int(len(part))}
            continue
        ric = daily_ic(part, "score", method="spearman")
        out["splits"][name] = summarize_ic(ric)
    recent = d[d["date"] >= "2024-01-01"]
    ric = daily_ic(recent, "score", method="spearman")
    ric_s = summarize_ic(ric)
    roll = rolling_positive_share(ric, window=126)
    out["recent_2024plus"] = {
        "rankic": ric_s,
        "rolling_pos_share": roll,
        "gate12a": evaluate_gate12a(ric_s["mean"], roll),
    }
    return out


def main() -> None:
    if not FUND.exists():
        raise SystemExit(f"Missing {FUND}; run fetch_sec_fundamentals first")

    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    fund = pd.read_parquet(FUND)
    feats = build_fundamental_features(close, fund)
    print("Feature coverage:")
    for k, v in feats.items():
        print(f"  {k}: shape={v.shape} nonnull%={(v.notna().mean().mean()*100):.1f}")

    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])
    sector_map = load_sector_map()

    results = {"factors": {}, "survivors": [], "near_misses": []}
    for feat in feats:
        for neut in (False, True):
            key = f"{feat}{'_n' if neut else ''}"
            ev = eval_factor(panel, feat, neut, sector_map)
            results["factors"][key] = ev
            ric = ev["recent_2024plus"]["rankic"]["mean"]
            roll = ev["recent_2024plus"]["rolling_pos_share"]
            g = ev["recent_2024plus"]["gate12a"]["pass"]
            print(
                f"{key:24s} sign={ev['sign']:+.0f} 2024+={ric:+.4f} "
                f"roll={roll:.3f} gate12a={'PASS' if g else 'FAIL'}"
            )
            row = {
                "factor": feat,
                "neutralized": neut,
                "sign": ev["sign"],
                "recent_rankic": ric,
                "rolling_pos": roll,
            }
            if g:
                results["survivors"].append(row)
            elif ric is not None and ric >= 0.01:
                results["near_misses"].append(row)

    results["near_misses"].sort(key=lambda x: x["recent_rankic"], reverse=True)
    results["recommendation"] = (
        "fundamental_gate12a_pass - promote to Gate12 combo"
        if results["survivors"]
        else (
            "fundamental_near_miss - try combo with price near-misses"
            if results["near_misses"]
            else "fundamental_weak - check coverage / concept mapping"
        )
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(f"Survivors={len(results['survivors'])} near_misses={len(results['near_misses'])}")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_A", {})["fundamentals_sec"] = {
        "status": "GATE12A_PASS" if results["survivors"] else "GATE12A_FAIL",
        "survivors": results["survivors"],
        "near_misses": results["near_misses"][:5],
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    st["updated_at"] = "2026-07-30"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
