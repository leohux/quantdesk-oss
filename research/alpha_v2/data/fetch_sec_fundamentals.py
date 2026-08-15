# -*- coding: utf-8 -*-
"""Fetch + cache SEC EDGAR companyfacts for PIT fundamentals.

Uses filing date (`filed`) as availability — true point-in-time.

Usage:
  .venv\\Scripts\\python.exe -m research.alpha_v2.data.fetch_sec_fundamentals
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_PX = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
TICKERS_JSON = ROOT / "data" / "cache" / "sec_company_tickers.json"
FACTS_DIR = ROOT / "data" / "cache" / "sec_facts"
OUT_PANEL = ROOT / "data" / "cache" / "sec_fundamentals_pit.parquet"
# SEC requires a descriptive UA with contact email (403 otherwise).
UA = "QuantDesk Research quantdesk@example.com"

# Prefer these concept aliases (first hit wins per row-building)
CONCEPTS = {
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "assets": ["Assets"],
    "ni": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "shares": [
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "opinc": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "assets_current": ["AssetsCurrent"],
    "liab_current": ["LiabilitiesCurrent"],
    "long_debt": ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
}


def http_json(url: str, retries: int = 5) -> dict | list:
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=120) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8"))
        except HTTPError as e:
            last = e
            if e.code in (429, 503):
                time.sleep(2 ** i)
                continue
            if e.code == 404:
                return {}
            raise
        except (URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
        except Exception as e:
            # IncompleteRead etc.
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Failed {url}: {last}")


def load_ticker_map() -> dict[str, int]:
    if TICKERS_JSON.exists():
        data = json.loads(TICKERS_JSON.read_text(encoding="utf-8"))
    else:
        data = http_json("https://www.sec.gov/files/company_tickers.json")
        TICKERS_JSON.parent.mkdir(parents=True, exist_ok=True)
        TICKERS_JSON.write_text(json.dumps(data), encoding="utf-8")
    out = {}
    for row in data.values():
        t = str(row.get("ticker", "")).upper()
        if t:
            out[t] = int(row["cik_str"])
    return out


def extract_series(facts: dict, concept_names: list[str]) -> pd.DataFrame:
    """Return DataFrame[filed, end, val, form] from first available concept."""
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    dei = (facts.get("facts") or {}).get("dei") or {}
    rows = []
    for name in concept_names:
        block = usgaap.get(name) or dei.get(name)
        if not block:
            continue
        units = block.get("units") or {}
        # prefer USD / shares / USD/shares
        for uk in ("USD", "shares", "USD/shares", "pure"):
            arr = units.get(uk)
            if not arr:
                continue
            for rec in arr:
                filed = rec.get("filed")
                end = rec.get("end")
                val = rec.get("val")
                if filed is None or end is None or val is None:
                    continue
                form = rec.get("form") or ""
                # keep 10-K/10-Q primarily
                if form and form not in ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A"):
                    continue
                rows.append(
                    {
                        "filed": pd.Timestamp(filed),
                        "end": pd.Timestamp(end),
                        "val": float(val),
                        "form": form,
                        "concept": name,
                    }
                )
            if rows:
                break
        if rows:
            break
    if not rows:
        return pd.DataFrame(columns=["filed", "end", "val", "form", "concept"])
    df = pd.DataFrame(rows).sort_values(["filed", "end"])
    # On same filed date keep latest period end
    df = df.drop_duplicates(subset=["filed"], keep="last")
    return df.reset_index(drop=True)


def fetch_facts(cik: int) -> dict:
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = FACTS_DIR / f"{cik:010d}.json"
    if path.exists() and path.stat().st_size > 50:
        return json.loads(path.read_text(encoding="utf-8"))
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    data = http_json(url)
    path.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(0.12)  # ~8 req/s polite
    return data


def build_symbol_pit(symbol: str, facts: dict) -> pd.DataFrame:
    parts = {}
    for key, names in CONCEPTS.items():
        s = extract_series(facts, names)
        if s.empty:
            continue
        parts[key] = s.set_index("filed")["val"].sort_index()
    if not parts:
        return pd.DataFrame()
    # union of filing dates
    idx = sorted(set().union(*[set(s.index) for s in parts.values()]))
    out = pd.DataFrame(index=pd.DatetimeIndex(idx, name="filed"))
    for k, s in parts.items():
        out[k] = s.reindex(out.index)
    out = out.sort_index().ffill()
    out["symbol"] = symbol
    return out.reset_index()


def main() -> None:
    close = pd.read_parquet(CACHE_PX)
    symbols = [c for c in close.columns if c != "SPY"]
    tmap = load_ticker_map()
    print(f"symbols={len(symbols)} mapped={sum(1 for s in symbols if s in tmap)}")

    rows = []
    miss = []
    for i, sym in enumerate(symbols, 1):
        cik = tmap.get(sym)
        if cik is None:
            # try without dots BRK.B -> BRK-B SEC uses BRK-B
            alt = sym.replace(".", "-")
            cik = tmap.get(alt)
        if cik is None:
            miss.append(sym)
            continue
        try:
            facts = fetch_facts(cik)
            if not facts:
                miss.append(sym)
                continue
            pit = build_symbol_pit(sym, facts)
            if not pit.empty:
                rows.append(pit)
        except Exception as e:
            print(f"  FAIL {sym}: {e}")
            miss.append(sym)
        if i % 25 == 0:
            print(f"  {i}/{len(symbols)} panels={len(rows)} miss={len(miss)}")

    if not rows:
        print("No fundamental panels built")
        return
    panel = pd.concat(rows, ignore_index=True)
    panel.to_parquet(OUT_PANEL, index=False)
    print(f"Saved {OUT_PANEL} rows={len(panel)} symbols={panel['symbol'].nunique()}")
    print(f"missing_cik_or_facts={len(miss)} sample={miss[:20]}")


if __name__ == "__main__":
    main()
