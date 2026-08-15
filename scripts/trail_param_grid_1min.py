# -*- coding: utf-8 -*-
"""Grid trail_activate_pct × trail_pct × take_profit on 1Min paths.

Replays the closed trades that can activate trail (default: AMC×2, MSFT)
and counts how often exit_reason == trail_stop (true takeover) vs hard_tp.

Usage:
  python /app/scripts/trail_param_grid_1min.py
"""
from __future__ import annotations

import itertools
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

import trail_stop_replay_1min as base  # noqa: E402
from sqlalchemy import text

from core.db import SyncSessionLocal

OUT = Path("/app/data/research/trail_param_grid_1min.json")
IDS = (13, 48, 143)
ACTIVATE = [0.08, 0.10, 0.13, 0.15]
TRAIL = [0.05, 0.075, 0.10]
TP = [0.12, 0.15, 0.18, 0.22]


def main() -> int:
    s = SyncSessionLocal()
    try:
        rows = [
            dict(r)
            for r in s.execute(
                text(
                    """
                    SELECT id, symbol, qty, entry_price, exit_price, realized_pnl,
                           opened_at, closed_at
                    FROM trade_journal
                    WHERE strategy_id='strategy-046bfa' AND status='closed'
                      AND id IN (13, 48, 143)
                    ORDER BY opened_at
                    """
                )
            ).mappings().all()
        ]
    finally:
        s.close()

    grid = []
    for act, tr, tp in itertools.product(ACTIVATE, TRAIL, TP):
        # monkeypatch module constants for simulate_1min
        base.TRAIL_ACTIVATE = act
        base.TRAIL_PCT = tr
        base.HARD_TP = tp
        base.DISABLE_HARD_TP_AFTER_TRAIL = False

        takeover = 0
        hard_tp_n = 0
        hard_sl_n = 0
        other = 0
        sim_pnl = 0.0
        actual_pnl = 0.0
        details = []
        for r in rows:
            actual_pnl += float(r["realized_pnl"] or 0)
            sim = base.simulate_1min(
                r["symbol"],
                float(r["entry_price"]),
                float(r["qty"]),
                r["opened_at"],
                r["closed_at"],
            )
            if sim.get("error"):
                other += 1
                continue
            sim_pnl += float(sim["realized_pnl"])
            reason = sim.get("exit_reason")
            if reason == "trail_stop":
                takeover += 1
            elif reason == "hard_tp":
                hard_tp_n += 1
            elif reason == "hard_sl":
                hard_sl_n += 1
            else:
                other += 1
            details.append(
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "reason": reason,
                    "sim_pnl": sim.get("realized_pnl"),
                    "actual_pnl": r["realized_pnl"],
                    "activated": sim.get("trail_activated"),
                }
            )

        grid.append(
            {
                "trail_activate_pct": act,
                "trail_pct": tr,
                "take_profit": tp,
                "buffer_pct": round(tp - act, 4),
                "n_trail_takeover": takeover,
                "n_hard_tp": hard_tp_n,
                "n_hard_sl": hard_sl_n,
                "n_other": other,
                "sim_pnl": sim_pnl,
                "actual_pnl": actual_pnl,
                "delta_pnl": sim_pnl - actual_pnl,
                "details": details,
            }
        )
        print(
            f"act={act:.0%} trail={tr:.1%} tp={tp:.0%} "
            f"takeover={takeover} hard_tp={hard_tp_n} Δpnl=${sim_pnl-actual_pnl:+.0f}",
            flush=True,
        )

    # Prefer combos that produce at least 1 takeover; among those max sim_pnl
    with_takeover = [g for g in grid if g["n_trail_takeover"] > 0]
    ranked = sorted(
        with_takeover or grid,
        key=lambda g: (g["n_trail_takeover"], g["sim_pnl"]),
        reverse=True,
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ids": list(IDS),
        "n_combos": len(grid),
        "combos_with_takeover": len(with_takeover),
        "top": ranked[:10],
        "all": grid,
        "note": (
            "Only 3 historical paths; diagnostic for whether activate/TP buffer "
            "can ever let trail fire. Not a live param recommendation alone."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT}")
    print("combos_with_takeover", len(with_takeover))
    if ranked:
        print("best", {k: ranked[0][k] for k in (
            "trail_activate_pct", "trail_pct", "take_profit",
            "n_trail_takeover", "sim_pnl", "delta_pnl",
        )})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
