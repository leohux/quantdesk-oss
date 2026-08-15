"""User override: reopen strategy-046bfa for new paper entries.

Research still labeled research_archive_gate_fail. This only flips live ops:
enabled=true, allow_new_entries=true. Runner re-reads strategies.json each tick.

  python scripts/reopen_046bfa.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import (
    CODE_DIR,
    get_strategy,
    list_strategies,
    save_strategies,
    update_strategy,
)

SID = "strategy-046bfa"
SNAP = ROOT / "data" / "research" / "strategy_046bfa_live_snapshot.json"

DESC = (
    "US tech breakout continuation: day-return in [buy_surge, buy_cap), "
    "SMA(trend_ma) with short-history fallback; false-breakout filters; "
    "IPO ann-vol gate while bars < recheck_at_bars; bracket SL/TP; "
    "max_hold_days; day_crash live; trail dry-run. SPCX @ 5% cap. "
    "Former name: 美股小盘早盘异动. | REOPEN 2026-08-13: user override of "
    "wind-down; allow_new_entries=true (research label unchanged)."
)


def _from_snapshot() -> dict:
    snap = json.loads(SNAP.read_text(encoding="utf-8"))
    params = dict(snap.get("params") or {})
    params["allow_new_entries"] = True
    params["paper_reopen_at"] = "2026-08-13"
    params["paper_reopen_note"] = "user override of 2026-08-04 wind-down"
    params.pop("wind_down_completed_at", None)
    code = str(snap.get("strategy_code") or "")
    if code:
        CODE_DIR.mkdir(parents=True, exist_ok=True)
        (CODE_DIR / "strategy-046bfa.py").write_text(code, encoding="utf-8")
    return {
        "id": SID,
        "name": snap.get("name") or "美股科技股突破延续",
        "description": DESC,
        "type": snap.get("type") or "intraday_surge",
        "enabled": True,
        "status": "running",
        "params": params,
        "metrics": {"sharpe": None, "total_return_pct": None},
    }


def main() -> None:
    patch = {
        "enabled": True,
        "status": "running",
        "description": DESC,
        "params": {
            "allow_new_entries": True,
            "paper_reopen_at": "2026-08-13",
            "paper_reopen_note": "user override of 2026-08-04 wind-down",
        },
    }
    try:
        get_strategy(SID)
        s = update_strategy(SID, patch)
    except KeyError:
        item = _from_snapshot()
        items = list(list_strategies())
        items.append(item)
        save_strategies(items)
        s = get_strategy(SID)

    p = s.get("params") or {}
    syms = [str(x).upper() for x in (p.get("symbols") or [])]
    print("id:", s.get("id"))
    print("name:", s.get("name"))
    print("enabled:", s.get("enabled"), "status:", s.get("status"))
    print("allow_new_entries:", p.get("allow_new_entries"))
    print("n_symbols:", len(syms), "CRWV:", "CRWV" in syms)
    print("stocknum:", p.get("stocknum"), "buy_cap:", p.get("buy_cap"))


if __name__ == "__main__":
    main()
