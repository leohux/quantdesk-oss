"""Expand strategy-046bfa watchlist: union current names with Nasdaq-100 / QQQ holdings.

Keeps extra names already on the sleeve (COIN, HOOD, ORCL, …) that are not in NDX.
Does not add QQQ itself or cash/futures sleeves.

  python scripts/expand_intraday_pool.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import get_strategy, update_strategy

SID = "strategy-046bfa"

# Nasdaq-100 component tickers (Wikipedia "List of NASDAQ-100 companies", 2026-08).
# GOOG + GOOGL both listed; QQQ holds both share classes. 102 names.
NASDAQ_100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALAB", "ALNY", "AMAT",
    "AMD", "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON", "BKR", "BKNG",
    "CCEP", "CDNS", "CEG", "CMCSA", "COST", "CPRT", "CRWD", "CRWV", "CSCO", "CSX",
    "CTAS", "DASH", "DDOG", "DXCM", "EXC", "FANG", "FAST", "FER", "FTNT", "GEHC",
    "GILD", "GOOG", "GOOGL", "HON", "HONA", "IDXX", "INTC", "INTU", "ISRG", "KDP",
    "KHC", "KLAC", "LIN", "LITE", "LRCX", "MAR", "MCHP", "MDLZ", "MELI", "META",
    "MNST", "MPWR", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NVDA", "NXPI",
    "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM",
    "REGN", "RKLB", "ROP", "ROST", "SBUX", "SHOP", "SNDK", "SNPS", "SPCX", "STX",
    "TER", "TMUS", "TRI", "TSLA", "TTWO", "TXN", "VRTX", "WBD", "WDAY", "WDC",
    "WMT", "XEL",
]


def _norm(syms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in syms:
        u = str(s or "").strip().upper()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def main() -> None:
    s = get_strategy(SID)
    current = _norm([str(x) for x in ((s.get("params") or {}).get("symbols") or [])])
    ndx = _norm(NASDAQ_100)
    merged = _norm(current + ndx)
    added = [x for x in merged if x not in set(current)]

    s = update_strategy(SID, {"params": {"symbols": merged}})
    p = s.get("params") or {}
    print("name:", s.get("name"))
    print("n_before:", len(current), "n_ndx:", len(ndx), "n_after:", len(merged))
    print("added:", ",".join(added) if added else "(none)")
    print("symbols:", ",".join(p.get("symbols") or []))
    print("buy_surge/cap:", p.get("buy_surge"), p.get("buy_cap"))
    print("SL/TP:", p.get("stop_loss"), p.get("take_profit"))
    print("stocknum:", p.get("stocknum"))


if __name__ == "__main__":
    main()
