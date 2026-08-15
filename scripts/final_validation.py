# -*- coding: utf-8 -*-
"""Final Validation (one-shot): revalidate Top-N Promote vs current paper 6.

- NO new candidates, NO parameter search. Fixed params only.
- Same IS/OOS engine as hybrid_oos_shortlist / cursor_classic_oos.
- Multi-symbol basket, two periods, one unified leaderboard.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import run_signal_backtest
from config.store import get_strategy, list_strategies
from data.loader import load_ohlcv

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "PLTR", "HOOD", "SOFI"]
IS = ("2022-01-01", "2023-12-31")
OOS = ("2024-01-01", None)
TOP_N = 50
PER_FAMILY_CAP = 8          # keep leaderboard diverse, not 50 RSITrend-SPY clones
OUT = Path("/app/data/store/final_validation.json")
OUT_MD = Path("/app/data/store/final_validation.md")

CACHE: dict = {}

FAMILY_TAGS = (
    "RSITrend", "DualMom", "VolMom", "TrendMom", "ChanRSI", "Surge", "Squeeze",
    "Turtle", "Connors", "AbsMom", "Golden", "TSMOM", "ATRBreak", "BBSqueeze",
    "InvDonchian", "Pullback", "BBTrend", "MeanRev", "MA", "BB", "DON", "RSI",
)


def all_strategies() -> list[dict]:
    items = list_strategies(mined_only=False)
    if isinstance(items, dict):
        items = items.get("strategies") or items.get("items") or []
    return items


def is_mined(s: dict) -> bool:
    sid = str(s.get("id") or "")
    name = str(s.get("name") or "")
    return sid.startswith(("alpha-", "hybrid-", "cursor-", "mimo-")) or name.startswith(
        ("Alpha-", "Hybrid-", "Cursor-", "MiMo-", "Classic-")
    )


def family(s: dict) -> str:
    typ = str(s.get("type") or "")
    if typ.startswith(("classic_", "hybrid_", "alpha_")):
        return typ
    name = str(s.get("name") or "")
    for tag in FAMILY_TAGS:
        if tag.lower() in name.lower():
            return "fam_" + tag.lower()
    return typ or "other"


def is_sharpe(s: dict) -> float:
    m = s.get("metrics") or {}
    for k in ("sharpe", "IS_sharpe", "sharpe_ratio"):
        v = m.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def dedupe_key(s: dict):
    m = s.get("metrics") or {}
    return (
        family(s),
        round(is_sharpe(s), 2),
        round(float(m.get("total_return") or m.get("total_return_pct") or 0), 2),
        round(float(m.get("max_drawdown") or m.get("max_drawdown_pct") or 0), 1),
        tuple((s.get("params") or {}).get("symbols") or []),
    )


def load_code(sid: str) -> str:
    p = Path(f"/app/data/store/strategy_code/{sid}.py")
    if p.exists():
        return p.read_text(encoding="utf-8")
    try:
        return get_strategy(sid).get("code") or ""
    except Exception:
        return ""


def prewarm_cache() -> None:
    for period in (IS, OOS):
        for sym in SYMBOLS:
            key = (sym, period[0], period[1])
            if key not in CACHE:
                try:
                    CACHE[key] = load_ohlcv(sym, start=period[0], end=period[1])
                except Exception as e:
                    CACHE[key] = e


def bt(code, params, symbol, period):
    key = (symbol, period[0], period[1])
    df = CACHE.get(key)
    if isinstance(df, Exception) or df is None:
        raise RuntimeError(f"no data {symbol}: {df}")
    return run_signal_backtest(df, code, params, init_cash=100_000, fees=0.001)


def eval_period(code, params, period) -> dict:
    rows = []
    for sym in SYMBOLS:
        try:
            m = bt(code, params, sym, period)
            rows.append({
                "symbol": sym,
                "sharpe": float(m.get("sharpe") or 0),
                "ret": float(m.get("total_return_pct") or 0),
                "dd": float(m.get("max_drawdown_pct") or 0),
                "trades": int(m.get("trades") or 0),
            })
        except Exception as e:
            rows.append({"symbol": sym, "sharpe": 0, "ret": 0, "dd": 99, "trades": 0, "error": str(e)[:100]})
    sharpes = [r["sharpe"] for r in rows]
    return {
        "avg_sharpe": statistics.mean(sharpes),
        "median_sharpe": statistics.median(sharpes),
        "min_sharpe": min(sharpes),
        "positive": sum(1 for r in rows if r["ret"] > 0),
        "avg_dd": statistics.mean(r["dd"] for r in rows),
        "avg_trades": statistics.mean(r["trades"] for r in rows),
        "rows": rows,
    }


def score(s: dict) -> dict:
    code = load_code(s["id"])
    params = dict(s.get("params") or {})
    params.pop("symbols", None)
    is_stats = eval_period(code, params, IS)
    oos_stats = eval_period(code, params, OOS)
    is_s = is_stats["avg_sharpe"]
    oos_s = oos_stats["avg_sharpe"]
    collapse = (oos_s / is_s) if is_s > 0.2 else (1.0 if oos_s > 0 else 0.0)
    verdict_score = oos_s + 0.25 * min(collapse, 1.5) + 0.05 * oos_stats["positive"]
    return {
        "id": s["id"],
        "name": s.get("name") or s["id"],
        "family": family(s),
        "orig_is_sharpe": is_sharpe(s),
        "is_avg_sharpe": is_s,
        "oos_avg_sharpe": oos_s,
        "oos_over_is": collapse,
        "oos_min_sharpe": oos_stats["min_sharpe"],
        "oos_positive": oos_stats["positive"],
        "avg_dd": oos_stats["avg_dd"],
        "avg_trades": oos_stats["avg_trades"],
        "verdict_score": verdict_score,
        "is": is_stats,
        "oos": oos_stats,
    }


def pick_top(items: list[dict]) -> list[dict]:
    mined = [s for s in items if is_mined(s)]
    mined.sort(key=is_sharpe, reverse=True)
    seen = set()
    fam_count: dict[str, int] = {}
    picked: list[dict] = []
    for s in mined:
        k = dedupe_key(s)
        if k in seen:
            continue
        fam = family(s)
        if fam_count.get(fam, 0) >= PER_FAMILY_CAP:
            continue
        seen.add(k)
        fam_count[fam] = fam_count.get(fam, 0) + 1
        picked.append(s)
        if len(picked) >= TOP_N:
            break
    return picked


def main():
    t0 = time.time()
    items = all_strategies()
    enabled = [s for s in items if s.get("enabled")]
    enabled_ids = {s["id"] for s in enabled}

    top = pick_top(items)
    # ensure current-6 are always evaluated as baseline (even if not in Top-N)
    eval_set = {s["id"]: s for s in top}
    for s in enabled:
        eval_set.setdefault(s["id"], s)
    cand = list(eval_set.values())
    print(f"prewarm cache ({len(SYMBOLS)}x2 frames)...", flush=True)
    prewarm_cache()
    print(f"evaluating {len(cand)} strategies (top={len(top)}, enabled={len(enabled)})", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(score, s): s for s in cand}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
                r["is_current_paper"] = r["id"] in enabled_ids
                results.append(r)
                if i % 10 == 0 or i == len(cand):
                    print(f"  {i}/{len(cand)} done", flush=True)
            except Exception as e:
                s = futs[fut]
                print("FAIL", s.get("name"), e, flush=True)

    # Baseline thresholds from current paper 6 (strict; don't loosen for high IS)
    base = [r for r in results if r["is_current_paper"]]
    base_min_oos = min((r["oos_avg_sharpe"] for r in base), default=0.45)
    base_max_dd = max((r["avg_dd"] for r in base), default=30.0)
    base_min_collapse = min((r["oos_over_is"] for r in base), default=0.5)

    MIN_TRADES = 15.0
    MIN_COLLAPSE = max(0.5, base_min_collapse * 0.9)

    for r in results:
        if r["is_current_paper"]:
            r["verdict"] = "KEEP"
            continue
        ok = (
            r["oos_avg_sharpe"] > base_min_oos
            and r["oos_over_is"] >= MIN_COLLAPSE
            and r["avg_dd"] <= base_max_dd * 1.10
            and r["avg_trades"] >= MIN_TRADES
            and r["oos_min_sharpe"] > -0.30
            and r["oos_positive"] >= 4
        )
        r["verdict"] = "REPLACE_CANDIDATE" if ok else "REJECT"

    results.sort(key=lambda r: r["oos_avg_sharpe"], reverse=True)

    replace = [r for r in results if r["verdict"] == "REPLACE_CANDIDATE"]

    def fmt(r):
        star = "*" if r["is_current_paper"] else " "
        return (
            f"{star} OOS={r['oos_avg_sharpe']:5.2f} IS={r['is_avg_sharpe']:5.2f} "
            f"O/I={r['oos_over_is']:4.2f} DD={r['avg_dd']:5.1f} "
            f"n={r['avg_trades']:5.1f} pos={r['oos_positive']}/8 "
            f"{r['verdict']:<17} {r['name'][:44]}"
        )

    print("\n===== UNIFIED LEADERBOARD (sorted by OOS Sharpe) =====")
    print(f"baseline: min_oos={base_min_oos:.2f} max_dd={base_max_dd:.1f} "
          f"min_collapse->{MIN_COLLAPSE:.2f} min_trades={MIN_TRADES:.0f}")
    print(f"(* = current paper strategy)\n")
    header = (
        f"{'Rank':<4} {'OOS':>5} {'IS':>5} {'O/I':>4} {'MaxDD':>6} {'Trades':>6} "
        f"{'Pos':>4} {'Verdict':<17} Strategy"
    )
    print(header)
    md = ["# Final Validation — Top-50 Promote vs Current Paper 6", "",
          f"- IS: {IS[0]}..{IS[1]}  OOS: {OOS[0]}..open  basket: {', '.join(SYMBOLS)}",
          f"- Baseline gate: OOS>{base_min_oos:.2f}, collapse>={MIN_COLLAPSE:.2f}, "
          f"DD<={base_max_dd*1.1:.1f}, trades>={MIN_TRADES:.0f}, minOOS>-0.30, pos>=4/8",
          "", "| Rank | Strategy | IS | OOS | OOS/IS | MaxDD | Trades | Pos | Verdict |",
          "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for i, r in enumerate(results, 1):
        tag = " (PAPER)" if r["is_current_paper"] else ""
        print(f"{i:<4}" + fmt(r))
        md.append(
            f"| {i} | {r['name'][:44]}{tag} | {r['is_avg_sharpe']:.2f} | "
            f"{r['oos_avg_sharpe']:.2f} | {r['oos_over_is']:.2f} | {r['avg_dd']:.1f} | "
            f"{r['avg_trades']:.0f} | {r['oos_positive']}/8 | {r['verdict']} |"
        )

    print("\n===== DECISION =====")
    if replace:
        print(f"{len(replace)} REPLACE_CANDIDATE(s) beat the weakest current paper strategy:")
        for r in replace:
            print(f"  -> {r['name']} OOS={r['oos_avg_sharpe']:.2f} O/I={r['oos_over_is']:.2f} "
                  f"DD={r['avg_dd']:.1f} n={r['avg_trades']:.1f}")
        decision = "REPLACE_SOME"
    else:
        print("No candidate cleanly beats the current 6 under strict OOS gates.")
        print("=> FREEZE this random-search round; shift to portfolio layer / new alpha families.")
        decision = "FREEZE"

    md += ["", "## Decision", ""]
    if replace:
        md.append(f"**REPLACE_SOME** — {len(replace)} candidate(s) clear the gate:")
        for r in replace:
            md.append(f"- {r['name']} — OOS {r['oos_avg_sharpe']:.2f}, O/I {r['oos_over_is']:.2f}, "
                      f"DD {r['avg_dd']:.1f}, trades {r['avg_trades']:.0f}")
    else:
        md.append("**FREEZE this search round.** No candidate beats the current 6 under strict "
                  "OOS/collapse/DD/trades gates. Shift effort to portfolio optimization "
                  "(correlation, risk budgeting, dynamic weights) and new alpha families.")

    OUT.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "is_period": IS, "oos_period": OOS, "symbols": SYMBOLS,
        "top_n": TOP_N, "per_family_cap": PER_FAMILY_CAP,
        "baseline": {"min_oos": base_min_oos, "max_dd": base_max_dd,
                     "min_collapse": MIN_COLLAPSE, "min_trades": MIN_TRADES},
        "decision": decision,
        "replace_candidates": [{"id": r["id"], "name": r["name"],
                                "oos": r["oos_avg_sharpe"], "is": r["is_avg_sharpe"],
                                "collapse": r["oos_over_is"], "dd": r["avg_dd"],
                                "trades": r["avg_trades"]} for r in replace],
        "leaderboard": [{k: r[k] for k in (
            "id", "name", "family", "is_current_paper", "orig_is_sharpe",
            "is_avg_sharpe", "oos_avg_sharpe", "oos_over_is", "oos_min_sharpe",
            "oos_positive", "avg_dd", "avg_trades", "verdict_score", "verdict")}
            for r in results],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(f"\nWrote {OUT} and {OUT_MD}")
    print(f"Runtime {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
