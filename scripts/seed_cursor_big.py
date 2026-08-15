# -*- coding: utf-8 -*-
"""Seed diverse Cursor hybrids with unique batch ids (no external LLM)."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import textwrap
import time
from pathlib import Path

INBOX = Path(os.environ.get("QUANTDESK_ROOT", str(Path(__file__).resolve().parents[1]))) / "data/store/alpha_miner/cursor_inbox.jsonl"
BATCH = time.strftime("%H%M%S")
RNG = random.Random(int(time.time()) ^ 0xC0FFEE)

CODES = {
    "rsi_trend": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            period = int(params.get("rsi_period", 14))
            lo = float(params.get("oversold", 30))
            hi = float(params.get("overbought", 70))
            trend = int(params.get("trend_ma", 50))
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            ma = close.rolling(trend).mean()
            entries = (rsi < lo) & (rsi.shift(1) >= lo) & (close > ma)
            exits = (rsi > hi) & (rsi.shift(1) <= hi)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "dual_mom": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            short = int(params.get("short_n", 20))
            long = int(params.get("long_n", 60))
            th_s = float(params.get("th_short", 0.0))
            th_l = float(params.get("th_long", 0.0))
            ms = close / close.shift(short) - 1.0
            ml = close / close.shift(long) - 1.0
            up = (ms > th_s) & (ml > th_l)
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "trend_mom": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            fast = int(params.get("fast", 10))
            slow = int(params.get("slow", 50))
            mom_n = int(params.get("mom_n", 20))
            mom_th = float(params.get("mom_th", 0.02))
            if fast >= slow:
                fast = max(1, slow - 1)
            f = close.rolling(fast).mean()
            s = close.rolling(slow).mean()
            mom = close / close.shift(mom_n) - 1.0
            up = (f > s) & (mom > mom_th)
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "vol_mom": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            vol_w = int(params.get("vol_window", 20))
            vol_cap = float(params.get("vol_cap", 0.02))
            mom_n = int(params.get("mom_n", 20))
            mom_th = float(params.get("mom_th", 0.03))
            trend = int(params.get("trend_ma", 50))
            vol = close.pct_change().rolling(vol_w).std()
            mom = close / close.shift(mom_n) - 1.0
            ma = close.rolling(trend).mean()
            up = (vol.shift(1) < vol_cap) & (mom > mom_th) & (close > ma)
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "squeeze": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            vol_w = int(params.get("vol_window", 20))
            sq = float(params.get("squeeze_threshold", 0.015))
            br = int(params.get("breakout_window", 20))
            trend = int(params.get("trend_ma", 50))
            exit_w = int(params.get("exit_window", 10))
            vol = close.pct_change().rolling(vol_w).std()
            squeeze = vol < sq
            hh = close.rolling(br).max().shift(1)
            ma = close.rolling(trend).mean()
            entries = squeeze.shift(1).fillna(False) & (close > hh) & (close > ma)
            exits = close < close.rolling(exit_w).min().shift(1)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "surge": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            surge = float(params.get("buy_surge", 0.02))
            cap = float(params.get("buy_cap", 0.08))
            trend = int(params.get("trend_ma", 50))
            hold = int(params.get("max_hold_days", 5))
            day_ret = close / close.shift(1) - 1.0
            ma = close.rolling(trend).mean()
            entries = ((day_ret >= surge) & (day_ret < cap) & (close > ma)).fillna(False)
            exits = (close < close.shift(hold)).fillna(False)
            return entries, exits
        '''
    ),
    "pullback": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            fast = int(params.get("fast", 10))
            slow = int(params.get("slow", 30))
            trend = int(params.get("trend_ma", 100))
            if fast >= slow:
                fast = max(1, slow - 1)
            f = close.rolling(fast).mean()
            s = close.rolling(slow).mean()
            ma = close.rolling(trend).mean()
            cross_up = (f > s) & (f.shift(1) <= s.shift(1))
            cross_dn = (f < s) & (f.shift(1) >= s.shift(1))
            entries = cross_up & (close > ma)
            exits = cross_dn
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "bb_trend": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            n = int(params.get("bb_period", 20))
            k = float(params.get("bb_std", 2.0))
            trend = int(params.get("trend_ma", 50))
            mid = close.rolling(n).mean()
            sd = close.rolling(n).std()
            lower = mid - k * sd
            ma = close.rolling(trend).mean()
            entries = (close < lower) & (close > ma)
            exits = close > mid
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    "chan_rsi": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            ch = int(params.get("channel", 20))
            period = int(params.get("rsi_period", 10))
            lo = float(params.get("oversold", 35))
            trend = int(params.get("trend_ma", 100))
            hh = close.rolling(ch).max().shift(1)
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            ma = close.rolling(trend).mean()
            entries = (close > hh) & (rsi > lo) & (close > ma)
            exits = close < close.rolling(max(5, ch // 2)).min().shift(1)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
}

SYMS = [
    "SPY", "QQQ", "IWM", "XLK", "XLF",
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "AMD", "NFLX", "TSLA",
    "PLTR", "HOOD", "SOFI", "UPST", "RIVN",
]
GROWTH = ["SOFI", "PLTR", "HOOD", "UPST", "RIVN", "TSLA", "NVDA", "AMD", "AMC"]


def tag(base: str) -> str:
    h = hashlib.md5(f"{base}-{BATCH}-{RNG.random()}".encode()).hexdigest()[:6]
    return f"{base}-{BATCH}-{h}"


rows: list[dict] = []

# Random sample grids (fresh each run)
for _ in range(120):
    sym = RNG.choice(["SPY", "QQQ", "IWM", "AAPL", "MSFT", "META", "AMZN", "GOOGL"])
    period = RNG.choice([2, 3, 5, 7, 10, 14])
    lo = RNG.choice([8, 10, 15, 20, 25, 30])
    hi = RNG.choice([70, 75, 80, 85, 90, 92])
    if lo >= hi - 25:
        continue
    tma = RNG.choice([50, 100, 150, 200])
    rows.append(
        {
            "name": tag(f"Cursor-RSITrend-{sym}-p{period}"),
            "type": "hybrid_rsi_trend",
            "description": "Cursor RSI+trend",
            "symbols": [sym],
            "params": {
                "rsi_period": period,
                "oversold": lo,
                "overbought": hi,
                "trend_ma": tma,
                "stop_loss": RNG.choice([-0.06, -0.08, -0.10]),
                "take_profit": RNG.choice([0.12, 0.15, 0.20]),
            },
            "code": CODES["rsi_trend"],
        }
    )

for _ in range(100):
    sym = RNG.choice(SYMS)
    sn = RNG.choice([5, 8, 10, 15, 20])
    ln = RNG.choice([40, 60, 90, 120])
    if sn >= ln:
        continue
    rows.append(
        {
            "name": tag(f"Cursor-DualMom-{sym}-{sn}x{ln}"),
            "type": "hybrid_dual_mom",
            "description": "Cursor dual mom",
            "symbols": [sym],
            "params": {
                "short_n": sn,
                "long_n": ln,
                "th_short": RNG.choice([0.0, 0.01, 0.02, 0.03]),
                "th_long": RNG.choice([0.0, 0.02, 0.05]),
                "stop_loss": -0.10,
                "take_profit": 0.20,
            },
            "code": CODES["dual_mom"],
        }
    )

for _ in range(80):
    sym = RNG.choice(SYMS)
    f = RNG.choice([5, 8, 10, 12, 15])
    s = RNG.choice([40, 50, 60, 80, 100])
    if f >= s:
        continue
    rows.append(
        {
            "name": tag(f"Cursor-TrendMom-{sym}-{f}-{s}"),
            "type": "hybrid_trend_mom",
            "description": "Cursor trend+mom",
            "symbols": [sym],
            "params": {
                "fast": f,
                "slow": s,
                "mom_n": RNG.choice([10, 15, 20, 30]),
                "mom_th": RNG.choice([0.01, 0.02, 0.03, 0.05]),
                "stop_loss": -0.08,
                "take_profit": 0.15,
            },
            "code": CODES["trend_mom"],
        }
    )

for _ in range(80):
    sym = RNG.choice(SYMS)
    rows.append(
        {
            "name": tag(f"Cursor-VolMom-{sym}"),
            "type": "hybrid_vol_mom",
            "description": "Cursor vol-then-mom",
            "symbols": [sym],
            "params": {
                "vol_window": RNG.choice([15, 20, 30]),
                "vol_cap": RNG.choice([0.01, 0.012, 0.015, 0.018, 0.02, 0.025]),
                "mom_n": RNG.choice([10, 15, 20, 30]),
                "mom_th": RNG.choice([0.02, 0.03, 0.05]),
                "trend_ma": RNG.choice([50, 100]),
                "stop_loss": -0.08,
                "take_profit": 0.18,
            },
            "code": CODES["vol_mom"],
        }
    )

for _ in range(70):
    sym = RNG.choice(SYMS)
    rows.append(
        {
            "name": tag(f"Cursor-Squeeze-{sym}"),
            "type": "hybrid_squeeze_break",
            "description": "Cursor squeeze",
            "symbols": [sym],
            "params": {
                "vol_window": 20,
                "squeeze_threshold": RNG.choice([0.01, 0.012, 0.015, 0.02]),
                "breakout_window": RNG.choice([15, 20, 30, 40]),
                "trend_ma": RNG.choice([20, 50, 100]),
                "exit_window": RNG.choice([8, 10, 15]),
                "stop_loss": -0.08,
                "take_profit": 0.18,
            },
            "code": CODES["squeeze"],
        }
    )

for _ in range(70):
    sym = RNG.choice(GROWTH)
    sg = RNG.choice([0.015, 0.02, 0.025, 0.03])
    cap = RNG.choice([0.07, 0.08, 0.10, 0.12])
    if sg >= cap:
        continue
    rows.append(
        {
            "name": tag(f"Cursor-Surge-{sym}"),
            "type": "hybrid_surge_trend",
            "description": "Cursor surge",
            "symbols": [sym],
            "params": {
                "buy_surge": sg,
                "buy_cap": cap,
                "trend_ma": RNG.choice([20, 50, 100]),
                "max_hold_days": RNG.choice([3, 4, 5, 7]),
                "stop_loss": -0.08,
                "take_profit": 0.15,
            },
            "code": CODES["surge"],
        }
    )

for _ in range(60):
    sym = RNG.choice(SYMS)
    f = RNG.choice([5, 8, 10])
    s = RNG.choice([20, 30, 40])
    if f >= s:
        continue
    rows.append(
        {
            "name": tag(f"Cursor-Pullback-{sym}"),
            "type": "hybrid_pullback",
            "description": "Cursor pullback",
            "symbols": [sym],
            "params": {
                "fast": f,
                "slow": s,
                "trend_ma": RNG.choice([50, 100, 150]),
                "stop_loss": -0.07,
                "take_profit": 0.14,
            },
            "code": CODES["pullback"],
        }
    )

for _ in range(50):
    sym = RNG.choice(SYMS)
    rows.append(
        {
            "name": tag(f"Cursor-BBTrend-{sym}"),
            "type": "hybrid_bb_trend",
            "description": "Cursor BB trend",
            "symbols": [sym],
            "params": {
                "bb_period": RNG.choice([10, 15, 20]),
                "bb_std": RNG.choice([1.5, 2.0, 2.5]),
                "trend_ma": RNG.choice([50, 100]),
                "stop_loss": -0.07,
                "take_profit": 0.12,
            },
            "code": CODES["bb_trend"],
        }
    )

for _ in range(50):
    sym = RNG.choice(SYMS)
    rows.append(
        {
            "name": tag(f"Cursor-ChanRSI-{sym}"),
            "type": "hybrid_channel_rsi",
            "description": "Cursor channel+RSI",
            "symbols": [sym],
            "params": {
                "channel": RNG.choice([15, 20, 40, 55]),
                "rsi_period": RNG.choice([7, 10, 14]),
                "oversold": RNG.choice([30, 35, 40]),
                "trend_ma": RNG.choice([50, 100, 200]),
                "stop_loss": -0.08,
                "take_profit": 0.18,
            },
            "code": CODES["chan_rsi"],
        }
    )

RNG.shuffle(rows)
# unique names
seen = set()
uniq = []
for r in rows:
    if r["name"] in seen:
        continue
    seen.add(r["name"])
    uniq.append(r)
rows = uniq[:600]

INBOX.parent.mkdir(parents=True, exist_ok=True)
with INBOX.open("a", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
n = sum(1 for line in INBOX.open(encoding="utf-8") if line.strip())
print(f"batch={BATCH} appended={len(rows)} inbox_total={n}")
