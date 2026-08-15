# -*- coding: utf-8 -*-
"""Nasdaq earnings calendar fetch (surprise / EPS) for PEAD Track B.

Usage:
  .venv\\Scripts\\python.exe -m research.event_alpha.fetch_nasdaq_earnings
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_PX = ROOT / "data" / "cache" / "sp500_pit_close.parquet"
DAY_DIR = ROOT / "data" / "cache" / "nasdaq_earnings"
OUT = ROOT / "data" / "cache" / "nasdaq_earnings_events.parquet"
UA = "Mozilla/5.0 (compatible; QuantDeskResearch/0.1)"


def http_json(url: str, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json,text/plain,*/*",
                    "Origin": "https://www.nasdaq.com",
                    "Referer": "https://www.nasdaq.com/",
                },
            )
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"fail {url}: {last}")


def parse_num(x) -> float:
    if x is None:
        return float("nan")
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "N/A", "n/a", "--", "None"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def fetch_day(day: pd.Timestamp) -> pd.DataFrame:
    DAY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAY_DIR / f"{day.strftime('%Y-%m-%d')}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.strftime('%Y-%m-%d')}"
        data = http_json(url)
        path.write_text(json.dumps(data), encoding="utf-8")
        time.sleep(0.15)
    rows = ((data.get("data") or {}).get("rows")) or []
    out = []
    for r in rows:
        out.append(
            {
                "event_date": day.strftime("%Y-%m-%d"),
                "symbol": str(r.get("symbol") or "").upper(),
                "eps": parse_num(r.get("eps")),
                "eps_forecast": parse_num(r.get("epsForecast")),
                "surprise": parse_num(r.get("surprise")),
                "time": r.get("time"),
                "name": r.get("name"),
            }
        )
    return pd.DataFrame(out)


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    close = pd.read_parquet(CACHE_PX)
    universe = set(close.columns)
    days = list(pd.bdate_range("2021-07-01", "2026-07-29"))
    parts: list[pd.DataFrame] = []
    done = 0
    # modest parallelism; day files are cached so reruns are fast
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_day, pd.Timestamp(d)): d for d in days}
        for fut in as_completed(futs):
            done += 1
            d = futs[fut]
            try:
                df = fut.result()
                if not df.empty:
                    parts.append(df)
            except Exception as e:
                print(f"  skip {pd.Timestamp(d).date()}: {e}", flush=True)
            if done % 50 == 0:
                print(f"  days {done}/{len(days)} parts={len(parts)}", flush=True)
    if not parts:
        print("No earnings rows")
        return
    all_e = pd.concat(parts, ignore_index=True)
    all_e = all_e[all_e["symbol"].isin(universe)].copy()
    all_e.to_parquet(OUT, index=False)
    print(
        f"Saved {OUT} events={len(all_e)} symbols={all_e['symbol'].nunique()} "
        f"with_surprise={all_e['surprise'].notna().sum()}",
        flush=True,
    )


if __name__ == "__main__":
    main()
