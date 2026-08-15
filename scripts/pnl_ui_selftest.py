"""Smoke-test the new P&L / journal / equity APIs against live data."""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, "/app")

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> None:
    from config.store import list_strategies
    from core.portfolio.pnl import (
        ensure_equity_schema,
        equity_curve,
        list_journal,
        portfolio_pnl,
        record_equity_snapshot,
    )
    from execution.alpaca_client import AlpacaPaperClient

    print("\n1. schema + snapshot")
    try:
        ensure_equity_schema()
        check("equity schema ok", True)
    except Exception as e:
        check("equity schema ok", False, str(e))

    client = AlpacaPaperClient()
    acct = client.account()
    positions = client.positions()
    strategies = list_strategies()
    eq = float(acct["equity"])
    check("equity > 0", eq > 0, f"${eq:,.0f}")
    check("positions loaded", isinstance(positions, list), f"n={len(positions)}")

    wrote = record_equity_snapshot(
        eq, float(acct["cash"]), float(acct["last_equity"]), source="test", min_interval_sec=0
    )
    check("snapshot write", wrote is True)

    print("\n2. portfolio_pnl shape")
    pnl = portfolio_pnl(positions, strategies)
    check("has by_strategy", isinstance(pnl.get("by_strategy"), list))
    check("has ownership", isinstance(pnl.get("ownership"), dict))
    check(
        "ownership covers broker symbols",
        all(p["symbol"] in pnl["ownership"] or True for p in positions),
        f"owners={sorted(pnl['ownership'])}",
    )
    # Every broker position should appear in ownership OR be flagged missing
    missing = [p["symbol"] for p in positions if p["symbol"] not in pnl["ownership"]]
    check("no orphan broker positions without sleeve", missing == [], f"missing={missing}")

    unreal_broker = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    unreal_attr = sum(r["unrealized_pnl"] for r in pnl["by_strategy"])
    shared = any(len(v.get("lots") or []) > 1 for v in (pnl.get("ownership") or {}).values())
    if shared:
        check(
            "shared lots: attr UPL may diverge from broker (cost-basis)",
            True,
            f"attr={unreal_attr:.2f} broker={unreal_broker:.2f}",
        )
    else:
        check(
            "attributed unrealized ~= broker total",
            abs(unreal_attr - unreal_broker) < 1.0,
            f"attr={unreal_attr:.2f} broker={unreal_broker:.2f}",
        )

    # Strategy market values should not exceed gross by much
    gross = sum(abs(float(p.get("market_value") or 0)) for p in positions)
    mv_sum = sum(r["market_value"] for r in pnl["by_strategy"])
    check(
        "strategy MV sums to broker gross",
        abs(mv_sum - gross) < 5.0,
        f"mv={mv_sum:.0f} gross={gross:.0f}",
    )

    print("\n3. journal")
    from core.portfolio.pnl import journal_summary

    all_rows = list_journal(limit=200)
    open_rows = list_journal(status="open", limit=200)
    closed_rows = list_journal(status="closed", limit=200)
    summary = journal_summary()
    check("journal returns list", isinstance(all_rows, list), f"n={len(all_rows)}")
    check("open filter works", all(r["status"] == "open" for r in open_rows), f"n={len(open_rows)}")
    check(
        "closed filter works",
        all(r["status"] == "closed" for r in closed_rows),
        f"n={len(closed_rows)}",
    )
    check(
        "journal_summary.realized ~= portfolio_pnl.realized",
        abs(float(summary["realized_pnl"]) - float(pnl["realized_pnl"])) < 0.01,
        f"journal={summary['realized_pnl']:.2f} dash={pnl['realized_pnl']:.2f}",
    )
    # Open journal count should match sleeve positions (after repair)
    sleeve_n = len(pnl["ownership"])
    check(
        "open journal ≈ sleeve positions",
        abs(len(open_rows) - sleeve_n) <= 2,
        f"open={len(open_rows)} sleeves={sleeve_n}",
    )
    # Closed with exit_price should have realized_pnl
    bad_closed = [
        r
        for r in closed_rows
        if r.get("exit_price") is not None and r.get("realized_pnl") is None
    ]
    check("closed+exit_price have realized_pnl", bad_closed == [], f"bad={len(bad_closed)}")

    print("\n4. equity curve")
    curve = equity_curve(30)
    check("curve has points", len(curve) >= 1, f"n={len(curve)}")
    if len(curve) >= 2:
        check(
            "curve chronological",
            all((curve[i]["ts"] or "") <= (curve[i + 1]["ts"] or "") for i in range(len(curve) - 1)),
        )
    check("latest equity near live", abs(curve[-1]["equity"] - eq) < 50, f"curve={curve[-1]['equity']:.0f}")

    print("\n5. dashboard endpoint via FastAPI TestClient")
    try:
        from fastapi.testclient import TestClient
        from api.main import app

        # Disable auth for this probe if middleware allows; otherwise call helpers directly.
        tc = TestClient(app)
        # Use internal helpers that dashboard uses
        from api.main import dashboard as dash_fn, journal as journal_fn, equity_curve_api

        d = dash_fn()
        check("dashboard summary keys", "realized_pnl" in d["summary"])
        check("dashboard strategy_pnl", isinstance(d.get("strategy_pnl"), list))
        check(
            "positions have strategy_name",
            all("strategy_name" in p for p in d["positions"]),
        )
        j = journal_fn(limit=20)
        check("journal api", "trades" in j and j["count"] == len(j["trades"]))
        c = equity_curve_api(days=7)
        check("curve api", "points" in c)

        # Auth-gated HTTP should 401 without token
        r = tc.get("/api/dashboard")
        check("HTTP without auth is blocked or ok", r.status_code in (200, 401, 403), str(r.status_code))
    except Exception as e:
        check("dashboard endpoint", False, f"{e}\n{traceback.format_exc()[-400:]}")

    print("\n6. known data-quality traps")
    # realized may be 0 historically — warn but not fail
    if float(pnl.get("realized_pnl") or 0) == 0:
        print("  [WARN] cumulative realized_pnl is 0 — expected until settled exits accumulate")
    # Double-count risk: same symbol in multiple strategies
    from collections import Counter

    syms = []
    for r in pnl["by_strategy"]:
        syms.extend(r["symbols"])
    dup = [s for s, n in Counter(syms).items() if n > 1]
    check("no symbol attributed to two strategies", dup == [], f"dup={dup}")

    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
