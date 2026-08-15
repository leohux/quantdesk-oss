#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor-Surge portfolio weight impact (A/B/C) + archival -6/+30 holdout.

Priority-1 experiment: does reducing / removing Cursor-Surge improve the book?
NOT a parameter search. Does not write any strategy state.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/app")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.store import list_strategies, get_strategy, ensure_strategy_code
from data.loader import load_ohlcv
from backtest.runner import run_signal_backtest

# reuse daily-return reconstruction from existing portfolio analysis
import portfolio_analysis as pa

START = "2022-01-01"
HOLDOUT_START = "2025-07-01"
VALID_START = "2024-01-01"
OUT_MD = Path("/app/data/store/surge_weight_impact.md")
OUT_JSON = Path("/app/data/store/surge_weight_impact.json")
OUT_ARCH = Path("/app/data/store/rr_holdout_minus6_plus30.md")


def rich_stats(r: pd.Series) -> dict:
    r = r.dropna()
    empty = {
        "cagr": 0.0,
        "sharpe": 0.0,
        "maxdd": 0.0,
        "vol": 0.0,
        "monthly_win_rate": 0.0,
        "dd_recovery_days": None,
        "days": 0,
        "total_return": 0.0,
    }
    if len(r) < 5 or float(r.std()) == 0:
        return empty
    eq = (1 + r).cumprod()
    dd = (eq - eq.cummax()) / eq.cummax()
    maxdd = float(abs(dd.min()))
    # recovery: days from max-dd trough back to prior peak (or None if not recovered)
    trough_i = int(dd.argmin())
    peak_before = float(eq.iloc[: trough_i + 1].cummax().iloc[-1]) if trough_i >= 0 else float(eq.iloc[0])
    rec = None
    after = eq.iloc[trough_i:]
    recovered = after[after >= peak_before]
    if len(recovered):
        rec = int((recovered.index[0] - after.index[0]).days)
    monthly = (1 + r).resample("ME").prod() - 1
    mwr = float((monthly > 0).mean()) if len(monthly) else 0.0
    return {
        "cagr": float((eq.iloc[-1] ** (252 / len(r)) - 1) * 100),
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)),
        "maxdd": maxdd * 100,
        "vol": float(r.std() * np.sqrt(252) * 100),
        "monthly_win_rate": mwr * 100,
        "dd_recovery_days": rec,
        "days": int(len(r)),
        "total_return": float((eq.iloc[-1] - 1) * 100),
    }


def find_surge_col(labels: list[str]) -> str:
    for c in labels:
        if "Cursor-Surge" in c or "Surge" in c:
            return c
    raise KeyError(f"Cursor-Surge not in {labels}")


def build_weights(labels: list[str], surge_col: str, mode: str, surge_w: float | None = None) -> np.ndarray:
    """
    mode:
      A current — use stored portfolio_weight if present, else ERC
      B fixed surge weight, rest ERC renormalized
      C remove surge, ERC on remaining
    """
    n = len(labels)
    R = _R  # global filled in main
    surge_i = labels.index(surge_col)

    if mode == "A":
        # prefer stored weights on enabled book
        w = np.zeros(n)
        items = {s.get("name"): s for s in list_strategies() if s.get("enabled")}
        got = 0.0
        for i, name in enumerate(labels):
            s = items.get(name) or {}
            pw = (s.get("params") or {}).get("portfolio_weight")
            if pw is not None:
                w[i] = float(pw)
                got += float(pw)
        if got > 0.5:
            w = w / w.sum()
            return w
        return pa.erc_weights(R)

    if mode == "C":
        keep = [i for i in range(n) if i != surge_i]
        wk = pa.erc_weights(R.iloc[:, keep])
        w = np.zeros(n)
        for pos, i in enumerate(keep):
            w[i] = wk[pos]
        return w

    # B: surge fixed, rest ERC
    assert surge_w is not None
    others = [i for i in range(n) if i != surge_i]
    Ro = R.iloc[:, others]
    wo = pa.erc_weights(Ro) * (1.0 - surge_w)
    w = np.zeros(n)
    w[surge_i] = surge_w
    for pos, i in enumerate(others):
        w[i] = wo[pos]
    return w


_R: pd.DataFrame = pd.DataFrame()


def scenario_row(name: str, R: pd.DataFrame, w: np.ndarray, masks: dict) -> dict:
    pr = pa.port_returns(R, w)
    out = {
        "name": name,
        "weights": {R.columns[i]: round(float(w[i]) * 100, 1) for i in range(len(w))},
        "full": rich_stats(pr),
    }
    for k, mask in masks.items():
        out[k] = rich_stats(pr.loc[mask])
    return out


def fmt_stats(st: dict) -> str:
    rec = st["dd_recovery_days"]
    rec_s = f"{rec}d" if rec is not None else "未恢复"
    return (
        f"CAGR={st['cagr']:.1f}% Sharpe={st['sharpe']:.2f} MaxDD={st['maxdd']:.1f}% "
        f"月胜率={st['monthly_win_rate']:.0f}% 回撤恢复={rec_s}"
    )


def archival_minus6_plus30():
    """Post-analysis only: -6/+30 on untouched holdout. Not for selection."""
    sid = None
    for s in list_strategies():
        if s.get("enabled") and "Cursor-Surge" in (s.get("name") or ""):
            sid = s["id"]
            break
    if not sid:
        return None
    s = get_strategy(sid)
    code = ensure_strategy_code(s)
    base = dict(s.get("params") or {})
    symbols = [str(x).upper() for x in (base.get("symbols") or [])]

    combos = [
        (-0.06, 0.30, "champion-archive"),
        (-0.08, 0.15, "baseline"),
        (-0.04, 0.08, "gate-pass-top1"),
    ]
    rows = []
    for sl, tp, tag in combos:
        params = deepcopy(base)
        params["stop_loss"] = sl
        params["take_profit"] = tp
        mets = []
        for sym in symbols:
            try:
                df = load_ohlcv(sym, start="2024-06-01")
                hdf = df.loc[HOLDOUT_START:]
                if len(hdf) < 40:
                    continue
                res = run_signal_backtest(hdf, code, params)
                dd = abs(float(res.get("max_drawdown_pct") or 0))
                if dd > 1:
                    dd /= 100.0
                mets.append(
                    {
                        "symbol": sym,
                        "sharpe": float(res.get("sharpe") or 0),
                        "ret": float(res.get("total_return") or 0),
                        "dd": dd,
                        "trades": float(res.get("trades") or 0),
                        "pf": float(res.get("profit_factor") or 0)
                        if res.get("profit_factor") not in (None, float("inf"))
                        else 10.0,
                    }
                )
            except Exception as e:
                print(f"  skip {sym}: {e}", flush=True)
        if not mets:
            continue
        rows.append(
            {
                "tag": tag,
                "sl": sl,
                "tp": tp,
                "sharpe": float(np.mean([m["sharpe"] for m in mets])),
                "ret": float(np.mean([m["ret"] for m in mets])),
                "dd": float(np.mean([m["dd"] for m in mets])),
                "trades": float(sum(m["trades"] for m in mets)),
                "pf": float(np.mean([m["pf"] for m in mets if m["pf"] == m["pf"]])),
                "pos_frac": float(np.mean([1.0 if m["ret"] > 0 else 0.0 for m in mets])),
            }
        )
    lines = [
        "# Archival: -6/+30 holdout (post-analysis, NOT for selection)",
        "",
        f"- Holdout: {HOLDOUT_START}..now",
        "- Purpose: did the former champion retain any edge without selection bias?",
        "",
        "| Tag | SL | TP | Sharpe | Ret | MaxDD | Trades | PF | 正收益占比 |",
        "|:--|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['tag']} | {r['sl']:.0%} | {r['tp']:.0%} | {r['sharpe']:.3f} | "
            f"{r['ret']:.3f} | {r['dd']:.1%} | {r['trades']:.0f} | {r['pf']:.2f} | "
            f"{r['pos_frac']:.0%} |"
        )
    OUT_ARCH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines), flush=True)
    return rows


def main():
    global _R
    items = [x for x in list_strategies() if x.get("enabled")]
    # daily book only — skip pure intraday if it has no daily symbols basket usable
    names, series = [], []
    for s in items:
        name = s.get("name") or s["id"]
        # skip news / intraday morning if they don't produce daily strategy returns cleanly
        if "news" in name.lower():
            continue
        r = pa.strategy_daily_returns(s)
        if r is None:
            print("skip (no returns):", name, flush=True)
            continue
        names.append(name)
        series.append(r.rename(name))
        print(f"  loaded returns: {name}", flush=True)

    R = pd.concat(series, axis=1).dropna(how="all").fillna(0.0)
    R = R.loc[R.index >= pd.Timestamp(START)]
    _R = R
    labels = list(R.columns)
    surge = find_surge_col(labels)
    print(f"\nbook={len(labels)} surge={surge} days={len(R)} "
          f"{R.index[0].date()}..{R.index[-1].date()}", flush=True)

    masks = {
        "valid": (R.index >= pd.Timestamp(VALID_START)) & (R.index < pd.Timestamp(HOLDOUT_START)),
        "holdout": R.index >= pd.Timestamp(HOLDOUT_START),
        "since_2024": R.index >= pd.Timestamp(VALID_START),
    }

    scenarios = [
        ("A 当前组合 (stored/ERC)", build_weights(labels, surge, "A")),
        ("B Surge=20% 其余ERC", build_weights(labels, surge, "B", 0.20)),
        ("B Surge=10% 其余ERC", build_weights(labels, surge, "B", 0.10)),
        ("C 完全移除 Surge (ERC)", build_weights(labels, surge, "C")),
    ]

    rows = []
    for name, w in scenarios:
        row = scenario_row(name, R, w, masks)
        rows.append(row)
        print(f"\n=== {name} ===", flush=True)
        print("  weights:", row["weights"], flush=True)
        print("  FULL   ", fmt_stats(row["full"]), flush=True)
        print("  VALID  ", fmt_stats(row["valid"]), flush=True)
        print("  HOLDOUT", fmt_stats(row["holdout"]), flush=True)

    # decision helpers vs A
    base = rows[0]
    verdict_lines = []
    for row in rows[1:]:
        d_s = row["holdout"]["sharpe"] - base["holdout"]["sharpe"]
        d_dd = row["holdout"]["maxdd"] - base["holdout"]["maxdd"]
        d_sf = row["full"]["sharpe"] - base["full"]["sharpe"]
        d_ddf = row["full"]["maxdd"] - base["full"]["maxdd"]
        verdict_lines.append(
            f"- vs A | {row['name']}: holdout ΔSharpe={d_s:+.2f} ΔMaxDD={d_dd:+.1f}pp; "
            f"full ΔSharpe={d_sf:+.2f} ΔMaxDD={d_ddf:+.1f}pp"
        )

    # markdown
    lines = [
        "# Cursor-Surge 组合降权/移除影响",
        "",
        f"- Period: {R.index[0].date()}..{R.index[-1].date()}",
        f"- VALID: {VALID_START}..{HOLDOUT_START}",
        f"- HOLDOUT: {HOLDOUT_START}..now",
        f"- Surge column: `{surge}`",
        "- Method: daily long/flat replay (same as portfolio_analysis), fees 0.1%",
        "- 这不是参数搜索；只比较组合权重",
        "",
        "## 场景对比",
        "",
        "| 场景 | Full CAGR | Full Sharpe | Full MaxDD | Full 月胜率 | Full 回撤恢复 | "
        "Holdout Sharpe | Holdout MaxDD | Holdout 月胜率 | Holdout 回撤恢复 |",
        "|:--|---:|---:|---:|---:|:--|---:|---:|---:|:--|",
    ]
    for row in rows:
        f, h = row["full"], row["holdout"]
        fr = f"{f['dd_recovery_days']}d" if f["dd_recovery_days"] is not None else "未恢复"
        hr = f"{h['dd_recovery_days']}d" if h["dd_recovery_days"] is not None else "未恢复"
        lines.append(
            f"| {row['name']} | {f['cagr']:.1f}% | {f['sharpe']:.2f} | {f['maxdd']:.1f}% | "
            f"{f['monthly_win_rate']:.0f}% | {fr} | {h['sharpe']:.2f} | {h['maxdd']:.1f}% | "
            f"{h['monthly_win_rate']:.0f}% | {hr} |"
        )
    lines += ["", "## 权重明细", ""]
    for row in rows:
        lines.append(f"### {row['name']}")
        lines.append("| Strategy | Weight% |")
        lines.append("|:--|---:|")
        for k, v in row["weights"].items():
            lines.append(f"| {k} | {v} |")
        lines.append("")
    lines += ["## 相对 A 的变化", ""] + verdict_lines
    lines += [
        "",
        "## 决策规则（按用户定义）",
        "",
        "- 若移除后 Holdout Sharpe ↑ 且 MaxDD ↓ → **停用**，不是降权",
        "- 若降权后 DD 改善但 Sharpe 明显掉 → 保留小仓位观察",
        "- 若三者差不多 → 优先简化（移除），避免无效复杂度",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nWrote", OUT_MD, OUT_JSON, flush=True)
    print(OUT_MD.read_text(encoding="utf-8"), flush=True)

    print("\n===== ARCHIVAL holdout -6/+30 =====", flush=True)
    archival_minus6_plus30()


if __name__ == "__main__":
    main()
