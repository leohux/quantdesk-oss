# -*- coding: utf-8 -*-
"""Apply portfolio decisions (writes strategy params only; no orders).

1) Merge momentum family: disable 3 redundant twins (lower OOS).
   keep book = Cursor-Surge, Cursor-ATRBreak (momentum reps) + RSITrend + MiMo
2) Recompute ERC on the final enabled book.
3) Store portfolio_weight in each enabled strategy's params.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import list_strategies, update_strategy
from scripts.portfolio_analysis import strategy_daily_returns, erc_weights, ann_stats, port_returns
import pandas as pd

# Redundant momentum twins to disable (higher-corr, lower-OOS of each pair)
DISABLE = {
    "hybrid-dualmom-aa4691-0c7e13",              # ~0.87 w/ Cursor-DualMom, OOS 0.58
    "hybrid-surge-539690-f7abe9",                # ~0.84 w/ Cursor-Surge,   OOS 0.78
    "cursor-dualmom-aapl-15x40-s0-l2-31b79d",    # ~0.80 w/ Cursor-ATRBreak, OOS 0.76
}


def main():
    items = list_strategies()
    enabled_before = [x for x in items if x.get("enabled")]
    print("enabled before:", len(enabled_before))

    # 1) disable redundant
    for sid in DISABLE:
        try:
            update_strategy(sid, {"enabled": False})
            print("  disabled", sid)
        except Exception as e:
            print("  disable FAIL", sid, e)

    # 2) recompute ERC on remaining enabled book
    items = list_strategies()
    book = [x for x in items if x.get("enabled")]
    print("\nfinal book:", len(book))
    series, keep = [], []
    for s in book:
        r = strategy_daily_returns(s)
        if r is None:
            print("  no returns, skip weight:", s.get("name"))
            continue
        series.append(r.rename(s["id"]))
        keep.append(s)
    R = pd.concat(series, axis=1).fillna(0.0)
    R = R.loc[R.index >= pd.Timestamp("2022-01-01")]
    w = erc_weights(R)

    # 3) write portfolio_weight
    print("\nERC weights on final book:")
    for i, s in enumerate(keep):
        wt = round(float(w[i]), 4)
        update_strategy(s["id"], {"params": {"portfolio_weight": wt}})
        print(f"  {wt*100:5.1f}%  {s.get('name')}")

    # verify portfolio stats
    st = ann_stats(port_returns(R, w))
    print(f"\nfinal-book ERC: Sharpe={st['sharpe']:.2f} ret={st['ret']:.1f}% "
          f"vol={st['vol']:.1f}% maxDD={st['maxdd']:.1f}%")

    print("\nenabled now:")
    for x in list_strategies():
        if x.get("enabled"):
            p = x.get("params") or {}
            print(f"  {x.get('name')} | w={p.get('portfolio_weight')} | {p.get('symbols')}")


if __name__ == "__main__":
    main()
