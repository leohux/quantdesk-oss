# -*- coding: utf-8 -*-
"""Gate11-style attribution for Surge-NVDA (EOD surge cousin of 046bfa).

Builds an equal-weight sleeve return series from per-symbol signal backtests
(same engine/params as IS/OOS), then:

  A) R = a + b*SPY + e
  B) R = a + b1*SPY + b2*MOM + b3*VALUE + b4*LOWVOL + e
  C) Bull/Bear split (SPY > MA200)
  D) Benchmarks: SPY B&H, SPY+200MA, QQQ B&H, MTUM B&H

Primary window: 2024-01-01 .. 2025-12-31 (clean OOS, aligned with Gate11)
Secondary:     2022-01-01 .. now (full sample robustness)
Cost: 10bp one-way (focus tier from IS/OOS)

Usage:
  python /app/scripts/surge_nvda_attribution.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest.runner import run_signal_backtest  # noqa: E402
from config.store import ensure_strategy_code, get_strategy  # noqa: E402
from data.loader import load_ohlcv  # noqa: E402

SID = "cursor-surge-nvda-052828-63859c-82d552"
START = "2022-01-01"
WIN_PRIMARY = ("2024-01-01", "2025-12-31")
FEE = 0.001  # 10bp
FACTOR_ETFS = ["SPY", "QQQ", "MTUM", "VLUE", "USMV"]
ROOT = Path(__file__).resolve().parents[1]
if Path("/app/data/research").exists():
    OUT = Path("/app/data/research/surge_nvda_attribution.json")
    STATUS = Path("/app/data/research/strategy_status.json")
else:
    OUT = ROOT / "data" / "research" / "surge_nvda_attribution.json"
    STATUS = ROOT / "data" / "research" / "strategy_status.json"


def load_factor_etfs(start: str = "2020-01-01") -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        FACTOR_ETFS,
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    out = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in FACTOR_ETFS:
            try:
                if sym in raw.columns.get_level_values(0):
                    s = raw[sym]["Close"]
                elif ("Close", sym) in raw.columns:
                    s = raw[("Close", sym)]
                else:
                    continue
                out[sym] = pd.to_numeric(s, errors="coerce")
            except Exception:
                continue
    else:
        if "Close" in raw.columns:
            out["SPY"] = pd.to_numeric(raw["Close"], errors="coerce")
    df = pd.DataFrame(out)
    df.index = pd.to_datetime(df.index)
    return df.sort_index().ffill()


def ols(y: pd.Series, X: pd.DataFrame) -> dict:
    data = pd.concat([y.rename("y"), X], axis=1).dropna()
    if len(data) < 40:
        return {"error": "insufficient_obs", "n": len(data)}
    yv = data["y"].to_numpy(dtype=float)
    xv = np.column_stack([np.ones(len(data)), data[X.columns].to_numpy(dtype=float)])
    beta, *_ = np.linalg.lstsq(xv, yv, rcond=None)
    fitted = xv @ beta
    resid = yv - fitted
    n, k = xv.shape
    dof = max(n - k, 1)
    sse = float(resid @ resid)
    xtx_inv = np.linalg.pinv(xv.T @ xv)
    meat = xv.T @ (resid[:, None] ** 2 * xv)
    cov = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    tstat = beta / np.where(se > 0, se, np.nan)
    from math import erfc, sqrt

    pvals = [float(erfc(abs(t) / sqrt(2.0))) for t in tstat]
    names = ["alpha", *list(X.columns)]
    coefs = {names[i]: float(beta[i]) for i in range(len(names))}
    tstats = {names[i]: float(tstat[i]) for i in range(len(names))}
    ps = {names[i]: pvals[i] for i in range(len(names))}
    r2 = 1.0 - sse / max(float(((yv - yv.mean()) ** 2).sum()), 1e-18)
    alpha_ann = float((1.0 + coefs["alpha"]) ** 252 - 1.0) if coefs["alpha"] > -1 else float("nan")
    return {
        "n": int(n),
        "r2": float(r2),
        "alpha_daily": coefs["alpha"],
        "alpha_ann": alpha_ann,
        "alpha_tstat": tstats["alpha"],
        "alpha_pval": ps["alpha"],
        "betas": {k: coefs[k] for k in X.columns},
        "tstats": {k: tstats[k] for k in ["alpha", *X.columns]},
        "pvals": {k: ps[k] for k in ["alpha", *X.columns]},
        "resid_vol_ann": float(np.std(resid, ddof=1) * np.sqrt(252)),
    }


def buyhold_rets(price: pd.Series, start: str, end: str | None) -> pd.Series:
    s = price.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None].dropna()
    return s.pct_change().dropna()


def ma_timing_rets(price: pd.Series, start: str, end: str | None, ma: int = 200) -> pd.Series:
    px = price.dropna().sort_index()
    sig = (px > px.rolling(ma).mean()).shift(1).fillna(False)
    rets = px.pct_change().fillna(0.0)
    timed = rets.where(sig, 0.0)
    timed = timed.loc[pd.Timestamp(start) : pd.Timestamp(end) if end else None]
    return timed.dropna()


def metrics_from_rets(rets: pd.Series) -> dict:
    rets = rets.dropna()
    if len(rets) < 2:
        return {"ann": 0.0, "sharpe": 0.0, "maxdd": 0.0, "total": 0.0}
    eq = (1.0 + rets).cumprod()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    vol = float(rets.std() * np.sqrt(252))
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
    dd = float(((eq - eq.cummax()) / eq.cummax()).min())
    return {"ann": float(ann), "sharpe": sharpe, "maxdd": dd, "total": total}


def regime_split(strat: pd.Series, spy: pd.Series, ma: int = 200) -> dict:
    spy = spy.reindex(strat.index).ffill()
    ma200 = spy.rolling(ma).mean()
    bull = (spy > ma200).shift(1)
    bull = bull.reindex(strat.index).fillna(False).astype(bool)
    out = {}
    for name, mask in (("bull_spy_gt_ma200", bull), ("bear_spy_lt_ma200", ~bull)):
        r = strat.loc[mask.to_numpy()]
        m = metrics_from_rets(r)
        out[name] = {
            **m,
            "n_days": int(mask.sum()),
            "fraction_of_days": float(mask.mean()),
            "sum_return": float(r.sum()) if len(r) else 0.0,
        }
    total = abs(out["bull_spy_gt_ma200"]["sum_return"]) + abs(out["bear_spy_lt_ma200"]["sum_return"])
    out["bull_return_share"] = (
        out["bull_spy_gt_ma200"]["sum_return"] / total if total > 1e-12 else None
    )
    return out


def classify(attr_mkt: dict, attr_multi: dict, regime: dict, vs_bench: dict) -> dict:
    alpha = attr_mkt.get("alpha_ann", 0.0) or 0.0
    t = abs(attr_mkt.get("alpha_tstat", 0.0) or 0.0)
    p = attr_mkt.get("alpha_pval", 1.0) or 1.0
    multi_a = attr_multi.get("alpha_ann", 0.0) or 0.0
    multi_t = abs(attr_multi.get("alpha_tstat", 0.0) or 0.0)
    beat_spy_timing = vs_bench.get("ma_trend_minus_spy_timing_ann")
    bull_share = regime.get("bull_return_share")

    if alpha >= 0.05 and t >= 2.0 and p < 0.05 and multi_a > 0.02 and multi_t >= 1.8:
        case = 1
        label = "independent_alpha_candidate"
        note = "Statistically meaningful alpha after market; multi-factor alpha still positive."
    elif alpha >= 0.01 and t >= 1.0:
        case = 2
        label = "weak_alpha_trend_etf_substitute"
        note = "Small residual alpha; mostly beta/trend exposure. Optimize execution, not signals."
    else:
        case = 3
        label = "market_timing_overlay_only"
        note = "Alpha ~0 after market; treat as timing overlay / beta sleeve, not alpha engine."

    if bull_share is not None and bull_share > 0.85 and case == 1:
        case = 2
        label = "weak_alpha_trend_etf_substitute"
        note = "Alpha stats look ok but returns concentrated in bull regime."
    if beat_spy_timing is not None and beat_spy_timing < 0 and case <= 2:
        note += " Does not beat simple SPY+200MA timing on clean OOS."

    return {
        "case": case,
        "label": label,
        "note": note,
        "alpha_ann_vs_spy": alpha,
        "alpha_tstat_vs_spy": attr_mkt.get("alpha_tstat"),
        "alpha_ann_multifactor": multi_a,
        "alpha_tstat_multifactor": attr_multi.get("alpha_tstat"),
    }


def _equity_full(ohlcv: pd.DataFrame, code: str, params: dict, fees: float) -> pd.Series:
    """Full equity curve (runner truncates to 365d in response; rebuild via vbt)."""
    from strategies.engine import run_signal_fn

    close = ohlcv["Close"].astype(float)
    run_params = dict(params or {})
    for col, key in (("High", "_high"), ("Low", "_low"), ("Open", "_open"), ("Volume", "_volume")):
        if col in ohlcv.columns:
            run_params[key] = ohlcv[col].astype(float)
    entries, exits = run_signal_fn(close, code, run_params)
    try:
        import vectorbt as vbt

        kwargs: dict = {}
        if "_open" in run_params:
            kwargs["open"] = run_params["_open"]
        if "_high" in run_params:
            kwargs["high"] = run_params["_high"]
        if "_low" in run_params:
            kwargs["low"] = run_params["_low"]
        if not run_params.get("_disable_engine_stops"):
            if run_params.get("stop_loss") is not None and "_low" in run_params:
                kwargs["sl_stop"] = abs(float(run_params["stop_loss"]))
            if run_params.get("take_profit") is not None and "_high" in run_params:
                kwargs["tp_stop"] = abs(float(run_params["take_profit"]))
        pf = vbt.Portfolio.from_signals(
            close,
            entries=entries,
            exits=exits,
            init_cash=100_000.0,
            fees=fees,
            freq="1D",
            **kwargs,
        )
        eq = pf.value()
        eq.index = pd.to_datetime(eq.index)
        return eq.astype(float)
    except Exception:
        # Fallback: use truncated curve from runner (last 365d only — last resort)
        res = run_signal_backtest(ohlcv, code, params, fees=fees)
        eq = pd.Series(
            {pd.Timestamp(p["date"]): float(p["equity"]) for p in res.get("equity_curve") or []}
        ).sort_index()
        return eq


def build_sleeve_rets(
    code: str,
    params: dict,
    symbols: list[str],
    fees: float,
) -> tuple[pd.Series, dict]:
    """Equal-weight average of per-symbol strategy daily returns (cash = flat)."""
    rets_map: dict[str, pd.Series] = {}
    n_ok = 0
    n_fail = 0
    for sym in symbols:
        try:
            df = load_ohlcv(sym, start=START)
            if df is None or len(df) < 80:
                n_fail += 1
                continue
            eq = _equity_full(df, code, params, fees)
            if eq is None or len(eq) < 40:
                n_fail += 1
                continue
            r = eq.pct_change().fillna(0.0)
            rets_map[sym] = r
            n_ok += 1
            print(f"  sleeve {sym}: bars={len(eq)}", flush=True)
        except Exception as exc:
            n_fail += 1
            print(f"  skip {sym}: {exc}", flush=True)
    if not rets_map:
        raise RuntimeError("no symbol equity series built")
    panel = pd.DataFrame(rets_map).sort_index()
    # equal-weight across available names that day (NaN → exclude)
    sleeve = panel.mean(axis=1, skipna=True).dropna()
    meta = {
        "n_symbols_ok": n_ok,
        "n_symbols_fail": n_fail,
        "n_days": int(len(sleeve)),
        "construction": "equal_weight_avg_of_single_name_signal_backtests",
        "fees": fees,
    }
    return sleeve, meta


def window_slice(rets: pd.Series, start: str, end: str | None) -> pd.Series:
    end_ts = pd.Timestamp(end) if end else None
    out = rets.loc[pd.Timestamp(start) : end_ts]
    return out.dropna()


def attr_block(strat: pd.Series, factors: pd.DataFrame, spy: pd.Series) -> dict:
    f_rets = factors.pct_change()
    X_mkt = f_rets[["SPY"]]
    cols = [c for c in ("SPY", "MTUM", "VLUE", "USMV") if c in f_rets.columns]
    rename = {"MTUM": "MOM", "VLUE": "VALUE", "USMV": "LOWVOL"}
    X_multi = f_rets[cols].rename(columns=rename)

    attr_mkt = ols(strat, X_mkt)
    attr_multi = ols(strat, X_multi)
    regime = regime_split(strat, spy)

    start = str(strat.index[0].date())
    end = str(strat.index[-1].date())
    spy_r = buyhold_rets(spy, start, end)
    qqq_r = buyhold_rets(factors["QQQ"], start, end) if "QQQ" in factors.columns else pd.Series(dtype=float)
    mtum_r = (
        buyhold_rets(factors["MTUM"], start, end) if "MTUM" in factors.columns else pd.Series(dtype=float)
    )
    timing_r = ma_timing_rets(spy, start, end, 200)

    bench = {
        "surge_nvda_sleeve": metrics_from_rets(strat),
        "spy_buyhold": metrics_from_rets(spy_r),
        "qqq_buyhold": metrics_from_rets(qqq_r),
        "mtum_buyhold": metrics_from_rets(mtum_r),
        "spy_ma200_timing": metrics_from_rets(timing_r),
    }
    spreads = {
        "sleeve_minus_spy_ann": bench["surge_nvda_sleeve"]["ann"] - bench["spy_buyhold"]["ann"],
        "sleeve_minus_spy_timing_ann": bench["surge_nvda_sleeve"]["ann"]
        - bench["spy_ma200_timing"]["ann"],
        "sleeve_minus_mtum_ann": bench["surge_nvda_sleeve"]["ann"] - bench["mtum_buyhold"]["ann"],
        "sleeve_minus_spy_sharpe": bench["surge_nvda_sleeve"]["sharpe"] - bench["spy_buyhold"]["sharpe"],
        "sleeve_minus_spy_timing_sharpe": bench["surge_nvda_sleeve"]["sharpe"]
        - bench["spy_ma200_timing"]["sharpe"],
        # classify() looks for ma_trend_minus_spy_timing_ann
        "ma_trend_minus_spy_timing_ann": bench["surge_nvda_sleeve"]["ann"]
        - bench["spy_ma200_timing"]["ann"],
    }
    verdict = classify(attr_mkt, attr_multi, regime, spreads)
    # Gate14-style checks (same spirit as alpha_v2 combo)
    a_t = abs(attr_mkt.get("alpha_tstat") or 0.0)
    mf_t = abs(attr_multi.get("alpha_tstat") or 0.0)
    gate14 = {
        "pass": bool(
            (attr_mkt.get("alpha_ann") or 0) > 0
            and a_t > 1.5
            and (attr_multi.get("alpha_ann") or 0) > 0
            and mf_t > 1.0
            and spreads["sleeve_minus_spy_timing_ann"] > 0
        ),
        "checks": {
            "spy_alpha_ann_gt_0": bool((attr_mkt.get("alpha_ann") or 0) > 0),
            "spy_alpha_t_gt_1.5": bool(a_t > 1.5),
            "sleeve_sharpe_gt_spy": bool(
                bench["surge_nvda_sleeve"]["sharpe"] > bench["spy_buyhold"]["sharpe"]
            ),
            "mf_alpha_ann_gt_0": bool((attr_multi.get("alpha_ann") or 0) > 0),
            "mf_alpha_t_gt_1.0": bool(mf_t > 1.0),
            "not_dominated_by_spy_timing": bool(spreads["sleeve_minus_spy_timing_ann"] > 0),
            "not_dominated_by_mtum": bool(spreads["sleeve_minus_mtum_ann"] > 0),
        },
    }
    return {
        "market_regression": attr_mkt,
        "multifactor_regression": attr_multi,
        "regime_split": regime,
        "benchmarks": bench,
        "benchmark_spreads": spreads,
        "verdict": verdict,
        "gate14_style": gate14,
    }


def main() -> int:
    st = get_strategy(SID)
    code = ensure_strategy_code(st)
    params = dict(st.get("params") or {})
    symbols = [str(s).upper() for s in (params.get("symbols") or []) if str(s).upper() != "SPCX"]

    print(f"strategy={st.get('name')} id={SID} symbols={len(symbols)} fee={FEE}", flush=True)
    sleeve, meta = build_sleeve_rets(code, params, symbols, FEE)
    print(f"sleeve days={len(sleeve)} symbols_ok={meta['n_symbols_ok']}", flush=True)

    print("loading factor ETFs...", flush=True)
    factors = load_factor_etfs(start="2020-01-01")
    spy = factors["SPY"]

    primary = window_slice(sleeve, WIN_PRIMARY[0], WIN_PRIMARY[1])
    full = window_slice(sleeve, START, None)

    print(f"\n=== PRIMARY {WIN_PRIMARY[0]}..{WIN_PRIMARY[1]} n={len(primary)} ===", flush=True)
    block_p = attr_block(primary, factors, spy)
    _print_block(block_p)

    print(f"\n=== FULL {START}..now n={len(full)} ===", flush=True)
    block_f = attr_block(full, factors, spy)
    _print_block(block_f)

    v = block_p["verdict"]
    g14 = block_p["gate14_style"]
    disposition = {
        "strategy_id": SID,
        "name": st.get("name"),
        "status": "research_archive_gate_fail",
        "live": False,
        "forbid": ["live", "increase_capital", "parameter_optimization", "use_paper_as_main_book"],
        "reason_codes": [
            "is_oos_fail_cost_sensitivity",
            "signal_decay_0bp_monotonic",
            v["label"],
            "gate14_style_fail" if not g14["pass"] else "gate14_style_pass",
        ],
        "one_liner": (
            f"CASE {v['case']} {v['label']}: "
            f"SPY α_ann={v.get('alpha_ann_vs_spy')} t={v.get('alpha_tstat_vs_spy')}; "
            f"multi α_ann={v.get('alpha_ann_multifactor')} t={v.get('alpha_tstat_multifactor')}; "
            f"gate14_style_pass={g14['pass']}."
        ),
        "note": v.get("note"),
    }

    out = {
        "strategy_id": SID,
        "name": st.get("name"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fee_bps": int(FEE * 10_000),
        "sleeve_meta": meta,
        "params_snapshot": {
            k: params.get(k)
            for k in (
                "buy_surge",
                "buy_cap",
                "stop_loss",
                "take_profit",
                "trend_ma",
                "max_hold_days",
                "day_crash",
                "filter_false_breakout",
            )
        },
        "primary_window": {"start": WIN_PRIMARY[0], "end": WIN_PRIMARY[1]},
        "primary": block_p,
        "full_sample": block_f,
        "disposition": disposition,
        "prior_evidence": {
            "is_oos_artifact": "data/research/surge_nvda_is_oos.json",
            "is_oos_gates_pass": None,
            "paper_booked_realized_after_cleanup": -2892.89,
            "related": "cousin of strategy-046bfa; overlapping names NVDA/PLTR/HOOD",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=float), encoding="utf-8")
    print(f"\nWrote {OUT}", flush=True)

    if STATUS.exists():
        status = json.loads(STATUS.read_text(encoding="utf-8"))
        status.setdefault("strategies", {})
        status["strategies"][SID] = {
            "status": disposition["status"],
            "live": False,
            "attribution": v,
            "gate14_style": g14,
            "artifact": str(OUT).replace("\\", "/").split("quantdesk/")[-1]
            if "quantdesk" in str(OUT).replace("\\", "/")
            else "data/research/surge_nvda_attribution.json",
        }
        status["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_list = status.setdefault("system", {}).setdefault("next", [])
        note = f"Surge-NVDA: attribution {disposition['status']}"
        if note not in next_list:
            next_list.insert(0, note)
        STATUS.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Updated {STATUS}", flush=True)

    print("\n=== DISPOSITION ===", flush=True)
    print(disposition["one_liner"], flush=True)
    print("status:", disposition["status"], flush=True)
    return 0 if g14["pass"] and v["case"] == 1 else 2


def _print_block(block: dict) -> None:
    m = block["market_regression"]
    mf = block["multifactor_regression"]
    b = block["benchmarks"]
    r = block["regime_split"]
    v = block["verdict"]
    g = block["gate14_style"]
    print(
        f"sleeve ann={b['surge_nvda_sleeve']['ann']:.1%} "
        f"S={b['surge_nvda_sleeve']['sharpe']:.2f} DD={b['surge_nvda_sleeve']['maxdd']:.1%}",
        flush=True,
    )
    print(
        f"vs SPY: alpha_ann={m.get('alpha_ann', float('nan')):.2%} "
        f"t={m.get('alpha_tstat', float('nan')):.2f} "
        f"beta={m.get('betas', {}).get('SPY', float('nan')):.2f} "
        f"R2={m.get('r2', float('nan')):.2f}",
        flush=True,
    )
    print(
        f"multi: alpha_ann={mf.get('alpha_ann', float('nan')):.2%} "
        f"t={mf.get('alpha_tstat', float('nan')):.2f} betas={mf.get('betas', {})}",
        flush=True,
    )
    print(
        f"bull_share={r.get('bull_return_share')} | "
        f"vs timing ann={block['benchmark_spreads']['sleeve_minus_spy_timing_ann']:+.1%} | "
        f"vs MTUM ann={block['benchmark_spreads']['sleeve_minus_mtum_ann']:+.1%}",
        flush=True,
    )
    print(f"CASE {v['case']}: {v['label']} | gate14_style_pass={g['pass']}", flush=True)
    print(f"  checks={g['checks']}", flush=True)
    print(f"  {v['note']}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
