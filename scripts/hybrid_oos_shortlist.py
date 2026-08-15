# -*- coding: utf-8 -*-
"""Dedupe hybrids and run multi-symbol IS/OOS shortlist screen."""
from __future__ import annotations

import sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backtest.runner import run_signal_backtest
from config.store import get_strategy, list_strategies
from data.loader import load_ohlcv

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "PLTR", "HOOD", "SOFI"]
IS = ("2022-01-01", "2023-12-31")
OOS = ("2024-01-01", None)
CACHE: dict[tuple, object] = {}


def all_strategies():
    items = list_strategies()
    if isinstance(items, dict):
        items = items.get("strategies") or items.get("items") or []
    return items


def is_hybrid(s):
    t = str(s.get("type") or "")
    n = str(s.get("name") or "")
    return "hybrid" in t.lower() or n.startswith("Hybrid-") or "融合" in n


def dedupe_key(s):
    m = s.get("metrics") or {}
    return (
        s.get("type"),
        round(float(m.get("sharpe") or 0), 2),
        round(float(m.get("total_return") or 0), 2),
        round(float(m.get("max_drawdown") or 0), 1),
        int(float(m.get("trades") or 0)),
        tuple((s.get("params") or {}).get("symbols") or []),
    )


def family(s):
    t = str(s.get("type") or "")
    if t.startswith("hybrid_"):
        return t
    n = str(s.get("name") or "")
    for tag in (
        "RSITrend",
        "ATRBreak",
        "DualMom",
        "Squeeze",
        "Surge",
        "TrendMom",
        "BBTrend",
    ):
        if tag in n:
            return "hybrid_" + tag.lower()
    return t or "other"


def load_code(sid: str) -> str:
    p = Path(f"/app/data/store/strategy_code/{sid}.py")
    if p.exists():
        return p.read_text(encoding="utf-8")
    return get_strategy(sid).get("code") or ""


def bt(code, params, symbol, start, end=None):
    key = (symbol, start, end)
    if key not in CACHE:
        CACHE[key] = load_ohlcv(symbol, start=start, end=end)
    return run_signal_backtest(CACHE[key], code, params, init_cash=100_000, fees=0.001)


def score_candidate(s):
    code = load_code(s["id"])
    params = dict(s.get("params") or {})
    # Don't force original single-symbol; evaluate across basket
    params.pop("symbols", None)

    def one(period, start, end):
        rows = []
        for sym in SYMBOLS:
            try:
                m = bt(code, params, sym, start, end)
                rows.append(
                    {
                        "symbol": sym,
                        "sharpe": float(m.get("sharpe") or 0),
                        "ret": float(m.get("total_return_pct") or 0),
                        "dd": float(m.get("max_drawdown_pct") or 0),
                        "trades": int(m.get("trades") or 0),
                    }
                )
            except Exception as e:
                rows.append(
                    {
                        "symbol": sym,
                        "sharpe": 0,
                        "ret": 0,
                        "dd": 99,
                        "trades": 0,
                        "error": str(e)[:120],
                    }
                )
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

    is_stats = one("IS", *IS)
    oos_stats = one("OOS", *OOS)
    # Prefer OOS stability: avg OOS sharpe, not too much worse than IS, breadth
    oos_s = oos_stats["avg_sharpe"]
    is_s = is_stats["avg_sharpe"]
    collapse = (oos_s / is_s) if is_s > 0.2 else (1.0 if oos_s > 0 else 0.0)
    verdict_score = oos_s + 0.25 * min(collapse, 1.5) + 0.05 * oos_stats["positive"]
    return {
        "id": s["id"],
        "name": s.get("name"),
        "type": s.get("type"),
        "family": family(s),
        "orig_symbol": ((s.get("params") or {}).get("symbols") or [None])[0],
        "orig_sharpe": float((s.get("metrics") or {}).get("sharpe") or 0),
        "is": is_stats,
        "oos": oos_stats,
        "collapse": collapse,
        "verdict_score": verdict_score,
    }


def main():
    items = all_strategies()
    hy = [s for s in items if is_hybrid(s)]
    hy = sorted(
        hy,
        key=lambda s: float((s.get("metrics") or {}).get("sharpe") or 0),
        reverse=True,
    )

    # Dedupe exact clones, then keep top 2 per family among sharpe>=1.2
    seen = set()
    uniq = []
    for s in hy:
        sh = float((s.get("metrics") or {}).get("sharpe") or 0)
        if sh < 1.2:
            continue
        k = dedupe_key(s)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)

    by_fam: dict[str, list] = {}
    for s in uniq:
        by_fam.setdefault(family(s), []).append(s)

    shortlist = []
    for fam, arr in by_fam.items():
        shortlist.extend(arr[:2])  # top 2 unique per family

    # Cap total work
    shortlist = shortlist[:14]
    print(f"unique_ge_1.2={len(uniq)} families={list(by_fam)} shortlist={len(shortlist)}")
    for s in shortlist:
        print(
            f"  candidate {s.get('id')} {s.get('name')} type={s.get('type')} "
            f"origS={float((s.get('metrics') or {}).get('sharpe') or 0):.2f} "
            f"sym={(s.get('params') or {}).get('symbols')}"
        )

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(score_candidate, s): s for s in shortlist}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
                print("done", results[-1]["name"], f"OOS={results[-1]['oos']['avg_sharpe']:.2f}")
            except Exception as e:
                s = futs[fut]
                print("FAIL", s.get("name"), e)

    results.sort(key=lambda r: r["verdict_score"], reverse=True)
    print("\n=== SHORTLIST RANKED ===")
    print(
        f"{'rank':<4} {'family':<18} {'OOS':>5} {'IS':>5} {'collapse':>8} "
        f"{'pos':>4} {'minOOS':>6} {'origS':>5}  name"
    )
    keep = []
    for i, r in enumerate(results, 1):
        o = r["oos"]
        inn = r["is"]
        flag = ""
        # Keep if OOS avg sharpe >= 0.4 and at least 4/8 symbols positive and min not terrible
        if o["avg_sharpe"] >= 0.45 and o["positive"] >= 4 and o["min_sharpe"] > -0.3:
            flag = "KEEP"
            keep.append(r)
        elif o["avg_sharpe"] >= 0.30 and o["positive"] >= 3:
            flag = "WATCH"
        else:
            flag = "DROP"
        print(
            f"{i:<4} {r['family']:<18} {o['avg_sharpe']:5.2f} {inn['avg_sharpe']:5.2f} "
            f"{r['collapse']:8.2f} {o['positive']:2d}/8 {o['min_sharpe']:6.2f} "
            f"{r['orig_sharpe']:5.2f}  {flag}  {r['name']} ({r['id']}) orig={r['orig_symbol']}"
        )

    print("\n=== KEEP DETAIL (per symbol OOS) ===")
    for r in keep[:8]:
        print(f"\n{r['name']} [{r['id']}] OOS avgS={r['oos']['avg_sharpe']:.2f}")
        for row in r["oos"]["rows"]:
            print(
                f"  {row['symbol']:<5} S={row['sharpe']:6.2f} ret={row['ret']:7.1f}% "
                f"dd={row['dd']:5.1f}% n={row['trades']}"
            )

    out = {
        "shortlist_size": len(shortlist),
        "keep": [
            {
                "id": r["id"],
                "name": r["name"],
                "family": r["family"],
                "oos_avg_sharpe": r["oos"]["avg_sharpe"],
                "is_avg_sharpe": r["is"]["avg_sharpe"],
                "collapse": r["collapse"],
                "oos_positive": r["oos"]["positive"],
            }
            for r in keep
        ],
        "ranked": [
            {
                "id": r["id"],
                "name": r["name"],
                "family": r["family"],
                "oos": r["oos"]["avg_sharpe"],
                "is": r["is"]["avg_sharpe"],
                "score": r["verdict_score"],
            }
            for r in results
        ],
    }
    Path("/app/data/store/hybrid_shortlist.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nWrote /app/data/store/hybrid_shortlist.json")


if __name__ == "__main__":
    main()
