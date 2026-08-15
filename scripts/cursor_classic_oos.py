# -*- coding: utf-8 -*-
"""OOS screen Cursor/Classic hot strategies 鈫?paper enable KEEP."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
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
CACHE: dict = {}
API = "http://127.0.0.1:18080"
OUT = Path(os.environ.get("QUANTDESK_OOS_OUT", str(ROOT / "data" / "store" / "cursor_classic_oos_shortlist.json")))


def is_cursor_classic(s: dict) -> bool:
    sid = str(s.get("id") or "")
    name = str(s.get("name") or "")
    typ = str(s.get("type") or "")
    return (
        sid.startswith("cursor-")
        or name.startswith(("Cursor-", "Classic-"))
        or "classic_" in typ
        or name.startswith("Cursor-Hybrid-Classic")
    )


def family(s: dict) -> str:
    typ = str(s.get("type") or "")
    if typ.startswith("classic_") or typ.startswith("hybrid_"):
        return typ
    name = str(s.get("name") or "")
    for tag in (
        "RSITrend",
        "DualMom",
        "VolMom",
        "TrendMom",
        "ChanRSI",
        "Surge",
        "Squeeze",
        "Turtle",
        "Connors",
        "AbsMom",
        "Golden",
        "TSMOM",
        "ATRBreak",
        "BBSqueeze",
        "InvDonchian",
        "Pullback",
        "BBTrend",
    ):
        if tag.lower() in name.lower() or tag in name:
            return "fam_" + tag.lower()
    return typ or "other"


def dedupe_key(s: dict):
    m = s.get("metrics") or {}
    return (
        family(s),
        round(float(m.get("sharpe") or 0), 2),
        round(float(m.get("total_return") or m.get("total_return_pct") or 0), 2),
        round(float(m.get("max_drawdown") or m.get("max_drawdown_pct") or 0), 1),
        tuple((s.get("params") or {}).get("symbols") or []),
    )


def load_code(sid: str) -> str:
    root = Path(os.environ.get("QUANTDESK_ROOT", str(ROOT)))
    for p in (
        Path(f"/app/data/store/strategy_code/{sid}.py"),
        root / "data" / "store" / "strategy_code" / f"{sid}.py",
    ):
        if p.exists():
            return p.read_text(encoding="utf-8")
    return get_strategy(sid).get("code") or ""


def bt(code, params, symbol, start, end=None):
    key = (symbol, start, end)
    if key not in CACHE:
        CACHE[key] = load_ohlcv(symbol, start=start, end=end)
    return run_signal_backtest(CACHE[key], code, params, init_cash=100_000, fees=0.001)


def score_candidate(s: dict) -> dict:
    code = load_code(s["id"])
    params = dict(s.get("params") or {})
    params.pop("symbols", None)

    def one(start, end):
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
                        "error": str(e)[:100],
                    }
                )
        sharpes = [r["sharpe"] for r in rows]
        return {
            "avg_sharpe": statistics.mean(sharpes),
            "median_sharpe": statistics.median(sharpes),
            "min_sharpe": min(sharpes),
            "positive": sum(1 for r in rows if r["ret"] > 0),
            "avg_dd": statistics.mean(r["dd"] for r in rows),
            "rows": rows,
        }

    is_stats = one(*IS)
    oos_stats = one(*OOS)
    is_s = is_stats["avg_sharpe"]
    oos_s = oos_stats["avg_sharpe"]
    collapse = (oos_s / is_s) if is_s > 0.2 else (1.0 if oos_s > 0 else 0.0)
    score = oos_s + 0.25 * min(collapse, 1.5) + 0.05 * oos_stats["positive"]
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
        "verdict_score": score,
    }


def login() -> str:
    import os

    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not password:
        raise RuntimeError("ADMIN_PASSWORD env var is required for API login")
    req = urllib.request.Request(
        API + "/api/auth/jwt-login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["access_token"]


def api(method: str, path: str, tok: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Content-Type": "application/json", "X-Access-Token": tok},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def pick_symbols(r: dict) -> list[str]:
    """Prefer OOS-positive names; always include orig if strong."""
    rows = sorted(r["oos"]["rows"], key=lambda x: x["sharpe"], reverse=True)
    good = [x["symbol"] for x in rows if x["ret"] > 0 and x["sharpe"] > 0.2][:6]
    if r["orig_symbol"] and r["orig_symbol"] not in good:
        # keep orig if not terrible
        for x in rows:
            if x["symbol"] == r["orig_symbol"] and x["sharpe"] > -0.2:
                good = [r["orig_symbol"]] + good
                break
    if not good:
        good = [r["orig_symbol"] or "SPY"]
    # unique preserve order
    out = []
    for s in good:
        if s and s not in out:
            out.append(s)
    return out[:5]


def main():
    items = list_strategies()
    if isinstance(items, dict):
        items = items.get("strategies") or items.get("items") or []
    pool = [s for s in items if is_cursor_classic(s)]
    pool.sort(
        key=lambda s: float((s.get("metrics") or {}).get("sharpe") or 0), reverse=True
    )
    print(f"cursor_classic_hot={len(pool)}")

    seen = set()
    uniq = []
    for s in pool:
        sh = float((s.get("metrics") or {}).get("sharpe") or 0)
        if sh < 1.0:
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
        shortlist.extend(arr[:2])
    shortlist = shortlist[:16]
    print(f"unique_ge_1.0={len(uniq)} families={len(by_fam)} shortlist={len(shortlist)}")
    for s in shortlist:
        print(
            f"  {s.get('id')} {s.get('name')} S={float((s.get('metrics') or {}).get('sharpe') or 0):.2f} "
            f"fam={family(s)} sym={(s.get('params') or {}).get('symbols')}"
        )

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(score_candidate, s): s for s in shortlist}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
                r = results[-1]
                print(
                    f"done {r['name']} OOS={r['oos']['avg_sharpe']:.2f} IS={r['is']['avg_sharpe']:.2f}"
                )
            except Exception as e:
                print("FAIL", futs[fut].get("name"), e)

    results.sort(key=lambda r: r["verdict_score"], reverse=True)
    print("\n=== RANKED ===")
    keep, watch = [], []
    for i, r in enumerate(results, 1):
        o = r["oos"]
        if o["avg_sharpe"] >= 0.45 and o["positive"] >= 4 and o["min_sharpe"] > -0.35:
            flag = "KEEP"
            keep.append(r)
        elif o["avg_sharpe"] >= 0.30 and o["positive"] >= 3:
            flag = "WATCH"
            watch.append(r)
        else:
            flag = "DROP"
        print(
            f"{i:<3} {r['family']:<22} OOS={o['avg_sharpe']:5.2f} IS={r['is']['avg_sharpe']:5.2f} "
            f"pos={o['positive']}/8 min={o['min_sharpe']:5.2f} {flag}  {r['name']} ({r['id']})"
        )

    # Paper enable: keep existing 3 hybrids + add up to 3 new KEEP with focused symbols
    # Cap total enabled paper strategies to avoid overtrading same account
    tok = login()
    currently = api("GET", "/api/strategies", tok)
    if isinstance(currently, dict):
        currently = currently.get("strategies") or currently.get("items") or []
    already = {x["id"] for x in currently if x.get("enabled")}

    enabled_new = []
    for r in keep[:3]:
        syms = pick_symbols(r)
        detail = api("GET", f"/api/strategies/{r['id']}", tok)
        params = dict(detail.get("params") or {})
        params["symbols"] = syms
        updated = api(
            "PATCH",
            f"/api/strategies/{r['id']}",
            tok,
            {"enabled": True, "params": params},
        )
        enabled_new.append(
            {
                "id": r["id"],
                "name": r["name"],
                "symbols": syms,
                "oos_avg_sharpe": r["oos"]["avg_sharpe"],
                "enabled": updated.get("enabled"),
            }
        )
        print("PAPER_ON", r["id"], syms, f"OOS={r['oos']['avg_sharpe']:.2f}")

    # dry-run paper to confirm loads
    import subprocess

    print("\n=== DRY RUN ===")
    subprocess.run(
        [
            "docker",
            "exec",
            "-w",
            "/app",
            "-e",
            "PYTHONPATH=/app",
            "quantdesk",
            "python",
            "/app/scripts/phase6_runner.py",
            "--dry-run",
        ],
        check=False,
    )

    out = {
        "elapsed_sec": round(time.time() - t0, 1),
        "shortlist": len(shortlist),
        "keep": [
            {
                "id": r["id"],
                "name": r["name"],
                "family": r["family"],
                "oos": r["oos"]["avg_sharpe"],
                "is": r["is"]["avg_sharpe"],
                "positive": r["oos"]["positive"],
                "orig": r["orig_symbol"],
            }
            for r in keep
        ],
        "watch": [
            {
                "id": r["id"],
                "name": r["name"],
                "oos": r["oos"]["avg_sharpe"],
                "positive": r["oos"]["positive"],
            }
            for r in watch
        ],
        "paper_enabled_new": enabled_new,
        "already_enabled": list(already),
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    # also write inside container mount path
    Path("/app/data/store/cursor_classic_oos_shortlist.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
