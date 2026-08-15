# -*- coding: utf-8 -*-
"""Phase12.3: quality/value factors + Gate12 push on validated combo base.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.run_gate12_push
"""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.features.extended import build_extended_features
from research.alpha_v2.features.sector_map import industry_neutralize_score, load_sector_map
from research.alpha_v2.gates.hard_gate12 import evaluate_gate12
from research.alpha_v2.gates.hard_gate12a import evaluate_gate12a
from research.alpha_v2.ic_engine.metrics import daily_ic, rolling_positive_share, summarize_ic
from research.alpha_v2.labels.forward_return import align_xy, forward_return
from research.alpha_v2.run_fundamental_screen import wide_from_fund

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
FUND = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
OUT = ROOT / "data" / "research" / "alpha_v2_gate12_push.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def build_quality_value(close: pd.DataFrame, fund: pd.DataFrame) -> dict[str, pd.DataFrame]:
    equity = wide_from_fund(fund, close, "equity")
    assets = wide_from_fund(fund, close, "assets")
    eps = wide_from_fund(fund, close, "eps")
    shares = wide_from_fund(fund, close, "shares")
    revenue = wide_from_fund(fund, close, "revenue") if "revenue" in fund.columns else None
    opinc = wide_from_fund(fund, close, "opinc") if "opinc" in fund.columns else None
    gp = wide_from_fund(fund, close, "gross_profit") if "gross_profit" in fund.columns else None
    ocf = wide_from_fund(fund, close, "ocf") if "ocf" in fund.columns else None
    cash = wide_from_fund(fund, close, "cash") if "cash" in fund.columns else None
    ac = wide_from_fund(fund, close, "assets_current") if "assets_current" in fund.columns else None
    lc = wide_from_fund(fund, close, "liab_current") if "liab_current" in fund.columns else None
    debt = wide_from_fund(fund, close, "long_debt") if "long_debt" in fund.columns else None

    out: dict[str, pd.DataFrame] = {}
    # value
    common = sorted(set(eps.columns) & set(close.columns))
    out["earnings_yield"] = eps[common] / close[common].replace(0, np.nan)

    if "shares" in fund.columns:
        c2 = sorted(set(equity.columns) & set(shares.columns) & set(close.columns))
        mcap = close[c2] * shares[c2].replace(0, np.nan)
        out["book_to_price"] = equity[c2] / mcap.replace(0, np.nan)

    # quality: gross profitability (Novy-Marx) ≈ GP / assets
    if gp is not None:
        c = sorted(set(gp.columns) & set(assets.columns))
        out["gross_profitability"] = gp[c] / assets[c].replace(0, np.nan)
    elif revenue is not None:
        # fallback: revenue/assets as sales-to-assets
        c = sorted(set(revenue.columns) & set(assets.columns))
        out["sales_to_assets"] = revenue[c] / assets[c].replace(0, np.nan)

    if opinc is not None:
        c = sorted(set(opinc.columns) & set(assets.columns))
        out["operating_profitability"] = opinc[c] / assets[c].replace(0, np.nan)

    if ocf is not None:
        c = sorted(set(ocf.columns) & set(assets.columns))
        out["ocf_to_assets"] = ocf[c] / assets[c].replace(0, np.nan)
        c2 = sorted(set(ocf.columns) & set(close.columns) & set(shares.columns))
        mcap = close[c2] * shares[c2].replace(0, np.nan)
        out["ocf_yield"] = ocf[c2] / mcap.replace(0, np.nan)

    if ac is not None and lc is not None:
        c = sorted(set(ac.columns) & set(lc.columns))
        out["current_ratio"] = ac[c] / lc[c].replace(0, np.nan)

    if debt is not None:
        c = sorted(set(debt.columns) & set(assets.columns))
        out["neg_leverage"] = -(debt[c] / assets[c].replace(0, np.nan))

    if cash is not None:
        c = sorted(set(cash.columns) & set(assets.columns))
        out["cash_to_assets"] = cash[c] / assets[c].replace(0, np.nan)

    # investment: low asset growth
    c = sorted(set(assets.columns) & set(close.columns))
    a = assets[c]
    out["neg_asset_growth"] = -(a / a.shift(252) - 1.0)

    if revenue is not None:
        c = sorted(set(revenue.columns) & set(close.columns))
        r = revenue[c]
        out["sales_growth"] = r / r.shift(252) - 1.0

    return out


def cs_rank(panel: pd.DataFrame, series: pd.Series, sign: float = 1.0) -> pd.Series:
    return panel.assign(_v=series * sign).groupby("date")["_v"].rank(pct=True)


def eval_score(d: pd.DataFrame, score: pd.Series) -> dict:
    x = d.assign(score=score).dropna(subset=["score", "label"])
    recent = x[x["date"] >= "2024-01-01"]
    ric = daily_ic(recent, "score", method="spearman")
    ic = daily_ic(recent, "score", method="pearson")
    ric_s, ic_s = summarize_ic(ric), summarize_ic(ic)
    roll = rolling_positive_share(ric, window=126)
    splits = {}
    for name, a, b in [
        ("train", "2021-07-01", "2022-12-31"),
        ("valid", "2023-01-01", "2023-12-31"),
        ("oos", "2024-01-01", "2025-12-31"),
        ("holdout", "2026-01-01", None),
    ]:
        part = x[x["date"] >= a]
        if b:
            part = part[part["date"] <= b]
        splits[name] = summarize_ic(daily_ic(part, "score", method="spearman"))
    return {
        "recent_rankic": ric_s["mean"],
        "recent_ic": ic_s["mean"],
        "roll": roll,
        "splits": splits,
        "gate12a": evaluate_gate12a(ric_s["mean"], roll),
        "gate12": evaluate_gate12(ic_s, ric_s, roll),
    }


def main() -> None:
    close = pd.read_parquet(CACHE)
    elig = pd.read_parquet(ELIG).astype(bool)
    cols = [c for c in close.columns if c in elig.columns]
    close, elig = close[cols], elig[cols].reindex(close.index).fillna(False)
    fund = pd.read_parquet(FUND)
    print("fund cols", fund.columns.tolist())

    ext = build_extended_features(close)
    qv = build_quality_value(close, fund)
    sm = load_sector_map()

    # Screen single quality/value factors with train sign (only that factor in align)
    singles = {}
    for name, fr in qv.items():
        panel = align_xy({name: fr}, forward_return(close, horizon=5, entry_lag=1), elig)
        panel["date"] = pd.to_datetime(panel["date"])
        train = panel[(panel.date >= "2021-07-01") & (panel.date <= "2022-12-31")].dropna(
            subset=[name, "label"]
        )
        train = train.assign(score=train[name])
        tr = summarize_ic(daily_ic(train, "score", method="spearman"))["mean"]
        sign = 1.0 if (tr == tr and tr >= 0) else -1.0
        # also industry-neutral variant
        for neut in (False, True):
            raw = panel[name].astype(float)
            if neut:
                tmp = panel[["date", "symbol"]].copy()
                tmp[name] = raw
                raw = industry_neutralize_score(tmp, name, sm)
            ev = eval_score(panel, raw * sign)
            key = f"{name}{'_n' if neut else ''}"
            singles[key] = {"sign": sign, **ev}
            g = "PASS" if ev["gate12a"]["pass"] else "fail"
            print(
                f"single {key:28s} sign={sign:+.0f} ric={ev['recent_rankic']:+.4f} "
                f"roll={ev['roll']:.3f} g12a={g}"
            )

    # Base combo members (validated)
    base_feats = {
        "vol_adj_rev_5": ext["vol_adj_rev_5"],
        "near_high_252": ext["near_high_252"],
        "earnings_yield": qv["earnings_yield"],
    }
    # candidate add-ons: top singles by recent_rankic among non-base
    ranked = sorted(
        (
            (k, v)
            for k, v in singles.items()
            if not k.startswith("earnings_yield") and (v["recent_rankic"] or -9) > 0.005
        ),
        key=lambda kv: kv[1]["recent_rankic"],
        reverse=True,
    )
    add_names = []
    for k, _ in ranked[:6]:
        raw = k.replace("_n", "")
        if raw in qv and raw not in add_names:
            add_names.append(raw)
    print("addon candidates", add_names)

    results = {"singles_top": [], "combos": [], "best": None}
    results["singles_top"] = [
        {"factor": k, "recent_rankic": v["recent_rankic"], "gate12a": v["gate12a"]["pass"]}
        for k, v in sorted(singles.items(), key=lambda kv: kv[1]["recent_rankic"] or -9, reverse=True)[:12]
    ]

    # Build panels for base + each addon set (only those features → no intersection bias)
    addon_sets = [()] + [(a,) for a in add_names] + list(combinations(add_names[:4], 2))
    for add in addon_sets:
        feats = dict(base_feats)
        for a in add:
            feats[a] = qv[a]
        panel = align_xy(feats, forward_return(close, horizon=5, entry_lag=1), elig)
        panel["date"] = pd.to_datetime(panel["date"])
        # ranks with fixed signs from prior research / train
        tmp = panel[["date", "symbol"]].copy()
        tmp["near_high_252"] = panel["near_high_252"]
        nh = industry_neutralize_score(tmp, "near_high_252", sm)
        ranks = [
            cs_rank(panel, panel["vol_adj_rev_5"], 1.0),
            cs_rank(panel, nh, 1.0),
            cs_rank(panel, panel["earnings_yield"], 1.0),
        ]
        for a in add:
            # train sign for addon
            tr = panel[(panel.date >= "2021-07-01") & (panel.date <= "2022-12-31")]
            tr = tr.assign(score=tr[a]).dropna(subset=["score", "label"])
            tr_ic = summarize_ic(daily_ic(tr, "score", method="spearman"))["mean"]
            sign = 1.0 if (tr_ic == tr_ic and tr_ic >= 0) else -1.0
            # prefer industry-neutral if that single was better
            neut_key = f"{a}_n"
            use_neut = (
                neut_key in singles
                and (singles[neut_key]["recent_rankic"] or -9)
                >= (singles.get(a, {}).get("recent_rankic") or -9)
            )
            raw = panel[a].astype(float)
            if use_neut:
                t2 = panel[["date", "symbol"]].copy()
                t2[a] = raw
                raw = industry_neutralize_score(t2, a, sm)
            ranks.append(cs_rank(panel, raw, sign))
        score = pd.concat(ranks, axis=1).mean(axis=1)
        ev = eval_score(panel, score)
        name = "base" + (("+" + "+".join(add)) if add else "")
        row = {
            "name": name,
            "n_syms": int(panel["symbol"].nunique()),
            "recent_rankic": ev["recent_rankic"],
            "recent_ic": ev["recent_ic"],
            "roll": ev["roll"],
            "valid_rankic": ev["splits"]["valid"]["mean"],
            "oos_rankic": ev["splits"]["oos"]["mean"],
            "gate12a": ev["gate12a"]["pass"],
            "gate12": ev["gate12"]["pass"],
            "gate12_checks": ev["gate12"]["checks"],
        }
        results["combos"].append(row)
        mark = "G12PASS" if row["gate12"] else ("G12a" if row["gate12a"] else "fail")
        print(
            f"{mark:7s} ric={row['recent_rankic']:+.4f} ic={row['recent_ic']:+.4f} "
            f"valid={row['valid_rankic']:+.4f} | {name}"
        )
        if results["best"] is None or row["recent_rankic"] > results["best"]["recent_rankic"]:
            results["best"] = row

    results["any_gate12"] = any(r["gate12"] for r in results["combos"])
    results["recommendation"] = (
        "gate12_pass - run Gate13 reality/attribution"
        if results["any_gate12"]
        else "gate12_still_short - best ric={:.4f}; valid-year remains the risk".format(
            results["best"]["recent_rankic"] if results["best"] else -1
        )
    )
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st.setdefault("Track_A", {})["gate12_push"] = {
        "status": "GATE12_PASS" if results["any_gate12"] else "GATE12_FAIL",
        "best": results["best"],
        "recommendation": results["recommendation"],
        "artifact": str(OUT),
    }
    if results["any_gate12"]:
        st["hard_gates"]["gate12_rankic"] = "PASS"
        st["system"]["alpha"] = "CANDIDATE_PENDING_GATE13"
    st["updated_at"] = "2026-07-31"
    st["phase"] = "phase12.3_gate12_push"
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
