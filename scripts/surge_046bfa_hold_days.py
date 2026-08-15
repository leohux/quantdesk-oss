# -*- coding: utf-8 -*-
"""Compare max_hold_days 3..6 for strategy-046bfa (diagnostic, not a live retune).

Freezes live SL/TP/FB/day_crash. Same splits and equal-weight per-symbol
aggregation as surge_046bfa_is_oos.py.

  python /app/scripts/surge_046bfa_hold_days.py
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if Path("/app/backtest").exists() and "/app" not in sys.path:
    sys.path.insert(0, "/app")

from backtest.runner import run_signal_backtest

SID = "strategy-046bfa"
START = "2022-01-01"
VALID_START = "2024-01-01"
HOLDOUT_START = "2025-07-01"
HOLD_DAYS = (3, 4, 5, 6)
COST_BPS = (0, 10, 20)
OUT = ROOT / "data" / "research" / "surge_046bfa_hold_days.json"
MIN_BARS = {"train": 80, "valid": 40, "holdout": 40}


def _finite(v, default=float("nan")) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


def _dd_ratio(res: dict) -> float:
    v = abs(_finite(res.get("max_drawdown_pct"), 0.0))
    return v / 100.0 if v > 1.0 else v


def _metrics(res: dict) -> dict:
    wr = _finite(res.get("win_rate_pct"), 0.0)
    return {
        "sharpe": _finite(res.get("sharpe"), 0.0),
        "total_return": _finite(res.get("total_return"), 0.0),
        "max_drawdown": _dd_ratio(res),
        "trades": int(_finite(res.get("trades"), 0)),
        "win_rate": wr / 100.0 if wr > 1 else wr,
        "profit_factor": min(_finite(res.get("profit_factor"), float("nan")), 10.0),
    }


def _slice(df, start: str | None, end: str | None):
    out = df
    if start:
        out = out.loc[start:]
    if end:
        out = out.loc[:end]
    return out


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"n_symbols": 0}
    keys = ["sharpe", "total_return", "max_drawdown", "win_rate", "profit_factor"]
    out = {"n_symbols": len(rows), "trades": int(sum(r["trades"] for r in rows))}
    for k in keys:
        vals = [r[k] for r in rows if r.get(k) == r.get(k)]
        out[f"avg_{k}"] = float(np.mean(vals)) if vals else float("nan")
        if k == "max_drawdown" and vals:
            out["worst_max_drawdown"] = float(np.max(vals))
    rets = [r["total_return"] for r in rows if r.get("total_return") == r.get("total_return")]
    out["pos_frac"] = float(np.mean([1.0 if x > 0 else 0.0 for x in rets])) if rets else float("nan")
    return out


def run_split(code: str, params: dict, data: dict, start: str | None, end: str | None, fees: float, min_bars: int):
    rows = []
    for sym, df in data.items():
        sub = _slice(df, start, end)
        if sub is None or len(sub) < min_bars:
            continue
        try:
            res = run_signal_backtest(sub, code, params, fees=fees)
            m = _metrics(res)
            m["symbol"] = sym
            rows.append(m)
        except Exception as exc:
            rows.append(
                {
                    "symbol": sym,
                    "error": str(exc)[:160],
                    "trades": 0,
                    "sharpe": float("nan"),
                    "total_return": float("nan"),
                    "max_drawdown": float("nan"),
                    "win_rate": float("nan"),
                    "profit_factor": float("nan"),
                }
            )
    return aggregate(rows)


def _fmt(v, pct=False) -> str:
    if v is None or v != v:
        return "  n/a"
    if pct:
        return f"{v:6.1%}"
    return f"{v:7.3f}"


def _load_strategy() -> tuple[dict, str]:
    try:
        from config.store import ensure_strategy_code, get_strategy

        st = get_strategy(SID)
        code = ensure_strategy_code(st)
        return st, code
    except Exception as exc:
        print(f"store helper unavailable ({exc}); loading JSON + strategy_code", flush=True)

    store = ROOT / "data" / "store" / "strategies.json"
    rows = json.loads(store.read_text(encoding="utf-8"))
    st = next(s for s in rows if s.get("id") == SID)
    code_path = ROOT / "data" / "store" / "strategy_code" / f"{SID}.py"
    code = code_path.read_text(encoding="utf-8")
    return st, code


def _normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    rename = {c: str(c).title() for c in df.columns}
    df = df.rename(columns=rename)
    need = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol} missing {missing}")
    out = df[need].copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    out = out.sort_index()
    out = out[out["Close"].notna() & (out["Close"].astype(float) > 0)]
    return out


def load_universe(symbols: list[str]) -> dict[str, pd.DataFrame]:
    cache_dir = ROOT / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, pd.DataFrame] = {}
    missing: list[str] = []

    for sym in symbols:
        path = cache_dir / f"ohlcv_{sym}_2021-01-01_open.pkl"
        if path.exists():
            try:
                df = pd.read_pickle(path)
                if isinstance(df, pd.DataFrame) and len(df) >= MIN_BARS["holdout"]:
                    data[sym] = df
                    print(f"  cache {sym}: {len(df)}", flush=True)
                    continue
            except Exception:
                pass
        missing.append(sym)

    if missing:
        print(f"yahoo chart fetch {len(missing)} symbols", flush=True)
        fetched = _yahoo_chart_fetch(missing, start="2021-01-01")
        for sym, df in fetched.items():
            path = cache_dir / f"ohlcv_{sym}_2021-01-01_open.pkl"
            try:
                df.to_pickle(path)
            except Exception:
                pass
            data[sym] = df
            print(f"  loaded {sym}: {len(df)}", flush=True)
        still = [s for s in missing if s not in fetched]
        for s in still:
            print(f"  skip {s}: fetch failed", flush=True)
    return data


def _yahoo_chart_fetch(symbols: list[str], start: str) -> dict[str, pd.DataFrame]:
    import json
    import time
    import urllib.error
    import urllib.request
    from datetime import datetime as dt

    start_ts = int(dt.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    end_ts = int(dt.now(timezone.utc).timestamp())
    out: dict[str, pd.DataFrame] = {}
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    for i, sym in enumerate(symbols):
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?period1={start_ts}&period2={end_ts}&interval=1d&events=div%2Csplits"
        )
        df = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode())
                result = (payload.get("chart") or {}).get("result") or []
                if not result:
                    raise ValueError("empty chart")
                res = result[0]
                ts = res.get("timestamp") or []
                quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                df = pd.DataFrame(
                    {
                        "Open": quote.get("open"),
                        "High": quote.get("high"),
                        "Low": quote.get("low"),
                        "Close": quote.get("close"),
                        "Volume": quote.get("volume"),
                    },
                    index=pd.to_datetime(ts, unit="s", utc=True).tz_convert("UTC").tz_localize(None),
                )
                df = df[df["Close"].notna() & (df["Close"].astype(float) > 0)]
                if len(df) < MIN_BARS["holdout"]:
                    raise ValueError(f"only {len(df)} bars")
                break
            except Exception as exc:
                df = None
                wait = 1.5 * (2**attempt)
                print(f"    retry {sym} ({attempt+1}/4): {exc} sleep {wait:.1f}s", flush=True)
                time.sleep(wait)
        if df is not None:
            out[sym] = df
        time.sleep(0.12)
        if (i + 1) % 10 == 0:
            print(f"  fetched {i+1}/{len(symbols)}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--skip-spcx", action="store_true", default=True)
    args = ap.parse_args()

    st, code = _load_strategy()
    base = dict(st.get("params") or {})
    symbols = [str(s).upper() for s in (base.get("symbols") or [])]
    if args.skip_spcx:
        symbols = [s for s in symbols if s != "SPCX"]
    if args.max_symbols and args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]

    print(
        f"strategy={st.get('name')} id={SID} symbols={len(symbols)} "
        f"SL={base.get('stop_loss')} TP={base.get('take_profit')} "
        f"live_hold={base.get('max_hold_days')} holds={list(HOLD_DAYS)}",
        flush=True,
    )

    data = load_universe(symbols)
    if not data:
        print("ERROR: no symbols loaded", flush=True)
        return 1

    split_specs = [
        ("train", START, VALID_START, MIN_BARS["train"]),
        ("valid", VALID_START, HOLDOUT_START, MIN_BARS["valid"]),
        ("holdout", HOLDOUT_START, None, MIN_BARS["holdout"]),
    ]

    report = {
        "strategy_id": SID,
        "name": st.get("name"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Diagnostic only. Backtest hold_days counts trading bars; live "
            "max_hold_days is calendar days. Do not promote from this grid."
        ),
        "frozen_params": {
            k: base.get(k)
            for k in (
                "buy_surge",
                "buy_cap",
                "stop_loss",
                "take_profit",
                "trend_ma",
                "day_crash",
                "filter_false_breakout",
                "min_vol_ratio",
                "breakout_lookback",
                "max_prior_ret",
                "min_close_loc",
            )
        },
        "splits": {
            "train": f"{START}..{VALID_START}",
            "valid": f"{VALID_START}..{HOLDOUT_START}",
            "holdout": f"{HOLDOUT_START}..now",
        },
        "n_symbols_loaded": len(data),
        "engine_note": "vectorbt if installed, else pandas fallback in run_signal_backtest",
        "results": {},
        "ranking_10bp": {},
    }

    for hold in HOLD_DAYS:
        params = deepcopy(base)
        params["max_hold_days"] = int(hold)
        report["results"][str(hold)] = {}
        print(f"\n=== max_hold_days={hold} ===", flush=True)
        for bps in COST_BPS:
            fees = bps / 10_000.0
            report["results"][str(hold)][str(bps)] = {}
            print(f"  cost {bps}bp", flush=True)
            for name, start, end, min_bars in split_specs:
                agg = run_split(code, params, data, start, end, fees, min_bars)
                report["results"][str(hold)][str(bps)][name] = agg
                print(
                    f"    {name:8s} n={agg.get('n_symbols')} trades={agg.get('trades')} "
                    f"sharpe={agg.get('avg_sharpe', float('nan')):.3f} "
                    f"ret={agg.get('avg_total_return', float('nan')):.3f} "
                    f"dd={agg.get('avg_max_drawdown', float('nan')):.3f} "
                    f"wr={agg.get('avg_win_rate', float('nan')):.2f} "
                    f"pos={agg.get('pos_frac', float('nan')):.2f}",
                    flush=True,
                )

    # Rank by valid Sharpe @ 10bp; confirm on holdout.
    ranked = []
    for hold in HOLD_DAYS:
        v = report["results"][str(hold)]["10"]["valid"]
        h = report["results"][str(hold)]["10"]["holdout"]
        t = report["results"][str(hold)]["10"]["train"]
        ranked.append(
            {
                "max_hold_days": hold,
                "train_sharpe": t.get("avg_sharpe"),
                "valid_sharpe": v.get("avg_sharpe"),
                "holdout_sharpe": h.get("avg_sharpe"),
                "valid_return": v.get("avg_total_return"),
                "holdout_return": h.get("avg_total_return"),
                "valid_win_rate": v.get("avg_win_rate"),
                "holdout_win_rate": h.get("avg_win_rate"),
                "valid_trades": v.get("trades") or 0,
                "holdout_trades": h.get("trades") or 0,
                "valid_dd": v.get("avg_max_drawdown"),
                "holdout_dd": h.get("avg_max_drawdown"),
                "valid_pos_frac": v.get("pos_frac"),
                "holdout_pos_frac": h.get("pos_frac"),
            }
        )
    if not ranked or all((r.get("valid_sharpe") is None or r["valid_sharpe"] != r["valid_sharpe"]) for r in ranked):
        print("ERROR: no backtest results (data load failed)", flush=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return 1
    ranked.sort(
        key=lambda r: (r["valid_sharpe"] if r["valid_sharpe"] == r["valid_sharpe"] else -9e9),
        reverse=True,
    )
    winner = ranked[0]["max_hold_days"] if ranked else None
    holdout_best = max(
        ranked,
        key=lambda r: r["holdout_sharpe"] if r["holdout_sharpe"] == r["holdout_sharpe"] else -9e9,
    )["max_hold_days"]
    live = int(base.get("max_hold_days") or 3)
    live_row = next(r for r in ranked if r["max_hold_days"] == live)
    win_row = ranked[0]
    report["ranking_10bp"] = {
        "rank_by_valid_sharpe": ranked,
        "best_valid": winner,
        "best_holdout": holdout_best,
        "valid_agrees_holdout": winner == holdout_best,
        "delta_vs_live_3d": {
            "best": winner,
            "valid_sharpe_delta": (win_row["valid_sharpe"] or 0) - (live_row["valid_sharpe"] or 0),
            "holdout_sharpe_delta": (win_row["holdout_sharpe"] or 0) - (live_row["holdout_sharpe"] or 0),
        },
        "verdict_note": (
            "Prefer holdout agreement. Small Sharpe deltas on a noise-scale "
            "entry are not a live-param change."
        ),
    }

    print("\n=== RANKING @ 10bp (valid Sharpe) ===", flush=True)
    print(
        f"{'hold':>4} {'tr_sh':>7} {'va_sh':>7} {'ho_sh':>7} "
        f"{'va_ret':>8} {'ho_ret':>8} {'va_wr':>7} {'ho_wr':>7} "
        f"{'va_n':>6} {'ho_n':>6}",
        flush=True,
    )
    for r in ranked:
        print(
            f"{r['max_hold_days']:4d} "
            f"{_fmt(r['train_sharpe'])} {_fmt(r['valid_sharpe'])} {_fmt(r['holdout_sharpe'])} "
            f"{_fmt(r['valid_return'], True)} {_fmt(r['holdout_return'], True)} "
            f"{_fmt(r['valid_win_rate'], True)} {_fmt(r['holdout_win_rate'], True)} "
            f"{int(r['valid_trades'] or 0):6d} {int(r['holdout_trades'] or 0):6d}",
            flush=True,
        )
    print(
        f"\nbest_valid={winner}d  best_holdout={holdout_best}d  "
        f"agree={winner == holdout_best}",
        flush=True,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nWrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
