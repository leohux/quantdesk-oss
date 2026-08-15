#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Disable Cursor-Surge, recompute ERC on remaining 3, write V2 allocation report.

Does NOT delete the strategy. Does NOT place/cancel orders.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/app")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.store import list_strategies, update_strategy, get_strategy
import portfolio_analysis as pa

SURGE_NAME_HINT = "Cursor-Surge"
DISABLED_AT = "2026-07-29"
REASON = "holdout degradation"
START = "2022-01-01"
HOLDOUT_START = "2025-07-01"
VALID_START = "2024-01-01"

OUT_MD = Path("/app/data/store/portfolio_v2_allocation.md")
OUT_JSON = Path("/app/data/store/portfolio_v2_allocation.json")

# Reactivation gates (documented; not auto-enforced yet)
REACTIVATION = {
    "min_trading_days": 60,
    "min_sharpe": 0.5,
    "min_profit_factor": 1.2,
    "max_dd": 0.15,
    "scope": "paper/live shadow",
}


def find_surge() -> dict:
    for s in list_strategies():
        name = s.get("name") or ""
        if SURGE_NAME_HINT in name and s.get("enabled"):
            return s
    # fallback: any Cursor-Surge even if already disabled
    for s in list_strategies():
        if SURGE_NAME_HINT in (s.get("name") or ""):
            return s
    raise KeyError("Cursor-Surge not found")


def rich_stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 5 or float(r.std()) == 0:
        return {"cagr": 0.0, "sharpe": 0.0, "maxdd": 0.0, "vol": 0.0,
                "monthly_win_rate": 0.0, "total_return": 0.0}
    eq = (1 + r).cumprod()
    dd = (eq - eq.cummax()) / eq.cummax()
    monthly = (1 + r).resample("ME").prod() - 1
    return {
        "cagr": float((eq.iloc[-1] ** (252 / len(r)) - 1) * 100),
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)),
        "maxdd": float(abs(dd.min()) * 100),
        "vol": float(r.std() * np.sqrt(252) * 100),
        "monthly_win_rate": float((monthly > 0).mean() * 100) if len(monthly) else 0.0,
        "total_return": float((eq.iloc[-1] - 1) * 100),
    }


def risk_table(R: pd.DataFrame, w: np.ndarray, labels: list[str]) -> list[dict]:
    rc = pa.risk_contrib(R, w)
    rows = []
    for i, name in enumerate(labels):
        st = pa.ann_stats(R.iloc[:, i])
        rows.append({
            "name": name,
            "weight_pct": round(float(w[i]) * 100, 1),
            "ret": st["ret"],
            "vol": st["vol"],
            "sharpe": st["sharpe"],
            "maxdd": st["maxdd"],
            "risk_contrib_pct": round(float(rc["pct_rc"][i]) * 100, 1),
        })
    return rows


def main():
    surge = find_surge()
    sid = surge["id"]
    print(f"Target: {surge.get('name')} ({sid}) enabled={surge.get('enabled')}", flush=True)

    # ---- Step 1: disable (keep record; do NOT delete) ----
    # store.py forces status=stopped when enabled=False, so set lifecycle in a
    # second patch without touching enabled.
    update_strategy(
        sid,
        {
            "enabled": False,
            "disabled_reason": REASON,
            "disabled_at": DISABLED_AT,
            "reactivation_gates": REACTIVATION,
            "params": {
                "portfolio_weight": 0.0,
                "lifecycle": "DISABLED",
                "disabled_reason": REASON,
                "disabled_at": DISABLED_AT,
            },
        },
    )
    updated = update_strategy(
        sid,
        {
            "lifecycle": "DISABLED",
            "status": "DISABLED",
        },
    )
    print(
        f"  disabled → enabled={updated.get('enabled')} status={updated.get('status')} "
        f"lifecycle={updated.get('lifecycle')} reason={updated.get('disabled_reason')}",
        flush=True,
    )

    # ---- Step 2: ERC on remaining enabled daily book ----
    book = []
    series = []
    for s in list_strategies():
        if not s.get("enabled"):
            continue
        name = s.get("name") or s["id"]
        if "news" in name.lower():
            continue
        r = pa.strategy_daily_returns(s)
        if r is None:
            print(f"  skip (no returns): {name}", flush=True)
            continue
        book.append(s)
        series.append(r.rename(name))
        print(f"  book member: {name}", flush=True)

    if len(book) < 2:
        raise RuntimeError(f"need >=2 strategies for ERC, got {len(book)}")

    R = pd.concat(series, axis=1).dropna(how="all").fillna(0.0)
    R = R.loc[R.index >= pd.Timestamp(START)]
    labels = list(R.columns)
    w = pa.erc_weights(R)

    print("\nERC V2 weights:", flush=True)
    for i, s in enumerate(book):
        wt = round(float(w[i]), 4)
        update_strategy(s["id"], {"params": {"portfolio_weight": wt}})
        print(f"  {wt*100:5.1f}%  {s.get('name')}", flush=True)

    # ---- Step 3: V2 allocation report ----
    pr = pa.port_returns(R, w)
    full = rich_stats(pr)
    valid = rich_stats(pr.loc[(R.index >= pd.Timestamp(VALID_START)) & (R.index < pd.Timestamp(HOLDOUT_START))])
    holdout = rich_stats(pr.loc[R.index >= pd.Timestamp(HOLDOUT_START)])
    rc_rows = risk_table(R, w, labels)
    corr = R.corr()

    # concentration flags
    max_w = max(float(x) for x in w)
    max_rc = max(float(r["risk_contrib_pct"]) for r in rc_rows)
    flags = []
    if max_w > 0.50:
        flags.append(f"单策略权重集中: max_w={max_w:.0%}")
    if max_rc > 45:
        flags.append(f"风险贡献集中: max_RC={max_rc:.1f}%")
    # pairwise corr
    high_corr = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            c = float(corr.iloc[i, j])
            if abs(c) >= 0.60:
                high_corr.append((labels[i], labels[j], c))

    # symbol exposure (unique vs overlapping)
    sym_sets = {}
    for s in book:
        syms = [str(x).upper() for x in (s.get("params") or {}).get("symbols") or []]
        sym_sets[s.get("name")] = set(syms)
    all_syms = set().union(*sym_sets.values()) if sym_sets else set()
    overlap = {}
    names = list(sym_sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            inter = sym_sets[names[i]] & sym_sets[names[j]]
            if inter:
                overlap[f"{names[i][:28]} ∩ {names[j][:28]}"] = sorted(inter)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action": "disable_cursor_surge_recompute_erc",
        "disabled": {
            "id": sid,
            "name": surge.get("name"),
            "reason": REASON,
            "disabled_at": DISABLED_AT,
            "lifecycle": "DISABLED",
            "reactivation_gates": REACTIVATION,
        },
        "book_size": len(book),
        "weights": {labels[i]: round(float(w[i]) * 100, 1) for i in range(len(labels))},
        "risk_contrib": rc_rows,
        "portfolio": {"full": full, "valid": valid, "holdout": holdout},
        "correlation": corr.round(3).to_dict(),
        "high_corr_pairs": [{"a": a, "b": b, "corr": round(c, 3)} for a, b, c in high_corr],
        "symbol_universe_size": len(all_syms),
        "symbol_overlaps": {k: v for k, v in overlap.items()},
        "flags": flags,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Portfolio V2 Allocation Report",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Action: Disable **{surge.get('name')}** → ERC on {len(book)} remaining strategies",
        f"- Reason: `{REASON}` @ {DISABLED_AT}",
        "- Strategy kept on disk (not deleted); lifecycle=`DISABLED`",
        "",
        "## Disabled strategy",
        "",
        f"- id: `{sid}`",
        f"- lifecycle: DISABLED",
        f"- reactivation gates: ≥{REACTIVATION['min_trading_days']} days, "
        f"Sharpe>{REACTIVATION['min_sharpe']}, PF>{REACTIVATION['min_profit_factor']}, "
        f"MaxDD<{REACTIVATION['max_dd']:.0%}",
        "",
        "## V2 ERC Weights",
        "",
        "| Strategy | Weight% | Ret% | Vol% | Sharpe | MaxDD% | RiskContrib% |",
        "|:--|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rc_rows:
        lines.append(
            f"| {r['name'][:48]} | {r['weight_pct']} | {r['ret']:.1f} | {r['vol']:.1f} | "
            f"{r['sharpe']:.2f} | {r['maxdd']:.1f} | {r['risk_contrib_pct']} |"
        )

    lines += [
        "",
        "## Portfolio quality (after disable)",
        "",
        "| Window | CAGR% | Sharpe | MaxDD% | Vol% | 月胜率% |",
        "|:--|---:|---:|---:|---:|---:|",
        f"| Full | {full['cagr']:.1f} | {full['sharpe']:.2f} | {full['maxdd']:.1f} | {full['vol']:.1f} | {full['monthly_win_rate']:.0f} |",
        f"| Valid 2024-01..2025-07 | {valid['cagr']:.1f} | {valid['sharpe']:.2f} | {valid['maxdd']:.1f} | {valid['vol']:.1f} | {valid['monthly_win_rate']:.0f} |",
        f"| Holdout 2025-07..now | {holdout['cagr']:.1f} | {holdout['sharpe']:.2f} | {holdout['maxdd']:.1f} | {holdout['vol']:.1f} | {holdout['monthly_win_rate']:.0f} |",
        "",
        "## Correlation matrix",
        "",
        "```",
        corr.round(2).to_string(),
        "```",
        "",
    ]
    if high_corr:
        lines += ["### High |corr| ≥ 0.60", ""]
        for a, b, c in high_corr:
            lines.append(f"- {c:+.2f}  {a[:36]} ~ {b[:36]}")
        lines.append("")
    else:
        lines += ["### High |corr| ≥ 0.60", "", "- none", ""]

    lines += ["## Symbol overlap", ""]
    if overlap:
        for k, v in overlap.items():
            lines.append(f"- {k}: {', '.join(v)}")
    else:
        lines.append("- no overlapping symbols across strategies")
    lines.append("")

    lines += ["## Concentration / risk flags", ""]
    if flags:
        for f in flags:
            lines.append(f"- ⚠ {f}")
    else:
        lines.append("- none (no single weight >50%, no RC >45%)")
    lines += [
        "",
        "## Verdict",
        "",
        "Cursor-Surge moved to DISABLED. Remaining book reweighted by ERC.",
        "Compare Holdout Sharpe/MaxDD to pre-disable A scenario (Surge≈15%): "
        "target was Sharpe↑ and MaxDD↓ — confirm in numbers above.",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("\nWrote", OUT_MD, OUT_JSON, flush=True)
    print(OUT_MD.read_text(encoding="utf-8"), flush=True)

    # final enabled dump
    print("\n=== enabled now ===", flush=True)
    for x in list_strategies():
        if x.get("enabled"):
            p = x.get("params") or {}
            print(f"  {x.get('name')} | w={p.get('portfolio_weight')} | status={x.get('status')}", flush=True)
    s2 = get_strategy(sid)
    print(
        f"\n=== surge freeze === enabled={s2.get('enabled')} status={s2.get('status')} "
        f"lifecycle={s2.get('lifecycle')} reason={s2.get('disabled_reason')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
