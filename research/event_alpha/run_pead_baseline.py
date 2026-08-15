# -*- coding: utf-8 -*-
"""Track B PEAD / event baseline (initial research).

Primary: yfinance earnings dates + reaction → T+5 abnormal return.
Fallback if earnings API empty: gap+volume shock events (information-delay proxy).

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.run_pead_baseline
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
OUT = ROOT / "data" / "research" / "event_alpha_pead_baseline.json"
STATUS = ROOT / "data" / "research" / "strategy_status.json"


def load_mkt(close: pd.DataFrame) -> pd.Series:
    if "SPY" in close.columns:
        return close["SPY"].astype(float)
    return close.pct_change().mean(axis=1).fillna(0.0).add(1.0).cumprod()


def forward_abnormal(close: pd.Series, mkt: pd.Series, pos: int, horizon: int = 5) -> float:
    entry = pos + 1
    exit_ = pos + horizon
    if exit_ >= len(close) or entry >= len(close):
        return np.nan
    r_s = float(close.iloc[exit_] / close.iloc[entry] - 1.0)
    m = mkt.reindex(close.index).ffill()
    r_m = float(m.iloc[exit_] / m.iloc[entry] - 1.0)
    return r_s - r_m


def earnings_events(symbol: str, start: str = "2021-01-01") -> pd.DataFrame:
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=40)
    except Exception:
        return pd.DataFrame()
    if ed is None or ed.empty:
        return pd.DataFrame()
    ed = ed.copy()
    ed.index = pd.to_datetime(ed.index).tz_localize(None)
    return ed[ed.index >= pd.Timestamp(start)]


def build_earnings_rows(close: pd.DataFrame, mkt: pd.Series, symbols: list[str]) -> pd.DataFrame:
    rows = []
    for i, sym in enumerate(symbols, 1):
        ed = earnings_events(sym)
        if ed.empty:
            continue
        px = close[sym].dropna()
        for dt, row in ed.iterrows():
            if dt not in px.index:
                continue
            pos = px.index.get_loc(dt)
            if not isinstance(pos, (int, np.integer)) or pos < 1:
                continue
            reaction = float(px.iloc[pos] / px.iloc[pos - 1] - 1.0)
            surprise = np.nan
            for col in ("Surprise(%)", "EPS Surprise"):
                if col in ed.columns and pd.notna(row.get(col)):
                    try:
                        surprise = float(row.get(col))
                    except Exception:
                        pass
                    break
            rows.append(
                {
                    "source": "earnings",
                    "symbol": sym,
                    "event_date": pd.Timestamp(dt).strftime("%Y-%m-%d"),
                    "reaction": reaction,
                    "surprise": surprise,
                    "abn_t5": forward_abnormal(px, mkt, int(pos), 5),
                    "abn_t20": forward_abnormal(px, mkt, int(pos), 20),
                }
            )
        if i % 10 == 0:
            print(f"  earnings {i}/{len(symbols)} rows={len(rows)}")
        time.sleep(0.2)
    return pd.DataFrame(rows)


def build_shock_rows(close: pd.DataFrame, mkt: pd.Series, symbols: list[str]) -> pd.DataFrame:
    """Fallback event: |overnight gap| high + volume-proxy (abs return) shock."""
    rows = []
    for sym in symbols:
        px = close[sym].dropna()
        if len(px) < 80:
            continue
        ret = px.pct_change()
        gap = ret  # close-to-close proxy for gap when open unavailable
        volz = ret.abs().rolling(20).mean()
        volz = (volz - volz.rolling(60).mean()) / volz.rolling(60).std()
        # event if |ret| in top 2% of own history and volz > 1
        thr = ret.abs().quantile(0.98)
        for i in range(60, len(px) - 6):
            if abs(float(ret.iloc[i])) < thr:
                continue
            if not np.isfinite(volz.iloc[i]) or float(volz.iloc[i]) < 1.0:
                continue
            rows.append(
                {
                    "source": "price_volume_shock",
                    "symbol": sym,
                    "event_date": px.index[i].strftime("%Y-%m-%d"),
                    "reaction": float(ret.iloc[i]),
                    "surprise": float(volz.iloc[i]),
                    "abn_t5": forward_abnormal(px, mkt, i, 5),
                    "abn_t20": forward_abnormal(px, mkt, i, 20),
                }
            )
    return pd.DataFrame(rows)


def rank_ic(df: pd.DataFrame, x: str, y: str) -> float:
    sub = df.dropna(subset=[x, y])
    if len(sub) < 30:
        return float("nan")
    return float(sub[x].rank().corr(sub[y].rank()))


def spread(df: pd.DataFrame, score: str) -> dict:
    sub = df.dropna(subset=[score, "abn_t5"])
    if len(sub) < 40:
        return {"note": "too_few"}
    q = sub[score].quantile([0.25, 0.75])
    top = float(sub[sub[score] >= q.loc[0.75]]["abn_t5"].mean())
    bot = float(sub[sub[score] <= q.loc[0.25]]["abn_t5"].mean())
    return {"top_abn_t5": top, "bot_abn_t5": bot, "spread": top - bot}


def summarize(df: pd.DataFrame, name: str) -> dict:
    if df.empty:
        return {"name": name, "n_events": 0, "note": "empty"}
    stats = {
        "name": name,
        "n_events": int(len(df)),
        "n_symbols": int(df["symbol"].nunique()),
        "mean_abn_t5": float(df["abn_t5"].mean()),
        "mean_abn_t20": float(df["abn_t20"].dropna().mean()) if df["abn_t20"].notna().any() else None,
        "reaction_rankic_t5": rank_ic(df, "reaction", "abn_t5"),
        "surprise_rankic_t5": rank_ic(df, "surprise", "abn_t5"),
        "reaction_quartile_spread": spread(df, "reaction"),
        "surprise_quartile_spread": spread(df, "surprise"),
    }
    ric = stats["reaction_rankic_t5"]
    stats["signal_hint"] = bool(ric == ric and abs(ric) >= 0.03)
    return stats


def main() -> None:
    close = pd.read_parquet(CACHE)
    prefer = [
        "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "JPM", "XOM", "UNH", "V",
        "MA", "HD", "PG", "JNJ", "COST", "AVGO", "LLY", "WMT", "BAC", "ORCL",
        "CRM", "CSCO", "KO", "PEP", "MRK", "ABBV", "CVX", "ADBE", "ACN", "MCD",
    ]
    symbols = [s for s in prefer if s in close.columns][:30]
    mkt = load_mkt(close)
    print(f"PEAD/event baseline symbols={len(symbols)}")

    earn = build_earnings_rows(close, mkt, symbols)
    print(f"earnings events={len(earn)}")
    shock = build_shock_rows(close, mkt, symbols)
    print(f"shock events={len(shock)}")

    results = {
        "earnings": summarize(earn.dropna(subset=["abn_t5"]) if not earn.empty else earn, "earnings"),
        "price_volume_shock": summarize(
            shock.dropna(subset=["abn_t5"]) if not shock.empty else shock,
            "price_volume_shock",
        ),
    }

    # choose recommendation
    e = results["earnings"]
    s = results["price_volume_shock"]
    if e.get("signal_hint"):
        rec = "pead_continue - earnings reaction RankIC hint >=0.03"
    elif s.get("signal_hint"):
        rec = (
            "event_proxy_continue - earnings feed weak/empty; "
            "price-volume shock shows RankIC hint; upgrade to true PEAD data next"
        )
    elif e.get("n_events", 0) == 0:
        rec = "pead_data_blocked - yfinance earnings empty/rate-limited; keep Track B on shock proxy + better feed"
    else:
        rec = "pead_weak - no RankIC>=0.03 yet; refine event definition"

    results["recommendation"] = rec
    print(json.dumps(results, indent=2, default=float))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
    if STATUS.exists():
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        st.setdefault("Track_B", {})["PEAD"] = {
            "status": "INITIAL_RESEARCH",
            "results": results,
            "artifact": str(OUT),
        }
        st["updated_at"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%d")
        STATUS.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
