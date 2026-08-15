# -*- coding: utf-8 -*-
"""Cursor-brain classic strategies from well-known quant literature.

Sources adapted to single-ticker, close-only daily signals (no look-ahead):
- Antonacci Dual Momentum (absolute momentum filter)
- Turtle / Donchian channel breakout (+ trend filter)
- Connors RSI(2) mean reversion
- Golden/Death cross (50/200)
- Time-series momentum 12-1 (Moskowitz-style)
- SMA200 trend + pullback reclaim
- Bollinger squeeze breakout
- Keltner/ATR-proxy breakout
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import textwrap
import time
from pathlib import Path

INBOX = Path(os.environ.get("QUANTDESK_ROOT", str(Path(__file__).resolve().parents[1]))) / "data/store/alpha_miner/cursor_inbox.jsonl"
BATCH = time.strftime("%H%M%S")
RNG = random.Random(int(time.time()) ^ 0xBEEF)

CODES = {
    # Absolute momentum: hold when trailing return > threshold (Antonacci abs mom)
    "abs_mom": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            look = int(params.get("lookback", 252))
            thr = float(params.get("abs_thresh", 0.0))
            ret = close / close.shift(look) - 1.0
            up = ret > thr
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Dual-horizon momentum concurrence (short + long both positive)
    "dual_abs_mom": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            s = int(params.get("short_n", 63))
            l = int(params.get("long_n", 252))
            th_s = float(params.get("th_short", 0.0))
            th_l = float(params.get("th_long", 0.0))
            rs = close / close.shift(s) - 1.0
            rl = close / close.shift(l) - 1.0
            up = (rs > th_s) & (rl > th_l)
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Turtle / Donchian breakout with long MA filter
    "turtle": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            entry_n = int(params.get("entry_n", 20))
            exit_n = int(params.get("exit_n", 10))
            trend = int(params.get("trend_ma", 200))
            hh = close.rolling(entry_n).max().shift(1)
            ll = close.rolling(exit_n).min().shift(1)
            ma = close.rolling(trend).mean()
            entries = (close > hh) & (close > ma)
            exits = close < ll
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Connors RSI(2): buy RSI2 < X in uptrend, exit RSI2 > Y
    "connors_rsi2": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            period = int(params.get("rsi_period", 2))
            lo = float(params.get("oversold", 10))
            hi = float(params.get("overbought", 65))
            trend = int(params.get("trend_ma", 200))
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            ma = close.rolling(trend).mean()
            entries = (rsi < lo) & (close > ma)
            exits = rsi > hi
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Golden cross 50/200
    "golden_cross": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            fast = int(params.get("fast", 50))
            slow = int(params.get("slow", 200))
            if fast >= slow:
                fast = max(1, slow - 1)
            f = close.rolling(fast).mean()
            s = close.rolling(slow).mean()
            up = f > s
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # 12-1 time series momentum (skip most recent month)
    "tsmom_12_1": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            long = int(params.get("long_n", 252))
            skip = int(params.get("skip_n", 21))
            thr = float(params.get("abs_thresh", 0.0))
            # return from t-long to t-skip (exclude last month)
            past = close.shift(skip)
            ret = past / past.shift(long - skip) - 1.0
            up = ret > thr
            entries = up & (~up.shift(1).fillna(False))
            exits = (~up) & up.shift(1).fillna(False)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # SMA200 trend + short MA reclaim (pullback buy)
    "sma200_pullback": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            trend = int(params.get("trend_ma", 200))
            fast = int(params.get("fast", 10))
            slow = int(params.get("slow", 20))
            if fast >= slow:
                fast = max(1, slow - 1)
            ma = close.rolling(trend).mean()
            f = close.rolling(fast).mean()
            s = close.rolling(slow).mean()
            uptrend = close > ma
            cross_up = (f > s) & (f.shift(1) <= s.shift(1))
            cross_dn = (f < s) & (f.shift(1) >= s.shift(1))
            entries = cross_up & uptrend
            exits = cross_dn | (close < ma)
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Bollinger squeeze -> breakout (TTM-style)
    "bb_squeeze": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            n = int(params.get("bb_period", 20))
            k = float(params.get("bb_std", 2.0))
            sq = float(params.get("squeeze_bw", 0.05))
            trend = int(params.get("trend_ma", 50))
            mid = close.rolling(n).mean()
            sd = close.rolling(n).std()
            upper = mid + k * sd
            lower = mid - k * sd
            bw = (upper - lower) / mid.replace(0, 1e-9)
            squeeze = bw < sq
            ma = close.rolling(trend).mean()
            entries = squeeze.shift(1).fillna(False) & (close > upper) & (close > ma)
            exits = close < mid
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # ATR-proxy channel breakout (Wilder/Keltner style on close)
    "atr_break": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            atr_n = int(params.get("atr_n", 14))
            mult = float(params.get("atr_mult", 1.5))
            mid_n = int(params.get("mid_n", 20))
            trend = int(params.get("trend_ma", 100))
            atr = close.diff().abs().rolling(atr_n).mean()
            mid = close.rolling(mid_n).mean()
            upper = mid + mult * atr
            ma = close.rolling(trend).mean()
            entries = (close > upper) & (close > ma)
            exits = close < mid
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
    # Inverse Donchian mean-reversion for stocks (fade N-day extremes)
    "inv_donchian": textwrap.dedent(
        '''\
        import pandas as pd
        def generate_signals(close: pd.Series, params: dict):
            n = int(params.get("channel", 15))
            trend = int(params.get("trend_ma", 200))
            ll = close.rolling(n).min().shift(1)
            hh = close.rolling(n).max().shift(1)
            mid = close.rolling(n).mean()
            ma = close.rolling(trend).mean()
            # buy weakness in uptrend, exit mid
            entries = (close <= ll) & (close > ma)
            exits = close >= mid
            return entries.fillna(False), exits.fillna(False)
        '''
    ),
}


def tag(base: str) -> str:
    h = hashlib.md5(f"{base}-{BATCH}-{RNG.random()}".encode()).hexdigest()[:5]
    return f"{base}-{BATCH}-{h}"


INDEX = ["SPY", "QQQ", "IWM", "XLK", "XLF"]
MEGA = ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "AMD", "NFLX"]
GROWTH = ["PLTR", "HOOD", "SOFI", "UPST", "TSLA", "AMD", "NVDA"]

rows: list[dict] = []

# --- Classic named recipes (high priority) ---
CLASSICS = [
    # Connors RSI2 on indices (famous)
    *[
        {
            "name": tag(f"Classic-ConnorsRSI2-{sym}-os{lo}"),
            "type": "classic_connors_rsi2",
            "description": "Connors RSI(2) mean-reversion in uptrend (literature classic)",
            "symbols": [sym],
            "params": {
                "rsi_period": 2,
                "oversold": lo,
                "overbought": hi,
                "trend_ma": 200,
                "stop_loss": -0.08,
                "take_profit": 0.10,
            },
            "code": CODES["connors_rsi2"],
        }
        for sym in INDEX + ["AAPL", "MSFT", "QQQ"]
        for lo, hi in [(5, 65), (10, 65), (10, 70), (15, 70), (25, 70)]
    ],
    # Antonacci absolute momentum
    *[
        {
            "name": tag(f"Classic-AbsMom-{sym}-{look}"),
            "type": "classic_abs_momentum",
            "description": "Antonacci absolute momentum (trailing return filter)",
            "symbols": [sym],
            "params": {
                "lookback": look,
                "abs_thresh": thr,
                "stop_loss": -0.12,
                "take_profit": 0.30,
            },
            "code": CODES["abs_mom"],
        }
        for sym in INDEX + MEGA
        for look in [126, 189, 252]
        for thr in [0.0, 0.02, 0.05]
    ],
    # Dual absolute momentum
    *[
        {
            "name": tag(f"Classic-DualAbsMom-{sym}-{s}x{l}"),
            "type": "classic_dual_abs_mom",
            "description": "Dual-horizon absolute momentum (short+long)",
            "symbols": [sym],
            "params": {
                "short_n": s,
                "long_n": l,
                "th_short": 0.0,
                "th_long": thr,
                "stop_loss": -0.12,
                "take_profit": 0.25,
            },
            "code": CODES["dual_abs_mom"],
        }
        for sym in INDEX + MEGA[:6]
        for s, l in [(63, 252), (42, 189), (21, 126), (63, 189)]
        for thr in [0.0, 0.02]
    ],
    # Turtle / Donchian
    *[
        {
            "name": tag(f"Classic-Turtle-{sym}-e{en}-x{ex}"),
            "type": "classic_turtle_donchian",
            "description": "Turtle/Donchian breakout + SMA200 filter",
            "symbols": [sym],
            "params": {
                "entry_n": en,
                "exit_n": ex,
                "trend_ma": tm,
                "stop_loss": -0.10,
                "take_profit": 0.25,
            },
            "code": CODES["turtle"],
        }
        for sym in INDEX + MEGA + GROWTH[:4]
        for en, ex in [(20, 10), (55, 20), (40, 15), (10, 5), (25, 10)]
        for tm in [100, 200]
    ],
    # Golden cross
    *[
        {
            "name": tag(f"Classic-GoldenCross-{sym}-{f}x{s}"),
            "type": "classic_golden_cross",
            "description": "Golden cross MA trend (classic)",
            "symbols": [sym],
            "params": {"fast": f, "slow": s, "stop_loss": -0.12, "take_profit": 0.30},
            "code": CODES["golden_cross"],
        }
        for sym in INDEX + MEGA
        for f, s in [(50, 200), (20, 100), (10, 50), (30, 150)]
    ],
    # 12-1 TSMOM
    *[
        {
            "name": tag(f"Classic-TSMOM12_1-{sym}"),
            "type": "classic_tsmom_12_1",
            "description": "Time-series momentum 12-1 (skip 1 month)",
            "symbols": [sym],
            "params": {
                "long_n": 252,
                "skip_n": skip,
                "abs_thresh": thr,
                "stop_loss": -0.12,
                "take_profit": 0.30,
            },
            "code": CODES["tsmom_12_1"],
        }
        for sym in INDEX + MEGA
        for skip in [10, 21]
        for thr in [0.0, 0.02]
    ],
    # SMA200 pullback
    *[
        {
            "name": tag(f"Classic-SMA200PB-{sym}-f{f}"),
            "type": "classic_sma200_pullback",
            "description": "SMA200 trend + short MA reclaim pullback",
            "symbols": [sym],
            "params": {
                "trend_ma": 200,
                "fast": f,
                "slow": s,
                "stop_loss": -0.08,
                "take_profit": 0.15,
            },
            "code": CODES["sma200_pullback"],
        }
        for sym in INDEX + MEGA
        for f, s in [(5, 20), (10, 20), (10, 30), (8, 21)]
    ],
    # BB squeeze
    *[
        {
            "name": tag(f"Classic-BBSqueeze-{sym}"),
            "type": "classic_bb_squeeze",
            "description": "Bollinger bandwidth squeeze then upside break",
            "symbols": [sym],
            "params": {
                "bb_period": n,
                "bb_std": 2.0,
                "squeeze_bw": bw,
                "trend_ma": tm,
                "stop_loss": -0.08,
                "take_profit": 0.18,
            },
            "code": CODES["bb_squeeze"],
        }
        for sym in INDEX + MEGA + GROWTH[:5]
        for n in [20, 15]
        for bw in [0.04, 0.05, 0.06, 0.08]
        for tm in [50, 100]
    ],
    # ATR break
    *[
        {
            "name": tag(f"Classic-ATRBreak-{sym}-m{int(m*10)}"),
            "type": "classic_atr_break",
            "description": "ATR-proxy channel breakout (Keltner-like)",
            "symbols": [sym],
            "params": {
                "atr_n": 14,
                "atr_mult": m,
                "mid_n": mid,
                "trend_ma": tm,
                "stop_loss": -0.10,
                "take_profit": 0.22,
            },
            "code": CODES["atr_break"],
        }
        for sym in MEGA + GROWTH
        for m in [1.0, 1.5, 2.0]
        for mid in [20, 30]
        for tm in [50, 100]
    ],
    # Inverse Donchian MR
    *[
        {
            "name": tag(f"Classic-InvDonchian-{sym}-n{n}"),
            "type": "classic_inv_donchian",
            "description": "Inverse Donchian mean-reversion for equities",
            "symbols": [sym],
            "params": {
                "channel": n,
                "trend_ma": 200,
                "stop_loss": -0.07,
                "take_profit": 0.12,
            },
            "code": CODES["inv_donchian"],
        }
        for sym in INDEX + ["AAPL", "MSFT", "AMZN"]
        for n in [5, 10, 15, 20]
    ],
]

rows.extend(CLASSICS)

# Extra random classic variants
for _ in range(80):
    sym = RNG.choice(INDEX + MEGA)
    rows.append(
        {
            "name": tag(f"Classic-ConnorsRSI2-{sym}-x"),
            "type": "classic_connors_rsi2",
            "description": "Connors RSI2 variant",
            "symbols": [sym],
            "params": {
                "rsi_period": 2,
                "oversold": RNG.choice([5, 8, 10, 15, 20]),
                "overbought": RNG.choice([60, 65, 70, 75]),
                "trend_ma": RNG.choice([100, 150, 200]),
                "stop_loss": -0.08,
                "take_profit": 0.12,
            },
            "code": CODES["connors_rsi2"],
        }
    )

seen = set()
uniq = []
for r in rows:
    if r["name"] in seen:
        continue
    seen.add(r["name"])
    uniq.append(r)
RNG.shuffle(uniq)
# Prefer classics first half, then rest
rows = uniq[:700]

INBOX.parent.mkdir(parents=True, exist_ok=True)
with INBOX.open("a", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
n = sum(1 for line in INBOX.open(encoding="utf-8") if line.strip())
print(f"classic_batch={BATCH} appended={len(rows)} inbox_total={n}")
# show family counts
from collections import Counter

c = Counter(r["type"] for r in rows)
print("families:", dict(c))
