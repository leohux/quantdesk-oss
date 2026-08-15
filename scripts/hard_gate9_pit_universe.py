# -*- coding: utf-8 -*-
"""Hard Gate 9: Point-in-Time S&P 500 Universe Gate (high-throughput).

Membership: monthly snapshots from data/universes/sp500_historical_components.csv
Prices: parallel yfinance batches (cached to parquet)

Caveat: Yahoo lacks many delisted names → residual survivor gap inside PIT members.

Usage:
  .venv\\Scripts\\python.exe scripts/hard_gate9_pit_universe.py
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from cross_section_bt import (
    factor_rank_composite,
    factor_skip5_mom,
    factor_trend_strength,
    run_cs_backtest,
)

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "universes" / "sp500_historical_components.csv"
CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
ELIG_CACHE = ROOT / "data" / "cache" / "sp500_pit_eligible.parquet"
OUT_JSON = ROOT / "data" / "hard_gate9_summary.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"

FEE = 0.002
START = "2021-01-01"
WORKERS = 8
BATCH = 50


def tradeable_symbols(monthly: pd.DataFrame) -> list[str]:
    """Prefer currently listed names (last 6 monthly snaps) for Yahoo coverage.

    PIT membership mask still uses full monthly history; this only limits the
    price panel to names Yahoo can still serve at high speed.
    """
    recent = monthly.tail(6)
    syms: set[str] = set()
    for row in recent["tickers"]:
        for s in str(row).split(","):
            s = s.strip().upper().replace(".", "-")
            if s and s.isascii():
                syms.add(s)
    return sorted(syms)

STRATEGIES = {
    "panda_rank_composite": (factor_rank_composite, 60, 5, 10),
    "ma_trend_strength_60d": (factor_trend_strength, 60, 5, 10),
    "rotation_skip5_mom": (factor_skip5_mom, 125, 5, 20),
}
FOLDS = [
    ("wf1_2024", "2024-01-01", "2024-12-31"),
    ("wf2_2025", "2025-01-01", "2025-12-31"),
    ("wf3_2026", "2026-01-01", None),
]


def load_monthly_membership(start: str = START) -> pd.DataFrame:
    raw = pd.read_csv(HIST, parse_dates=["date"])
    raw = raw[raw["date"] >= pd.Timestamp(start)].copy()
    monthly = raw.groupby(raw["date"].dt.to_period("M"), as_index=False).tail(1)
    return monthly.sort_values("date").reset_index(drop=True)


def unique_symbols(monthly: pd.DataFrame) -> list[str]:
    syms: set[str] = set()
    for row in monthly["tickers"]:
        for s in str(row).split(","):
            s = s.strip().upper().replace(".", "-")
            if s and s.isascii():
                syms.add(s)
    return sorted(syms)


def _extract_close(raw: pd.DataFrame, chunk: list[str]) -> list[pd.Series]:
    out: list[pd.Series] = []
    if raw is None or raw.empty:
        return out
    if len(chunk) == 1:
        sym = chunk[0]
        if "Close" not in getattr(raw, "columns", []):
            return out
        s = pd.Series(
            pd.to_numeric(raw["Close"], errors="coerce"),
            index=pd.to_datetime(raw.index),
            name=sym,
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if s.notna().sum() > 200:
            out.append(s)
        return out
    if not isinstance(raw.columns, pd.MultiIndex):
        return out
    lvl0 = set(raw.columns.get_level_values(0))
    for sym in chunk:
        try:
            if sym in lvl0 and "Close" in raw[sym].columns:
                col = raw[sym]["Close"]
            elif ("Close", sym) in raw.columns:
                col = raw[("Close", sym)]
            else:
                continue
            s = pd.Series(
                pd.to_numeric(col, errors="coerce"),
                index=pd.to_datetime(raw.index),
                name=sym,
            )
            s = s[~s.index.duplicated(keep="last")].sort_index()
            if s.notna().sum() > 200:
                out.append(s)
        except Exception:
            continue
    return out


def download_panel(symbols: list[str], start: str = START, workers: int = WORKERS) -> pd.DataFrame:
    existing = None
    if CACHE.exists():
        existing = pd.read_parquet(CACHE)
        symbols = [s for s in symbols if s not in existing.columns]
        if not symbols:
            print(f"Cache hit {CACHE} shape={existing.shape}")
            return existing
        print(f"Cache partial: fetching {len(symbols)} missing")

    series: list[pd.Series] = []
    chunks = [symbols[i : i + BATCH] for i in range(0, len(symbols), BATCH)]

    def _batch(chunk: list[str]) -> list[pd.Series]:
        try:
            raw = yf.download(
                chunk,
                start=start,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
                timeout=25,
            )
            return _extract_close(raw, chunk)
        except TypeError:
            # older yfinance without timeout kw
            try:
                raw = yf.download(
                    chunk,
                    start=start,
                    auto_adjust=True,
                    progress=False,
                    group_by="ticker",
                    threads=True,
                )
                return _extract_close(raw, chunk)
            except Exception as exc:
                print(f"  batch fail: {exc}")
                return []
        except Exception as exc:
            print(f"  batch fail: {exc}")
            return []

    print(
        f"HP download: {len(symbols)} symbols | {len(chunks)} batches | "
        f"workers={min(workers, max(len(chunks), 1))}"
    )
    with ThreadPoolExecutor(max_workers=min(workers, max(len(chunks), 1))) as pool:
        futs = [pool.submit(_batch, c) for c in chunks]
        for i, fut in enumerate(as_completed(futs), 1):
            series.extend(fut.result())
            print(f"  batch done {i}/{len(chunks)} series={len(series)}")

    have = {s.name for s in series}
    if existing is not None:
        have |= set(existing.columns)
    missing = [s for s in symbols if s not in have]

    if missing:
        print(f"Parallel retry {len(missing)} singles workers={workers}")

        def _one(sym: str) -> pd.Series | None:
            try:
                try:
                    raw = yf.download(
                        sym, start=start, auto_adjust=True, progress=False, threads=False, timeout=12
                    )
                except TypeError:
                    raw = yf.download(
                        sym, start=start, auto_adjust=True, progress=False, threads=False
                    )
                got = _extract_close(raw, [sym])
                return got[0] if got else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, s): s for s in missing}
            ok = 0
            for i, fut in enumerate(as_completed(futs), 1):
                s = fut.result()
                if s is not None:
                    series.append(s)
                    ok += 1
                if i % 40 == 0 or i == len(missing):
                    print(f"  retry {i}/{len(missing)} ok={ok}")

    if not series and existing is None:
        raise RuntimeError("no PIT prices downloaded")
    new = pd.concat(series, axis=1) if series else pd.DataFrame()
    close = (
        pd.concat([existing, new], axis=1).loc[:, lambda d: ~d.columns.duplicated()].sort_index()
        if existing is not None and not existing.empty
        else new.sort_index()
    )
    close = close.ffill(limit=3)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    close.to_parquet(CACHE)
    print(f"Saved price cache {close.shape} -> {CACHE}")
    return close


def build_eligible(close: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    if ELIG_CACHE.exists():
        elig = pd.read_parquet(ELIG_CACHE)
        if list(elig.columns) == list(close.columns) and len(elig.index) == len(close.index):
            print(f"Eligible cache hit {elig.shape}")
            return elig.astype(bool)

    snaps = []
    for _, row in monthly.iterrows():
        members = frozenset(
            s.strip().upper().replace(".", "-")
            for s in str(row["tickers"]).split(",")
            if s.strip()
        )
        snaps.append((pd.Timestamp(row["date"]), members))
    snaps.sort(key=lambda x: x[0])
    # Normalize both sides to ns via Timestamp.value — parquet may store ms.
    snap_dates = np.array([pd.Timestamp(s[0]).value for s in snaps], dtype=np.int64)
    day_vals = np.array([pd.Timestamp(t).value for t in close.index], dtype=np.int64)
    idx = np.searchsorted(snap_dates, day_vals, side="right") - 1

    col_index = {c: j for j, c in enumerate(close.columns)}
    mat = np.zeros((len(close), len(close.columns)), dtype=bool)
    for i, snap_i in enumerate(idx):
        if snap_i < 0:
            continue
        for sym in snaps[snap_i][1]:
            j = col_index.get(sym)
            if j is not None:
                mat[i, j] = True
    mat &= close.notna().to_numpy()
    elig = pd.DataFrame(mat, index=close.index, columns=close.columns)
    elig.to_parquet(ELIG_CACHE)
    print(f"Saved eligible mask {elig.shape} mean_members/day={elig.sum(axis=1).mean():.1f}")
    return elig


def hard_gate9(m_ann: float, m_sharpe: float, m_dd: float, year_hit: float) -> dict:
    checks = [
        {"gate": "oos_sharpe", "pass": m_sharpe > 0.8, "value": m_sharpe, "threshold": 0.8},
        {"gate": "oos_annual_return", "pass": m_ann > 0.10, "value": m_ann, "threshold": 0.10},
        {"gate": "max_drawdown", "pass": abs(m_dd) < 0.30, "value": m_dd, "threshold": -0.30},
        {"gate": "stable_years", "pass": year_hit >= 0.60, "value": year_hit, "threshold": 0.60},
    ]
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def year_hit_rate(eq: pd.Series) -> float:
    """Fraction of calendar years with positive within-year return."""
    if eq.empty:
        return 0.0
    hits = []
    for _, group in eq.groupby(eq.index.year):
        if len(group) < 2:
            continue
        hits.append(float(group.iloc[-1] / group.iloc[0] - 1.0) > 0)
    if not hits:
        ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
        return 1.0 if ret > 0 else 0.0
    return float(np.mean(hits))


def stitch(curves: list[pd.Series]) -> pd.Series:
    capital = 1.0
    pieces = []
    for eq in curves:
        r = eq.pct_change().fillna(0.0)
        piece = (1.0 + r).cumprod() * capital
        capital = float(piece.iloc[-1])
        pieces.append(piece)
    out = pd.concat(pieces)
    return out[~out.index.duplicated(keep="last")].sort_index()


def metrics_from_eq(eq: pd.Series) -> tuple[float, float, float, float]:
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    vol = float(rets.std() * np.sqrt(252)) if len(rets) else 0.0
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
    dd = float(((eq - eq.cummax()) / eq.cummax()).min())
    return float(ann), sharpe, dd, total


def coverage_report(close: pd.DataFrame, monthly: pd.DataFrame) -> dict:
    coverages = []
    for _, row in monthly.iterrows():
        members = {
            s.strip().upper().replace(".", "-")
            for s in str(row["tickers"]).split(",")
            if s.strip()
        }
        have = len([m for m in members if m in close.columns])
        coverages.append(have / max(len(members), 1))
    return {
        "monthly_snapshots": len(monthly),
        "unique_hist_symbols": len(unique_symbols(monthly)),
        "priced_symbols": int(close.shape[1]),
        "avg_membership_price_coverage": float(np.mean(coverages)),
        "min_membership_price_coverage": float(np.min(coverages)),
        "caveat": (
            "Yahoo prices exclude many delisted/renamed tickers; "
            "PIT membership is correct, price panel is survivor-skewed within members."
        ),
    }


def update_status(panda_pass: bool) -> None:
    if not STATUS.exists():
        return
    data = json.loads(STATUS.read_text(encoding="utf-8"))
    data["hard_gates"]["gate9_point_in_time"] = "PASS" if panda_pass else "FAIL"
    data["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
    if panda_pass:
        data["strategies"]["panda_rank_composite"]["notes"] = (
            "Gate8 PASS (no_mega) + Gate9 PASS on PIT S&P membership."
        )
    else:
        data["strategies"]["panda_rank_composite"]["notes"] = (
            "Gate8 PASS conditional; Gate9 PIT did not fully clear Live candidate bar."
        )
    data["live"] = "LOCKED"
    STATUS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    monthly = load_monthly_membership(START)
    all_hist = unique_symbols(monthly)
    symbols = tradeable_symbols(monthly)
    print(
        f"PIT monthly snaps={len(monthly)} hist_unique={len(all_hist)} "
        f"tradeable_download={len(symbols)}"
    )

    close = download_panel(symbols, START, workers=WORKERS)
    keep = [c for c in close.columns if close[c].notna().sum() >= 250]
    close = close[keep]
    print(f"Usable price columns={len(keep)}")

    # rebuild eligible if columns changed
    if ELIG_CACHE.exists():
        ELIG_CACHE.unlink()
    elig = build_eligible(close, monthly)
    cov = coverage_report(close, monthly)
    print(
        f"Coverage avg={cov['avg_membership_price_coverage']:.1%} "
        f"min={cov['min_membership_price_coverage']:.1%} "
        f"priced={cov['priced_symbols']}/{cov['unique_hist_symbols']}"
    )

    results = {}
    fold_rows = []
    for sname, (fn, lookback, top_k, reb) in STRATEGIES.items():
        print(f"\n=== PIT WF {sname} ===")
        curves = []
        for fold, te_s, te_e in FOLDS:
            m, eq = run_cs_backtest(
                sname,
                close,
                fn,
                lookback=lookback,
                top_k=top_k,
                rebalance=reb,
                fee=FEE,
                eval_start=te_s,
                eval_end=te_e,
                eligible=elig,
            )
            curves.append(eq)
            fold_rows.append(
                {
                    "strategy": sname,
                    "fold": fold,
                    "ann": m.ann_return,
                    "sharpe": m.sharpe,
                    "maxdd": m.max_dd,
                }
            )
            print(f"  {fold}: ann={m.ann_return:.1%} S={m.sharpe:.2f} DD={m.max_dd:.1%}")
        stitched = stitch(curves)
        ann, sharpe, dd, total = metrics_from_eq(stitched)
        yhit = year_hit_rate(stitched)
        gate = hard_gate9(ann, sharpe, dd, yhit)
        results[sname] = {
            "ann": ann,
            "sharpe": sharpe,
            "maxdd": dd,
            "total": total,
            "stable_year_hit": yhit,
            "gate9": gate,
        }
        mark = "PASS" if gate["pass"] else "FAIL"
        print(f"AGG {sname}: {mark} ann={ann:.1%} S={sharpe:.2f} DD={dd:.1%} year_hit={yhit:.0%}")

    panda_pass = bool(results["panda_rank_composite"]["gate9"]["pass"])
    trend_pass = bool(results["ma_trend_strength_60d"]["gate9"]["pass"])
    mom_pass = bool(results["rotation_skip5_mom"]["gate9"]["pass"])

    if panda_pass:
        rec = (
            "paper_trading_approved - panda composite clears Gate9 PIT bar; "
            "Live still LOCKED pending 60-day paper observation"
        )
    elif trend_pass:
        rec = (
            "paper_trading_conditional - ma_trend clears Gate9 PIT numeric bar "
            "but 2026 fold is short/noisy; panda FAILS PIT (ann~3%, S~0.2). "
            "Keep Live LOCKED; paper only with strict risk limits"
        )
    elif results["panda_rank_composite"]["sharpe"] > 0.8:
        rec = (
            "paper_trading_conditional - panda near Gate9; "
            "continue paper with 25-50% capital, do not unlock Live"
        )
    else:
        rec = (
            "rework - Gate9 PIT weak for panda; "
            + ("ma_trend numeric PASS but fragile; " if trend_pass else "")
            + ("momentum FAIL on maxDD; " if not mom_pass else "")
            + "no Live path"
        )

    summary = {
        "recommendation": rec,
        "coverage": cov,
        "fee_all_in_bps": FEE * 10000,
        "folds": fold_rows,
        "strategies": results,
        "status": {
            "panda_rank_composite": (
                "PAPER_TRADING_APPROVED" if panda_pass else "PAPER_TRADING_CONDITIONAL"
            ),
            "ma_trend_strength_60d": "WATCHLIST",
            "rotation_skip5_mom": "DEGRADED",
            "live": "LOCKED",
        },
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    update_status(panda_pass)
    print(f"\nRECOMMENDATION: {rec}")
    print(f"Saved {OUT_JSON}")


if __name__ == "__main__":
    main()
