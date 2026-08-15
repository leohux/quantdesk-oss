# -*- coding: utf-8 -*-
"""Phase 12.6: residualize quality / idio factors vs earnings_yield.

Track A combo died because it was EY + market timing. This asks the leftover
question: after stripping EY in the cross-section, does any quality, accrual,
investment, or idiosyncratic-price score still predict T+5 rank?

Sign is fit on TRAIN RankIC only. Gate12-A on 2024+.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_residual_ey_screen
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
from research.alpha_v2.run_fundamental_screen import wide_from_fund
from research.alpha_v2.run_gate12_push import build_quality_value

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_residual_ey_screen.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

SPLITS = {
    "train": ("2021-07-01", "2022-12-31"),
    "valid": ("2023-01-01", "2023-12-31"),
    "oos": ("2024-01-01", "2025-12-31"),
    "holdout": ("2026-01-01", None),
}

PRICE_FEATS = (
    "vol_adj_rev_5",
    "resid_mom_20",
    "resid_mom_60",
    "near_high_252",
    "idiovol_20",
    "lottery_20",
)
FUND_FEATS = (
    "gross_profitability",
    "operating_profitability",
    "ocf_yield",
    "ocf_to_assets",
    "neg_leverage",
    "neg_asset_growth",
    "cash_to_assets",
    "book_to_price",
    "accruals",
    "ocf_minus_ni",
    "ey_chg_63",
)


def _slice(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    m = df["date"] >= pd.Timestamp(start)
    if end:
        m &= df["date"] <= pd.Timestamp(end)
    return df.loc[m]


def build_extra_fund(close: pd.DataFrame, fund: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Sloan accruals + cash-earnings gap + 63d EY change (revision/cheapness)."""
    extra: dict[str, pd.DataFrame] = {}
    if not {"ni", "ocf", "assets"}.issubset(fund.columns):
        return extra
    ni = wide_from_fund(fund, close, "ni")
    ocf = wide_from_fund(fund, close, "ocf")
    assets = wide_from_fund(fund, close, "assets")
    c = sorted(set(ni.columns) & set(ocf.columns) & set(assets.columns))
    den = assets[c].replace(0, np.nan)
    extra["accruals"] = (ni[c] - ocf[c]) / den
    extra["ocf_minus_ni"] = (ocf[c] - ni[c]) / den
    if "eps" in fund.columns:
        eps = wide_from_fund(fund, close, "eps")
        c2 = sorted(set(eps.columns) & set(close.columns))
        ey = eps[c2] / close[c2].replace(0, np.nan)
        extra["ey_chg_63"] = ey - ey.shift(63)
    return extra


def cs_residual(panel: pd.DataFrame, y_col: str, x_col: str) -> pd.Series:
    """Daily OLS residual of y on 1 + x. PIT: both columns are known at t."""
    out = np.full(len(panel), np.nan, dtype=float)
    yv = panel[y_col].to_numpy(dtype=float, copy=False)
    xv = panel[x_col].to_numpy(dtype=float, copy=False)
    dates = panel["date"].to_numpy()
    order = np.argsort(dates, kind="mergesort")
    dates_s = dates[order]
    breaks = np.flatnonzero(dates_s[1:] != dates_s[:-1]) + 1
    spans = np.concatenate([[0], breaks, [len(order)]])
    for a, b in zip(spans[:-1], spans[1:]):
        idx = order[a:b]
        y = yv[idx]
        x = xv[idx]
        m = np.isfinite(y) & np.isfinite(x)
        if int(m.sum()) < 30:
            continue
        yy = y[m]
        xx = x[m]
        X = np.column_stack([np.ones(len(xx)), xx])
        beta, *_ = np.linalg.lstsq(X, yy, rcond=None)
        resid = np.full(len(idx), np.nan)
        resid[m] = yy - X @ beta
        out[idx] = resid
    return pd.Series(out, index=panel.index)


def eval_signed(panel: pd.DataFrame, score: pd.Series) -> dict:
    d = panel[["date", "symbol", "label"]].copy()
    d["score"] = score
    d = d.dropna(subset=["score", "label"])
    train = _slice(d, *SPLITS["train"])
    tr = summarize_ic(daily_ic(train, "score", method="spearman"))["mean"]
    sign = 1.0 if (tr == tr and tr >= 0) else -1.0
    d["score"] = d["score"] * sign
    out = {"train_rankic_raw": tr, "sign": sign, "splits": {}}
    for name, bounds in SPLITS.items():
        part = _slice(d, *bounds)
        if len(part) < 300:
            out["splits"][name] = {"note": "too_few", "n": int(len(part))}
            continue
        out["splits"][name] = summarize_ic(daily_ic(part, "score", method="spearman"))
    recent = d[d["date"] >= pd.Timestamp("2024-01-01")]
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
    print("Phase12.6 residual-vs-EY screen...")
    close = pd.read_parquet(CACHE)
    eligible = pd.read_parquet(ELIG).astype(bool) if ELIG.exists() else None
    if eligible is not None:
        cols = [c for c in close.columns if c in eligible.columns]
        close = close[cols]
        eligible = eligible[cols].reindex(close.index).fillna(False)

    fund = pd.read_parquet(FUND)
    feats = {}
    feats.update(build_extended_features(close))
    feats.update(build_quality_value(close, fund))
    feats.update(build_extra_fund(close, fund))
    if "earnings_yield" not in feats:
        raise SystemExit("earnings_yield missing — cannot residualize")

    keep = ["earnings_yield", *PRICE_FEATS, *FUND_FEATS]
    feats = {k: v for k, v in feats.items() if k in keep}
    print("coverage:")
    for k, v in feats.items():
        print(f"  {k:28s} nonnull%={(v.notna().mean().mean() * 100):.1f}")

    label = forward_return(close, horizon=5, entry_lag=1)
    panel = align_xy(feats, label, eligible)
    panel["date"] = pd.to_datetime(panel["date"])
    sector_map = load_sector_map()

    results = {
        "hypothesis": (
            "After CS-OLS residualizing vs earnings_yield (optionally after "
            "industry neutralization), leftover quality/accrual/idio scores "
            "have T+5 RankIC."
        ),
        "factors": {},
        "survivors": [],
        "near_misses": [],
    }

    variants = (
        ("resid_ey", False, True),
        ("indneut_resid_ey", True, True),
    )
    targets = [c for c in (*PRICE_FEATS, *FUND_FEATS) if c in panel.columns]
    for feat in targets:
        for vname, neut, resid in variants:
            key = f"{feat}__{vname}"
            raw = panel[feat].astype(float)
            if neut:
                tmp = panel[["date", "symbol"]].copy()
                tmp[feat] = raw
                raw = industry_neutralize_score(tmp, feat, sector_map)
            work = panel[["date", "symbol", "label", "earnings_yield"]].copy()
            work["_y"] = raw
            if resid:
                ey = panel["earnings_yield"].astype(float)
                if neut:
                    tmp = panel[["date", "symbol"]].copy()
                    tmp["earnings_yield"] = ey
                    ey = industry_neutralize_score(tmp, "earnings_yield", sector_map)
                work["_x"] = ey
                score = cs_residual(work, "_y", "_x")
            else:
                score = raw
            ev = eval_signed(work, score)
            results["factors"][key] = ev
            ric = ev["recent_2024plus"]["rankic"]["mean"]
            roll = ev["recent_2024plus"]["rolling_pos_share"]
            g = ev["recent_2024plus"]["gate12a"]["pass"]
            print(
                f"{key:42s} sign={ev['sign']:+.0f} 2024+={ric:+.4f} "
                f"roll={roll:.3f} gate12a={'PASS' if g else 'FAIL'}",
                flush=True,
            )
            row = {
                "factor": key,
                "base": feat,
                "variant": vname,
                "sign": ev["sign"],
                "recent_rankic": ric,
                "rolling_pos": roll,
            }
            if g:
                results["survivors"].append(row)
            elif ric is not None and ric == ric and ric >= 0.01:
                results["near_misses"].append(row)

    results["near_misses"].sort(key=lambda x: x["recent_rankic"], reverse=True)
    results["survivors"].sort(key=lambda x: x["recent_rankic"], reverse=True)

    # Gate12-A on 2024+ includes 2026 holdout. Require OOS 2024-2025 as well.
    honest = []
    inflated = []
    for row in results["survivors"]:
        ev = results["factors"][row["factor"]]
        oos = (ev.get("splits") or {}).get("oos") or {}
        valid = (ev.get("splits") or {}).get("valid") or {}
        oos_ric = oos.get("mean")
        val_ric = valid.get("mean")
        row = dict(row)
        row["oos_rankic"] = oos_ric
        row["valid_rankic"] = val_ric
        if oos_ric is not None and oos_ric > 0.02 and (val_ric is None or val_ric > 0):
            honest.append(row)
        else:
            inflated.append(row)
    results["survivors"] = honest
    results["holdout_inflated"] = inflated
    results["verdict"] = (
        "GATE12A_PASS"
        if honest
        else (
            "NO_INDEPENDENT_ALPHA_AFTER_EY_holdout_inflated"
            if inflated
            else "NO_INDEPENDENT_ALPHA_AFTER_EY"
        )
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(
        f"Survivors={len(results['survivors'])} near_misses={len(results['near_misses'])} "
        f"verdict={results['verdict']}"
    )
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st["updated_at"] = "2026-08-13"
    st["phase"] = "phase12.6_residual_ey"
    st["live"] = "LOCKED"
    st.setdefault("Track_A", {})["residual_ey_screen"] = {
        "status": results["verdict"],
        "survivors": results["survivors"],
        "near_misses": results["near_misses"][:8],
        "artifact": str(OUT).replace("\\", "/"),
    }
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
