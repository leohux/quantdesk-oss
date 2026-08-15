# -*- coding: utf-8 -*-
"""Portfolio-layer analysis for the 7 enabled paper strategies (READ-ONLY).

Phase 1  correlation suite (daily / rolling-60 / drawdown / monthly)
Phase 2  risk contribution table (ret / vol / RC / MRC / weight)
Phase 3  weight schemes (Equal / InvVol / ERC / MaxDiversification / VolTarget)
Phase 4  stress tests (leave-one-out / MiMo sweep 0..30% / regimes)

Daily returns are reconstructed with the SAME long/flat + intraday stop logic as
backtest.runner._pandas, per strategy across its configured symbol basket
(equal-weight, daily-rebalanced). No orders, no state changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import list_strategies, get_strategy
from data.loader import load_ohlcv
from strategies.engine import run_signal_fn

START = "2022-01-01"
END = None  # open
FEES = 0.001
ANN = 252
OOS_START = pd.Timestamp("2024-01-01")
OUT = Path("/app/data/store/portfolio_analysis.json")
OUT_MD = Path("/app/data/store/portfolio_analysis.md")

_OHLCV: dict[str, pd.DataFrame] = {}


def ohlcv(sym: str) -> pd.DataFrame | None:
    if sym not in _OHLCV:
        try:
            df = load_ohlcv(sym, start=START, end=END)
            df.index = pd.to_datetime(df.index)
            _OHLCV[sym] = df
        except Exception:
            _OHLCV[sym] = None
    return _OHLCV[sym]


def load_code(sid: str) -> str:
    p = Path(f"/app/data/store/strategy_code/{sid}.py")
    if p.exists():
        return p.read_text(encoding="utf-8")
    try:
        return get_strategy(sid).get("code") or ""
    except Exception:
        return ""


def _stop_fill(entry, close_px, open_px, high_px, low_px, sl, tp):
    if sl is not None and low_px is not None:
        sl_px = entry * (1.0 + sl)
        if open_px is not None and open_px <= sl_px:
            return open_px
        if low_px <= sl_px:
            return sl_px
    if tp is not None and high_px is not None:
        tp_px = entry * (1.0 + tp)
        if open_px is not None and open_px >= tp_px:
            return open_px
        if high_px >= tp_px:
            return tp_px
    return close_px


def equity_series(code: str, params: dict, sym: str) -> pd.Series | None:
    df = ohlcv(sym)
    if df is None or len(df) < 60:
        return None
    close = df["Close"].astype(float)
    open_ = df["Open"].astype(float) if "Open" in df else None
    high = df["High"].astype(float) if "High" in df else None
    low = df["Low"].astype(float) if "Low" in df else None
    rp = dict(params or {})
    rp.pop("symbols", None)
    for k, s in (("_high", high), ("_low", low), ("_open", open_), ("_volume", df.get("Volume"))):
        if s is not None:
            rp[k] = s.astype(float) if hasattr(s, "astype") else s
    try:
        entries, exits = run_signal_fn(close, code, rp)
    except Exception:
        return None
    sl = params.get("stop_loss")
    tp = params.get("take_profit")
    sl = float(sl) if isinstance(sl, (int, float)) else None
    tp = float(tp) if isinstance(tp, (int, float)) else None

    cash, shares, entry_price = 100_000.0, 0.0, None
    eq = []
    for dt in close.index:
        price = float(close.loc[dt])
        opx = float(open_.loc[dt]) if open_ is not None else None
        hpx = float(high.loc[dt]) if high is not None else None
        lpx = float(low.loc[dt]) if low is not None else None
        force = False
        if shares > 0 and entry_price:
            if sl is not None and lpx is not None:
                if (opx is not None and opx <= entry_price * (1 + sl)) or lpx <= entry_price * (1 + sl):
                    force = True
            if tp is not None and hpx is not None and not force:
                if (opx is not None and opx >= entry_price * (1 + tp)) or hpx >= entry_price * (1 + tp):
                    force = True
        if bool(entries.loc[dt]) and shares == 0:
            shares = (cash * (1 - FEES)) / price
            cash, entry_price = 0.0, price
        elif (bool(exits.loc[dt]) or force) and shares > 0:
            fill = _stop_fill(entry_price or price, price, opx, hpx, lpx, sl, tp)
            cash = shares * fill * (1 - FEES)
            shares, entry_price = 0.0, None
        eq.append(cash + shares * price)
    return pd.Series(eq, index=close.index)


def strategy_daily_returns(s: dict) -> pd.Series | None:
    code = load_code(s["id"])
    if not code:
        return None
    syms = [str(x).upper() for x in (s.get("params") or {}).get("symbols") or []]
    if not syms:
        return None
    per = []
    for sym in syms:
        eq = equity_series(code, s.get("params") or {}, sym)
        if eq is not None:
            r = eq.pct_change().fillna(0.0)
            per.append(r)
    if not per:
        return None
    mat = pd.concat(per, axis=1).fillna(0.0)
    return mat.mean(axis=1)  # equal-weight across basket, daily rebalanced


# ---------- portfolio math ----------
def ann_stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 5 or r.std() == 0:
        return {"ret": 0.0, "vol": 0.0, "sharpe": 0.0, "maxdd": 0.0}
    eq = (1 + r).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return {
        "ret": float((eq.iloc[-1] ** (ANN / len(r)) - 1) * 100),
        "vol": float(r.std() * np.sqrt(ANN) * 100),
        "sharpe": float(r.mean() / r.std() * np.sqrt(ANN)),
        "maxdd": float(abs(dd) * 100),
    }


def port_returns(R: pd.DataFrame, w: np.ndarray) -> pd.Series:
    return R.mul(w, axis=1).sum(axis=1)


def risk_contrib(R: pd.DataFrame, w: np.ndarray) -> dict:
    cov = R.cov().values * ANN
    pv = float(w @ cov @ w)
    pvol = np.sqrt(pv)
    mrc = cov @ w / pvol if pvol > 0 else np.zeros_like(w)
    rc = w * mrc
    pct = rc / rc.sum() if rc.sum() != 0 else rc
    return {"pvol": pvol * 100, "mrc": mrc, "rc": rc, "pct_rc": pct}


def erc_weights(R: pd.DataFrame, iters: int = 20000, lr: float = 0.05) -> np.ndarray:
    cov = R.cov().values * ANN
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        pvol = np.sqrt(w @ cov @ w)
        if pvol <= 0:
            break
        mrc = cov @ w / pvol
        rc = w * mrc
        target = rc.mean()
        grad = rc - target
        w = w - lr * grad
        w = np.clip(w, 1e-4, None)
        w = w / w.sum()
    return w


def inv_vol_weights(R: pd.DataFrame) -> np.ndarray:
    vol = R.std().values * np.sqrt(ANN)
    inv = 1.0 / np.where(vol > 0, vol, np.inf)
    return inv / inv.sum()


def max_div_weights(R: pd.DataFrame, iters: int = 20000, lr: float = 0.05) -> np.ndarray:
    cov = R.cov().values * ANN
    vol = np.sqrt(np.diag(cov))
    n = len(vol)
    w = np.ones(n) / n
    for _ in range(iters):
        pvol = np.sqrt(w @ cov @ w)
        if pvol <= 0:
            break
        # diversification ratio DR = (w·vol)/pvol ; ascend
        num = w @ vol
        grad = vol / pvol - (num / (pvol**3)) * (cov @ w)
        w = w + lr * grad / (np.linalg.norm(grad) + 1e-9)
        w = np.clip(w, 1e-4, None)
        w = w / w.sum()
    return w


def scheme_row(name: str, R: pd.DataFrame, w: np.ndarray, oos_mask) -> dict:
    full = ann_stats(port_returns(R, w))
    oos = ann_stats(port_returns(R.loc[oos_mask], w))
    return {"scheme": name, "weights": w.tolist(),
            "full": full, "oos": oos}


def main():
    items = list_strategies()
    enabled = [x for x in items if x.get("enabled")]
    names, series = [], []
    for s in enabled:
        r = strategy_daily_returns(s)
        if r is None:
            print("skip (no returns):", s.get("name"))
            continue
        names.append(s.get("name"))
        series.append(r.rename(s.get("name")))
    R = pd.concat(series, axis=1).dropna(how="all").fillna(0.0)
    R = R.loc[R.index >= pd.Timestamp(START)]
    n = R.shape[1]
    print(f"strategies={n} days={len(R)} range={R.index[0].date()}..{R.index[-1].date()}")

    short = {nm: nm.split("-")[0][:10] + "…" if len(nm) > 14 else nm for nm in names}
    labels = list(R.columns)

    # ---- Phase 1: correlation suite ----
    daily_corr = R.corr()
    # rolling 60d: avg pairwise corr over time + max sustained pair
    roll_pairs = {}
    if len(R) > 80:
        for i in range(n):
            for j in range(i + 1, n):
                rc = R.iloc[:, i].rolling(60).corr(R.iloc[:, j])
                roll_pairs[(labels[i], labels[j])] = float(rc.mean())
    # drawdown correlation
    dd_series = {}
    for c in labels:
        eq = (1 + R[c]).cumprod()
        dd_series[c] = (eq - eq.cummax()) / eq.cummax()
    dd_corr = pd.DataFrame(dd_series).corr()
    # monthly return correlation
    monthly = (1 + R).resample("ME").prod() - 1
    monthly_corr = monthly.corr()

    high_pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            high_pairs.append((labels[i], labels[j], float(daily_corr.iloc[i, j])))
    high_pairs.sort(key=lambda x: abs(x[2]), reverse=True)

    # ---- Phase 2: risk contribution at equal weight ----
    w_eq = np.ones(n) / n
    rc_eq = risk_contrib(R, w_eq)
    per_stats = {c: ann_stats(R[c]) for c in labels}

    # ---- Phase 3: weight schemes ----
    oos_mask = R.index >= OOS_START
    schemes = []
    schemes.append(scheme_row("Equal (1/n)", R, w_eq, oos_mask))
    schemes.append(scheme_row("InverseVol", R, inv_vol_weights(R), oos_mask))
    w_erc = erc_weights(R)
    schemes.append(scheme_row("ERC / RiskParity", R, w_erc, oos_mask))
    schemes.append(scheme_row("MaxDiversification", R, max_div_weights(R), oos_mask))
    # Vol target 10% annual via scaling ERC (report leverage, cap 1.0 gross for paper)
    erc_vol = ann_stats(port_returns(R, w_erc))["vol"]
    lev = min(1.0, 10.0 / erc_vol) if erc_vol > 0 else 1.0
    schemes.append(scheme_row(f"VolTarget10%(ERC*{lev:.2f})", R, w_erc * lev, oos_mask))

    # ---- Phase 4: stress ----
    # leave-one-out on ERC
    loo = []
    for k in range(n):
        keep = [i for i in range(n) if i != k]
        Rk = R.iloc[:, keep]
        wk = erc_weights(Rk)
        st = ann_stats(port_returns(Rk, wk))
        loo.append({"dropped": labels[k], **st})
    # MiMo sweep 0..30%
    mimo_idx = next((i for i, c in enumerate(labels) if "Mean-Reversion" in c or "MiMo" in c), None)
    sweep = []
    if mimo_idx is not None:
        others = [i for i in range(n) if i != mimo_idx]
        for wm in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
            w = np.zeros(n)
            w[mimo_idx] = wm
            rest = (1 - wm)
            # distribute rest by ERC among others
            Ro = R.iloc[:, others]
            wo = erc_weights(Ro) * rest
            for pos, i in enumerate(others):
                w[i] = wo[pos]
            st = ann_stats(port_returns(R, w))
            sweep.append({"mimo_w": wm, **st})
    # regimes by SPY 200d trend
    spy = ohlcv("SPY")
    regime_stats = {}
    if spy is not None:
        spx = spy["Close"].reindex(R.index).ffill()
        sma200 = spx.rolling(200).mean()
        ret20 = spx.pct_change(20)
        up = (spx > sma200) & (ret20 > 0.02)
        down = (spx < sma200) & (ret20 < -0.02)
        chop = ~(up | down)
        for label, mask in (("up", up), ("down", down), ("chop", chop)):
            pr = port_returns(R, w_erc).loc[mask.fillna(False)]
            regime_stats[label] = {"days": int(mask.sum()), **ann_stats(pr)}

    # ---------- report ----------
    def w_map(w):
        return {labels[i]: round(float(w[i]) * 100, 1) for i in range(n)}

    md = ["# Portfolio-Layer Analysis (7 paper strategies)", "",
          f"- Daily-return replay, {R.index[0].date()}..{R.index[-1].date()}, "
          f"{len(R)} days, fees {FEES}, each strategy = equal-weight over its basket", "",
          "## Phase 1 — Correlation", "",
          "### Top pairwise daily correlations"]
    md += ["| A | B | daily corr |", "|---|---|---:|"]
    for a, b, c in high_pairs[:12]:
        md.append(f"| {a[:34]} | {b[:34]} | {c:+.2f} |")
    md += ["", "### Monthly-return correlation (diag=1)", "",
           "```", monthly_corr.round(2).to_string(), "```", "",
           "### Drawdown correlation", "", "```", dd_corr.round(2).to_string(), "```", ""]

    md += ["## Phase 2 — Risk contribution @ equal weight", "",
           "| Strategy | Ret% | Vol% | Weight% | RiskContrib% |", "|---|---:|---:|---:|---:|"]
    for i, c in enumerate(labels):
        md.append(f"| {c[:40]} | {per_stats[c]['ret']:.1f} | {per_stats[c]['vol']:.1f} "
                  f"| {w_eq[i]*100:.1f} | {rc_eq['pct_rc'][i]*100:.1f} |")
    md += [f"\nPortfolio vol @ equal weight: {rc_eq['pvol']:.1f}%", ""]

    md += ["## Phase 3 — Weight schemes", "",
           "| Scheme | Full Sharpe | Full Ret% | Full Vol% | Full MaxDD% | OOS Sharpe | OOS MaxDD% |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for s in schemes:
        f_, o_ = s["full"], s["oos"]
        md.append(f"| {s['scheme']} | {f_['sharpe']:.2f} | {f_['ret']:.1f} | {f_['vol']:.1f} "
                  f"| {f_['maxdd']:.1f} | {o_['sharpe']:.2f} | {o_['maxdd']:.1f} |")
    md += ["", "### ERC / RiskParity weights", "",
           "| Strategy | Weight% |", "|---|---:|"]
    for c, v in w_map(w_erc).items():
        md.append(f"| {c[:40]} | {v} |")

    md += ["", "## Phase 4 — Stress tests", "",
           "### Leave-One-Out (ERC on remaining)", "",
           "| Dropped | Sharpe | Ret% | Vol% | MaxDD% |", "|---|---:|---:|---:|---:|"]
    base_sharpe = next(s["full"]["sharpe"] for s in schemes if s["scheme"].startswith("ERC"))
    for r in loo:
        md.append(f"| {r['dropped'][:40]} | {r['sharpe']:.2f} | {r['ret']:.1f} | {r['vol']:.1f} | {r['maxdd']:.1f} |")
    md.append(f"\n(ERC full-book Sharpe = {base_sharpe:.2f}; lower after-drop = that strategy helps)")
    if sweep:
        md += ["", "### MiMo weight sweep (rest = ERC)", "",
               "| MiMo w% | Sharpe | Ret% | Vol% | MaxDD% |", "|---|---:|---:|---:|---:|"]
        for r in sweep:
            md.append(f"| {r['mimo_w']*100:.0f} | {r['sharpe']:.2f} | {r['ret']:.1f} | {r['vol']:.1f} | {r['maxdd']:.1f} |")
    if regime_stats:
        md += ["", "### Regimes (ERC weights, SPY 200d trend)", "",
               "| Regime | Days | Sharpe | Ret% | MaxDD% |", "|---|---:|---:|---:|---:|"]
        for k, v in regime_stats.items():
            md.append(f"| {k} | {v['days']} | {v['sharpe']:.2f} | {v['ret']:.1f} | {v['maxdd']:.1f} |")

    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    OUT.write_text(json.dumps({
        "labels": labels,
        "daily_corr": daily_corr.round(4).to_dict(),
        "rolling60_avg_pair": {f"{a}|{b}": v for (a, b), v in roll_pairs.items()},
        "dd_corr": dd_corr.round(4).to_dict(),
        "monthly_corr": monthly_corr.round(4).to_dict(),
        "per_strategy_stats": per_stats,
        "risk_contrib_equal": {"pvol": rc_eq["pvol"],
                               "pct_rc": {labels[i]: float(rc_eq["pct_rc"][i]) for i in range(n)}},
        "schemes": [{"scheme": s["scheme"], "weights": w_map(np.array(s["weights"])),
                     "full": s["full"], "oos": s["oos"]} for s in schemes],
        "erc_weights": w_map(w_erc),
        "leave_one_out": loo,
        "mimo_sweep": sweep,
        "regimes": regime_stats,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # console summary
    print("\n=== TOP CORRELATIONS ===")
    for a, b, c in high_pairs[:8]:
        print(f"  {c:+.2f}  {a[:30]:<30} ~ {b[:30]}")
    print("\n=== RISK CONTRIB @ EQUAL WEIGHT ===")
    for i, c in enumerate(labels):
        print(f"  {c[:38]:<38} vol={per_stats[c]['vol']:5.1f}%  RC={rc_eq['pct_rc'][i]*100:5.1f}%")
    print(f"  portfolio vol = {rc_eq['pvol']:.1f}%")
    print("\n=== SCHEMES (full / OOS Sharpe) ===")
    for s in schemes:
        print(f"  {s['scheme']:<26} full S={s['full']['sharpe']:.2f} dd={s['full']['maxdd']:.1f}  "
              f"OOS S={s['oos']['sharpe']:.2f} dd={s['oos']['maxdd']:.1f}")
    print("\n=== ERC WEIGHTS ===")
    for c, v in w_map(w_erc).items():
        print(f"  {c[:40]:<40} {v:5.1f}%")
    print("\n=== LEAVE-ONE-OUT (ERC) ===  base=%.2f" % base_sharpe)
    for r in loo:
        print(f"  drop {r['dropped'][:34]:<34} S={r['sharpe']:.2f} dd={r['maxdd']:.1f}")
    if sweep:
        print("\n=== MiMo SWEEP ===")
        for r in sweep:
            print(f"  w={r['mimo_w']*100:4.0f}%  S={r['sharpe']:.2f} ret={r['ret']:.1f} vol={r['vol']:.1f} dd={r['maxdd']:.1f}")
    if regime_stats:
        print("\n=== REGIMES (ERC) ===")
        for k, v in regime_stats.items():
            print(f"  {k:<5} days={v['days']:4d} S={v['sharpe']:.2f} ret={v['ret']:.1f} dd={v['maxdd']:.1f}")
    print(f"\nWrote {OUT} and {OUT_MD}")


if __name__ == "__main__":
    main()
