# -*- coding: utf-8 -*-
"""Plan A: disjoint + expand tech baskets, recompute ERC."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import list_strategies, update_strategy
from scripts.portfolio_analysis import strategy_daily_returns, erc_weights, ann_stats, port_returns

# Match by strategy id (stable)
BASKETS = {
    # will fill ids dynamically by name match below
}

NAME_BASKETS = {
    "Cursor-Surge-NVDA-052828-63859c": [
        "NVDA", "PLTR", "HOOD", "SOFI", "UPST",
        "AMD", "ARM", "TSLA", "SMCI", "COIN",
    ],
    "Cursor-Hybrid-Classic-ATRBreak-AAPL-m15-061001-d4e35": [
        "AAPL", "MSFT", "META", "AMZN", "GOOGL",
        "AVGO", "ORCL", "NFLX", "CRM", "ADBE",
    ],
    "MiMo-Mean-Reversion RSI Extreme": [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA",
        "AMZN", "META", "GOOGL", "AMD", "AVGO",
    ],
}


def main():
    items = list_strategies()
    enabled = [s for s in items if s.get("enabled")]
    print("enabled:", [s.get("name") for s in enabled])

    for name_key, syms in NAME_BASKETS.items():
        hit = next((s for s in enabled if s.get("name") == name_key), None)
        if hit is None:
            hit = next((s for s in enabled if name_key in (s.get("name") or "")), None)
        if hit is None:
            print("MISS", name_key)
            continue
        update_strategy(hit["id"], {"params": {"symbols": syms}})
        print(f"UPDATED {hit['name']} -> {len(syms)} syms: {syms}")

    items = list_strategies()
    book = [x for x in items if x.get("enabled")]
    series, keep = [], []
    for s in book:
        print("computing returns:", s.get("name"), (s.get("params") or {}).get("symbols"))
        r = strategy_daily_returns(s)
        if r is None:
            print("  SKIP no returns")
            continue
        series.append(r.rename(s["id"]))
        keep.append(s)

    R = pd.concat(series, axis=1).fillna(0.0)
    R = R.loc[R.index >= pd.Timestamp("2022-01-01")]
    w = erc_weights(R)

    print("\n=== ERC weights after basket expand ===")
    for i, s in enumerate(keep):
        # refresh params after symbols update
        wt = round(float(w[i]), 4)
        update_strategy(s["id"], {"params": {"portfolio_weight": wt}})
        # re-read for print
        from config.store import get_strategy
        cur = get_strategy(s["id"])
        print(f"  {wt*100:5.1f}%  {cur.get('name')}  {(cur.get('params') or {}).get('symbols')}")

    st = ann_stats(port_returns(R, w))
    print(
        f"\nbook ERC: Sharpe={st['sharpe']:.2f} ret={st['ret']:.1f}% "
        f"vol={st['vol']:.1f}% maxDD={st['maxdd']:.1f}%"
    )

    print("\n=== daily corr (final book) ===")
    C = R.corr()
    labels = [k.get("name")[:32] for k in keep]
    for i in range(len(keep)):
        for j in range(i + 1, len(keep)):
            print(f"  {C.iloc[i, j]:+.2f}  {labels[i]}  x  {labels[j]}")


if __name__ == "__main__":
    main()
