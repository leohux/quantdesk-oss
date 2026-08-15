# -*- coding: utf-8 -*-
"""Enable KEEP hybrid shortlist for daily paper trading."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import get_strategy, list_strategies, update_strategy

# KEEP from OOS shortlist + RSITrend@SPY as watch (locked to SPY)
ENABLE = {
    "hybrid-surge-539690-f7abe9": {
        # Multi-symbol OOS was strong on growth/small names; keep a focused basket
        "symbols": ["GOOGL", "AAPL", "PLTR", "HOOD", "SOFI", "NVDA"],
    },
    "hybrid-dualmom-aa4691-0c7e13": {
        # Index OOS weak; stick to single-name winners from OOS
        "symbols": ["AAPL", "NVDA", "PLTR", "HOOD"],
    },
    "hybrid-rsitrend-258a9f-3f21dc": {
        # Twin of ad9dbd; paper only on SPY as recommended
        "symbols": ["SPY"],
    },
}


def main() -> None:
    items = list_strategies()
    if isinstance(items, dict):
        items = items.get("strategies") or items.get("items") or []

    print("before_enabled:")
    for s in items:
        if s.get("enabled"):
            print(" ", s.get("id"), s.get("name"), (s.get("params") or {}).get("symbols"))

    for sid, cfg in ENABLE.items():
        s = get_strategy(sid)
        if not s:
            print("MISSING", sid)
            continue
        params = dict(s.get("params") or {})
        params["symbols"] = cfg["symbols"]
        update_strategy(sid, {"enabled": True, "params": params})
        s2 = get_strategy(sid)
        print(
            "ENABLED",
            sid,
            s2.get("name"),
            "symbols=",
            (s2.get("params") or {}).get("symbols"),
            "enabled=",
            s2.get("enabled"),
        )

    # Keep morning-surge intraday as-is (already enabled)
    s046 = get_strategy("strategy-046bfa")
    print(
        "keep_intraday",
        s046 and s046.get("id"),
        "enabled=",
        s046 and s046.get("enabled"),
        "symbols=",
        (s046 or {}).get("params", {}).get("symbols"),
    )

    items = list_strategies()
    if isinstance(items, dict):
        items = items.get("strategies") or items.get("items") or []
    en = [x for x in items if x.get("enabled")]
    print("after_enabled_count", len(en))
    for x in en:
        print(
            " ",
            x.get("id"),
            x.get("name"),
            (x.get("params") or {}).get("symbols"),
        )


if __name__ == "__main__":
    main()
