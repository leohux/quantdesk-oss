# -*- coding: utf-8 -*-
"""Rebuild SEC PIT panel from local companyfacts cache (no network).

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.data.rebuild_sec_panel
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from research.alpha_v2.data.fetch_sec_fundamentals import (  # noqa: E402
    CACHE_PX,
    FACTS_DIR,
    OUT_PANEL,
    build_symbol_pit,
    fetch_facts,
    load_ticker_map,
)


def main() -> None:
    close = pd.read_parquet(CACHE_PX)
    symbols = [c for c in close.columns if c != "SPY"]
    tmap = load_ticker_map()
    rows, miss = [], []
    cached = {p.stem for p in FACTS_DIR.glob("*.json")}
    print(f"symbols={len(symbols)} local_facts={len(cached)}")
    for i, sym in enumerate(symbols, 1):
        cik = tmap.get(sym) or tmap.get(sym.replace(".", "-"))
        if cik is None:
            miss.append(sym)
            continue
        path = FACTS_DIR / f"{cik:010d}.json"
        if not path.exists():
            miss.append(sym)
            continue
        try:
            facts = fetch_facts(cik)  # reads cache
            pit = build_symbol_pit(sym, facts)
            if pit.empty:
                miss.append(sym)
            else:
                rows.append(pit)
        except Exception as e:
            print(f"  FAIL {sym}: {e}")
            miss.append(sym)
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} panels={len(rows)}", flush=True)
    if not rows:
        raise SystemExit("no panels")
    panel = pd.concat(rows, ignore_index=True)
    panel.to_parquet(OUT_PANEL, index=False)
    print(
        f"Saved {OUT_PANEL} rows={len(panel)} symbols={panel['symbol'].nunique()} "
        f"cols={list(panel.columns)} miss={len(miss)}"
    )


if __name__ == "__main__":
    main()
