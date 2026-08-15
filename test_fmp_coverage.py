#!/usr/bin/env python3
"""Test FMP /stable/grades coverage for 17 symbols."""
import json, sys, urllib.request, urllib.error, time

API_KEY=*** len(sys.argv) > 1 else ""
if not API_KEY:
    print("Usage: python3 test_fmp_coverage.py <FMP_API_KEY>"); sys.exit(1)

SYMBOLS = [
    "NVDA","AMD","INTC","QCOM","MU","MRVL","AMAT","AVGO","TSM","LRCX","KLAC","ON",
    "AAPL","MSFT","GOOG","META","NFLX",
]

print("="*60)
print("FMP /stable/grades Coverage Test (17 symbols)")
print("="*60)
ok=0
for sym in SYMBOLS:
    url = f"https://financialmodelingprep.com/stable/grades?symbol={sym}&apikey={API_KEY}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data,list) and len(data)>0:
                dates=sorted([d.get("gradingDate","") for d in data if d.get("gradingDate")])
                firms=len(set(d.get("gradingCompany","") for d in data))
                dr=f"{dates[0]}~{dates[-1]}" if dates else "nodate"
                print(f"OK {sym:>6}: {len(data):>4} records, {firms:>3} firms, {dr}")
                ok+=1
            else:
                print(f"-- {sym:>6}: 0 records")
    except urllib.error.HTTPError as e:
        print(f"XX {sym:>6}: HTTP {e.code}")
    except Exception as e:
        print(f"XX {sym:>6}: {e}")
    time.sleep(0.5)

print(f"\nResult: {ok}/{len(SYMBOLS)} accessible")
if ok>=12: print("VERDICT: FULL UNIVERSE OK")
elif ok>=8: print("VERDICT: REDUCED (marginal)")
elif ok>=3: print("VERDICT: INSUFFICIENT")
else: print("VERDICT: CRITICAL - need upgrade or alt source")
