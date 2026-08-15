#!/usr/bin/env python3
"""
Research-002: FMP Grades Feature Pipeline (v2)
Changes: revision_breadth -> breadth_efficiency (VIF fix)
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

FMP_API_KEY=os.environ.get("FMP_API_KEY", "")
BASE_URL = "https://financialmodelingprep.com/stable"
SYMBOLS = [
    "NVDA","AMD","INTC","QCOM","MU","MRVL","AMAT","AVGO","TSM","LRCX","KLAC","ON",
    "AAPL","MSFT","GOOG","META","NFLX",
]
LOOKBACK_DAYS = 30
CACHE_DIR = Path("cache/fmp_grades")
OUTPUT_DIR = Path("features")

def fetch_grades(symbol, api_key):
    cache_file = CACHE_DIR / f"{symbol}.parquet"
    if cache_file.exists():
        df = pd.read_parquet(cache_file)
        last_date = pd.to_datetime(df["gradingDate"]).max()
        if (datetime.now() - last_date).days < 7:
            print(f"  {symbol}: cache hit ({len(df)} rows)")
            return df
    url = f"{BASE_URL}/grades?symbol={symbol}&apikey={api_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  {symbol}: HTTP {e.code}")
        return pd.DataFrame()
    except Exception as e:
        print(f"  {symbol}: error {e}")
        return pd.DataFrame()
    if not isinstance(data, list) or len(data) == 0:
        print(f"  {symbol}: 0 records")
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["symbol"] = symbol
    df["gradingDate"] = pd.to_datetime(df["gradingDate"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_file)
    print(f"  {symbol}: fetched {len(df)} rows")
    return df

def fetch_all(api_key):
    frames = []
    for i, sym in enumerate(SYMBOLS):
        df = fetch_grades(sym, api_key)
        if len(df) > 0:
            frames.append(df)
        if i < len(SYMBOLS) - 1:
            time.sleep(1.0)
    if not frames:
        print("ERROR: No data fetched"); sys.exit(1)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_parquet(CACHE_DIR / "all_grades.parquet")
    print(f"\nTotal: {len(combined)} records, {combined['symbol'].nunique()} symbols")
    return combined

def normalize_action(action):
    if not isinstance(action, str): return "maintain"
    a = action.lower().strip()
    if "upgrade" in a or "raise" in a or "increase" in a or "outperform" in a: return "upgrade"
    elif "downgrade" in a or "lower" in a or "reduce" in a or "underperform" in a: return "downgrade"
    return "maintain"

def compute_features(grades_df):
    g = grades_df.copy()
    g["gradingDate"] = pd.to_datetime(g["gradingDate"])
    g["action_norm"] = g["action"].apply(normalize_action)
    g["gradingCompany"] = g.get("gradingCompany", g.get("grading_company", "unknown"))
    date_range = pd.date_range(g["gradingDate"].min(),
                               max(g["gradingDate"].max(), datetime.now()), freq="B")
    results = []
    for symbol in g["symbol"].unique():
        sym_data = g[g["symbol"] == symbol].sort_values("gradingDate")
        for date in date_range:
            ws = date - timedelta(days=LOOKBACK_DAYS)
            w = sym_data[(sym_data["gradingDate"] > ws) & (sym_data["gradingDate"] <= date)]
            n = len(w)
            if n == 0:
                results.append(dict(date=date, symbol=symbol, analyst_revision_count_30d=0,
                    breadth_efficiency=0.5, grade_sentiment_score=0.0,
                    n_upgrades=0, n_downgrades=0, n_maintains=0, n_unique_companies=0))
                continue
            n_up = (w["action_norm"]=="upgrade").sum()
            n_down = (w["action_norm"]=="downgrade").sum()
            n_maintain = (w["action_norm"]=="maintain").sum()
            n_unique = w["gradingCompany"].nunique()
            results.append(dict(date=date, symbol=symbol,
                analyst_revision_count_30d=n,
                breadth_efficiency=n_unique/n,
                grade_sentiment_score=(n_up-n_down)/n,
                n_upgrades=n_up, n_downgrades=n_down,
                n_maintains=n_maintain, n_unique_companies=n_unique))
    return pd.DataFrame(results)

def compute_vif(fdf):
    cols = ["analyst_revision_count_30d","breadth_efficiency","grade_sentiment_score"]
    X = fdf[cols].dropna().values
    Xs = (X - X.mean(0)) / X.std(0)
    vifs = []
    for i, c in enumerate(cols):
        y = Xs[:, i]; Xo = np.delete(Xs, i, 1)
        Xo = np.column_stack([Xo, np.ones(len(Xo))])
        beta, *_ = np.linalg.lstsq(Xo, y, rcond=None)
        yp = Xo @ beta
        r2 = 1 - np.sum((y-yp)**2)/np.sum((y-y.mean())**2)
        vifs.append(dict(feature=c, VIF=round(1/(1-r2),2) if r2<1 else 999, R2=round(r2,4)))
    return pd.DataFrame(vifs)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fetch",action="store_true")
    p.add_argument("--compute",action="store_true")
    p.add_argument("--all",action="store_true")
    p.add_argument("--api-key",type=str,default="")
    args = p.parse_args()
    ak = args.api_key or FMP_API_KEY
    if not ak and (args.fetch or args.all):
        print("ERROR: No API key"); sys.exit(1)
    if args.fetch or args.all:
        print("="*60); print("FETCHING FMP GRADES"); print("="*60)
        gdf = fetch_all(ak)
    else:
        cf = CACHE_DIR/"all_grades.parquet"
        if not cf.exists(): print(f"ERROR: {cf} not found"); sys.exit(1)
        gdf = pd.read_parquet(cf)
        print(f"Loaded {len(gdf)} cached records, {gdf['symbol'].nunique()} symbols")
    if args.compute or args.all:
        print("\n"+"="*60); print("COMPUTING FEATURES (v2)"); print("="*60)
        fdf = compute_features(gdf)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR/"analyst_features_v2.parquet"
        fdf.to_parquet(out)
        print(f"\nSaved: {out} ({len(fdf)} rows)")
        print("\nFeature Stats:")
        for c in ["analyst_revision_count_30d","breadth_efficiency","grade_sentiment_score"]:
            v = fdf[c]
            print(f"  {c:35s} mean={v.mean():.4f} std={v.std():.4f} [{v.min():.4f}, {v.max():.4f}]")
        print("\nVIF:")
        vdf = compute_vif(fdf)
        for _, r in vdf.iterrows():
            s = "OK" if r.VIF<5 else "!!" if r.VIF<10 else "XX"
            print(f"  [{s}] {r.feature:35s} VIF={r.VIF:8.2f}  R2={r.R2:.4f}")
        print("\nCorrelation:")
        cols = ["analyst_revision_count_30d","breadth_efficiency","grade_sentiment_score"]
        print(fdf[cols].corr().round(4).to_string())
        print("\nCoverage:")
        for sym in sorted(fdf["symbol"].unique()):
            sd = fdf[fdf["symbol"]==sym]
            act = (sd["analyst_revision_count_30d"]>0).sum()
            print(f"  {sym:>6}: {len(sd):>5} days, {act:>5} active ({act/len(sd)*100:.0f}%)")

if __name__ == "__main__":
    main()
