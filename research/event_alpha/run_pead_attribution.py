# -*- coding: utf-8 -*-
"""PEAD T+20 window-contribution attribution.

Reuses the same accepted event book as run_pead_reality (via pead_book).
Primary gate (MASTER_PROMPT Event Contribution):
  early (hold days 1-5) share of total abnormal return > 50% → EVENT_DRIVEN

Auxiliary:
  - single-symbol OOS contribution >15-20%
  - surprise-quintile monotonicity of abnormal return
  - event-window SPY capture of gross stock return (earnings-beta proxy)

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_attribution
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from research.event_alpha.pead_book import HOLD, MAX_NAMES, select_accepted_events

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
EVENTS = ROOT / "data" / "cache" / "nasdaq_earnings_events.parquet"
OUT = ROOT / "data" / "research" / "event_alpha_pead_attribution.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

# hold-day buckets (1-indexed within [entry, exit))
EARLY = (1, 5)   # sessions 1-5
MID = (6, 10)
LATE = (11, 20)

PERIODS = {
    "train": ("2021-07-01", "2022-12-31"),
    "valid": ("2023-01-01", "2023-12-31"),
    "oos": ("2024-01-01", "2025-12-31"),
    "holdout": ("2026-01-01", None),
    "recent_2024plus": ("2024-01-01", None),
}

EARLY_SHARE_THRESHOLD = 0.50
SINGLE_NAME_WARN = 0.15
SINGLE_NAME_HARD = 0.20


def _bucket_abn(
    close: pd.DataFrame,
    spy_px: pd.Series,
    sym: str,
    entry_i: int,
    exit_i: int,
) -> dict[str, float]:
    """Abnormal (stock - SPY) cumulative return in early/mid/late/total."""
    out = {"early": 0.0, "mid": 0.0, "late": 0.0, "total": 0.0, "stock_total": 0.0, "spy_total": 0.0}
    hold_len = exit_i - entry_i
    if hold_len <= 0 or sym not in close.columns:
        return out

    for day in range(1, hold_len + 1):
        i = entry_i + day - 1
        if i >= len(close.index) or i + 0 >= exit_i:
            break
        # session return from close[i-1]->close[i] is pct_change at i; use price ratio day-by-day
        # We measure hold from entry close to subsequent closes via daily pct.
        r_s = float(close[sym].iloc[i] / close[sym].iloc[i - 1] - 1.0) if i > 0 else 0.0
        r_m = float(spy_px.iloc[i] / spy_px.iloc[i - 1] - 1.0) if i > 0 else 0.0
        if not np.isfinite(r_s):
            r_s = 0.0
        if not np.isfinite(r_m):
            r_m = 0.0
        abn = r_s - r_m
        out["stock_total"] += r_s
        out["spy_total"] += r_m
        out["total"] += abn
        if EARLY[0] <= day <= EARLY[1]:
            out["early"] += abn
        elif MID[0] <= day <= MID[1]:
            out["mid"] += abn
        elif LATE[0] <= day <= LATE[1]:
            out["late"] += abn
    return out


def event_window_table(close: pd.DataFrame, acc: pd.DataFrame) -> pd.DataFrame:
    spy_px = close["SPY"] if "SPY" in close.columns else close.mean(axis=1)
    rows = []
    for _, r in acc.iterrows():
        b = _bucket_abn(close, spy_px, r["symbol"], int(r["entry_i"]), int(r["exit_i"]))
        rows.append(
            {
                "symbol": r["symbol"],
                "event_date": pd.Timestamp(r["event_date"]),
                "surprise": float(r["surprise"]),
                "score": float(r["score"]) if "score" in r and pd.notna(r["score"]) else float(r["surprise"]),
                "early_abn": b["early"],
                "mid_abn": b["mid"],
                "late_abn": b["late"],
                "total_abn": b["total"],
                "stock_total": b["stock_total"],
                "spy_total": b["spy_total"],
            }
        )
    return pd.DataFrame(rows)


def _period_mask(dates: pd.Series, start: str, end: str | None) -> pd.Series:
    m = dates >= start
    if end is not None:
        m &= dates <= end
    return m


def summarize_window(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n_events": 0}
    se, sm, sl, st = df["early_abn"].sum(), df["mid_abn"].sum(), df["late_abn"].sum(), df["total_abn"].sum()
    # Primary: share of summed abnormal; if total ~0, fall back to sum of |abn| weights
    if abs(st) > 1e-12:
        early_share = float(se / st)
        mid_share = float(sm / st)
        late_share = float(sl / st)
        share_basis = "signed_sum"
    else:
        w = df["total_abn"].abs().sum()
        if w > 1e-12:
            early_share = float((df["early_abn"].abs()).sum() / w)  # not ideal; use abs buckets of early over abs total
            # better: contribution via abs of each bucket sum
            early_share = float(abs(se) / (abs(se) + abs(sm) + abs(sl) + 1e-12))
            mid_share = float(abs(sm) / (abs(se) + abs(sm) + abs(sl) + 1e-12))
            late_share = float(abs(sl) / (abs(se) + abs(sm) + abs(sl) + 1e-12))
            share_basis = "abs_bucket_sum_fallback"
        else:
            early_share = mid_share = late_share = float("nan")
            share_basis = "undefined"

    # mean path (equal-weight events) for dispersion check
    mean_early = float(df["early_abn"].mean())
    mean_mid = float(df["mid_abn"].mean())
    mean_late = float(df["late_abn"].mean())
    mean_total = float(df["total_abn"].mean())

    stock_sum = float(df["stock_total"].sum())
    spy_sum = float(df["spy_total"].sum())
    spy_capture = float(spy_sum / stock_sum) if abs(stock_sum) > 1e-12 else float("nan")

    return {
        "n_events": int(len(df)),
        "sum_early_abn": float(se),
        "sum_mid_abn": float(sm),
        "sum_late_abn": float(sl),
        "sum_total_abn": float(st),
        "early_share": early_share,
        "mid_share": mid_share,
        "late_share": late_share,
        "share_basis": share_basis,
        "mean_early_abn": mean_early,
        "mean_mid_abn": mean_mid,
        "mean_late_abn": mean_late,
        "mean_total_abn": mean_total,
        "late_has_substance": bool(mean_late > 0 and abs(mean_late) >= 0.25 * abs(mean_total) if mean_total != 0 else mean_late > 0),
        "spy_capture_of_stock": spy_capture,
        "event_driven_flag": bool(early_share == early_share and early_share > EARLY_SHARE_THRESHOLD),
    }


def single_name_contrib(df: pd.DataFrame) -> dict:
    if df.empty or abs(df["total_abn"].sum()) < 1e-12:
        return {"top_symbol": None, "top_share": None, "warn_15pct": False, "hard_20pct": False, "top5": []}
    tot = df["total_abn"].sum()
    by = df.groupby("symbol")["total_abn"].sum().sort_values(ascending=False)
    shares = (by / tot).astype(float)
    top = shares.head(5)
    top_share = float(shares.iloc[0])
    return {
        "top_symbol": str(shares.index[0]),
        "top_share": top_share,
        "warn_15pct": top_share > SINGLE_NAME_WARN,
        "hard_20pct": top_share > SINGLE_NAME_HARD,
        "top5": [{"symbol": str(k), "share": float(v)} for k, v in top.items()],
    }


def surprise_quintile_monotonicity(df: pd.DataFrame) -> dict:
    """Within accepted book, rank by surprise into 5 bins; mean total_abn."""
    if len(df) < 25:
        return {"n": int(len(df)), "monotonic_increasing": None, "mean_abn_by_q": {}}
    try:
        q = pd.qcut(df["surprise"], 5, labels=[1, 2, 3, 4, 5], duplicates="drop")
    except ValueError:
        return {"n": int(len(df)), "monotonic_increasing": None, "mean_abn_by_q": {}, "note": "qcut_failed"}
    means = df.groupby(q, observed=False)["total_abn"].mean()
    vals = [float(means.loc[i]) for i in means.index]
    mono = all(vals[i] <= vals[i + 1] + 1e-12 for i in range(len(vals) - 1)) if len(vals) >= 2 else None
    return {
        "n": int(len(df)),
        "monotonic_increasing": mono,
        "mean_abn_by_q": {str(int(k)): float(v) for k, v in means.items()},
        "spread_q5_minus_q1": float(vals[-1] - vals[0]) if len(vals) >= 2 else None,
    }


def verdict(oos: dict, oos_names: dict, oos_mono: dict) -> dict:
    """
    FAIL_AS_ALPHA: early burst dominates (>50%) — EVENT_DRIVEN label, not continuous alpha
    PASS_AS_EVENT: early >50% but keep as labeled event module (same as FAIL_AS_ALPHA path for live)
    KEEP_DIGGING: early <=50% and late has substance
    """
    early = oos.get("early_share")
    event_driven = bool(oos.get("event_driven_flag"))
    late_ok = bool(oos.get("late_has_substance"))

    if early is None or (isinstance(early, float) and early != early):
        label = "KEEP_DIGGING"
        reason = "early_share undefined; need more diagnostics"
    elif event_driven:
        label = "PASS_AS_EVENT"
        reason = (
            f"early_share={early:.3f} > {EARLY_SHARE_THRESHOLD} = EVENT_DRIVEN "
            "(MASTER_PROMPT); may keep as event module, not continuous CS alpha; LIVE still LOCKED"
        )
    elif late_ok:
        label = "KEEP_DIGGING"
        reason = (
            f"early_share={early:.3f} <= {EARLY_SHARE_THRESHOLD} and late window has substance; "
            "continue sector/capacity before any live path"
        )
    else:
        label = "FAIL_AS_ALPHA"
        reason = (
            f"early_share={early:.3f} <= {EARLY_SHARE_THRESHOLD} but late contribution weak; "
            "signal looks thin outside narrow post-earnings window"
        )

    # concentration can force fail-as-alpha for live candidacy
    if oos_names.get("hard_20pct"):
        reason += f"; single-name OOS contrib {oos_names.get('top_symbol')}={oos_names.get('top_share'):.1%} >20%"

    return {
        "label": label,
        "event_driven": event_driven,
        "reason": reason,
        "live": "LOCKED",
        "surprise_monotonic_oos": oos_mono.get("monotonic_increasing"),
    }


def main() -> None:
    close = pd.read_parquet(CACHE)
    events = pd.read_parquet(EVENTS)
    events["event_date"] = pd.to_datetime(events["event_date"])

    acc, sign, tr_ic = select_accepted_events(close, events, hold=HOLD, max_names=MAX_NAMES)
    print(f"accepted={len(acc)} sign={sign:+.0f} train_ic={tr_ic:.4f}")

    table = event_window_table(close, acc)
    periods_out = {}
    for name, (start, end) in PERIODS.items():
        sub = table[_period_mask(table["event_date"], start, end)]
        win = summarize_window(sub)
        names = single_name_contrib(sub)
        mono = surprise_quintile_monotonicity(sub)
        periods_out[name] = {
            "window": win,
            "single_name": names,
            "surprise_quintiles": mono,
        }
        es = win.get("early_share")
        es_s = f"{es:.3f}" if isinstance(es, float) and es == es else "nan"
        print(f"  {name}: n={win.get('n_events')} early_share={es_s} event_driven={win.get('event_driven_flag')}")

    oos = periods_out["oos"]["window"]
    oos_names = periods_out["oos"]["single_name"]
    oos_mono = periods_out["oos"]["surprise_quintiles"]
    v = verdict(oos, oos_names, oos_mono)

    results = {
        "meta": {
            "hold": HOLD,
            "max_names": MAX_NAMES,
            "early_days": list(EARLY),
            "mid_days": list(MID),
            "late_days": list(LATE),
            "early_share_threshold": EARLY_SHARE_THRESHOLD,
            "cost_note": "window attribution on gross abnormal returns (pre-cost); same event book as reality @10bps",
            "n_accepted_events": int(len(acc)),
            "sign": sign,
            "train_ic": tr_ic,
        },
        "periods": periods_out,
        "attribution_gate": {
            "primary": "early_share_of_total_abn",
            "threshold": EARLY_SHARE_THRESHOLD,
            "oos_early_share": oos.get("early_share"),
            "oos_event_driven": oos.get("event_driven_flag"),
            "pass_as_continuous_alpha": False,  # never unlock continuous alpha from PEAD here
            "verdict": v,
        },
        "recommendation": v["label"] + " - " + v["reason"],
    }

    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    print(results["recommendation"])
    print(f"Saved {OUT}")

    st = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    st["updated_at"] = "2026-08-03"
    st["phase"] = "phase12.4_pead_attribution"
    pead = st.setdefault("Track_B", {}).setdefault("PEAD", {})
    pead["status"] = v["label"]
    pead["attribution"] = {
        "oos_early_share": oos.get("early_share"),
        "oos_mid_share": oos.get("mid_share"),
        "oos_late_share": oos.get("late_share"),
        "event_driven": oos.get("event_driven_flag"),
        "verdict": v["label"],
        "reason": v["reason"],
        "artifact": "data/research/event_alpha_pead_attribution.json",
    }
    pead["live"] = False
    st.setdefault("strategies", {}).setdefault("pead_surprise_t20", {})["status"] = (
        "event_driven_labeled" if v["label"] == "PASS_AS_EVENT" else (
            "event_keep_digging" if v["label"] == "KEEP_DIGGING" else "event_fail_as_alpha"
        )
    )
    st["strategies"]["pead_surprise_t20"]["live"] = False
    nxt = st.setdefault("system", {}).setdefault("next", [])
    # refresh next steps
    st["system"]["next"] = [
        "PEAD: " + v["label"],
        "CS combo residualize vs SPY+200MA / earnings_yield (archive confirm)",
        "Do not unlock LIVE",
        "No ML yet",
    ]
    st["system"]["live"] = "LOCKED"
    st["system"]["alpha"] = "NOT_FOUND"
    st["live"] = "LOCKED"
    st["live_candidates"] = {
        "count": 0,
        "note": f"PEAD attribution={v['label']}; CS Gate14 fail; no live candidates",
    }
    STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Updated {STATUS}")


if __name__ == "__main__":
    main()
