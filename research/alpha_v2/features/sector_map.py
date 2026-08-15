# -*- coding: utf-8 -*-
"""S&P sector map helper (Wikipedia GICS)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data" / "universes" / "sp500_sector_map.csv"


def load_sector_map(force: bool = False) -> pd.Series:
    """Return Series: symbol -> GICS Sector."""
    if CACHE.exists() and not force:
        df = pd.read_csv(CACHE)
        s = df.set_index("symbol")["sector"]
        s.index = s.index.astype(str).str.upper().str.replace(".", "-", regex=False)
        return s

    import io
    import urllib.request

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; QuantDeskResearch/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    tables = pd.read_html(io.StringIO(html))
    raw = tables[0]
    sym_col = "Symbol" if "Symbol" in raw.columns else raw.columns[0]
    sec_col = "GICS Sector" if "GICS Sector" in raw.columns else raw.columns[3]
    out = pd.DataFrame(
        {
            "symbol": raw[sym_col].astype(str).str.upper().str.replace(".", "-", regex=False),
            "sector": raw[sec_col].astype(str),
        }
    ).drop_duplicates("symbol")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(CACHE, index=False)
    return out.set_index("symbol")["sector"]


def industry_neutralize_score(
    panel: pd.DataFrame,
    score_col: str,
    sector_map: pd.Series,
) -> pd.Series:
    """Subtract cross-sectional sector mean of score each day."""
    d = panel[["date", "symbol", score_col]].copy()
    d["sector"] = d["symbol"].map(sector_map)
    # unmapped -> own pseudo sector (no neutralization)
    d["sector"] = d["sector"].fillna(d["symbol"])
    mu = d.groupby(["date", "sector"])[score_col].transform("mean")
    return d[score_col] - mu
