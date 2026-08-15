# -*- coding: utf-8 -*-
"""Path A: parametric / template factor search — includes HYBRID templates.

Hybrid = combine 2+ signals (trend + momentum, squeeze + breakout, RSI + MA, etc.)
and optionally risk exits (stop/take via params that backtest runner can apply).
"""
from __future__ import annotations

import random
import uuid
from typing import Any

SYMBOLS_CORE = [
    ["AAPL"],
    ["MSFT"],
    ["NVDA"],
    ["AMZN"],
    ["META"],
    ["GOOGL"],
    ["TSLA"],
    ["AMD"],
    ["NFLX"],
    ["SPY"],
    ["QQQ"],
    ["IWM"],
    ["XLK"],
    ["XLF"],
    ["SOFI"],
    ["PLTR"],
    ["HOOD"],
    ["UPST"],
]

# ---------- single-factor templates ----------
TEMPLATES: list[dict[str, Any]] = [
    {
        "type": "ma_cross",
        "name_prefix": "Alpha-MA",
        "description": "Template MA crossover trend",
        "params_space": {
            "fast": [5, 8, 10, 12, 15, 20, 25],
            "slow": [30, 40, 50, 60, 80, 100, 120],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 60))
    if fast >= slow:
        fast, slow = min(fast, slow - 1), slow
    f = close.rolling(fast).mean()
    s = close.rolling(slow).mean()
    pos = (f > s).astype(int)
    entries = (pos == 1) & (pos.shift(1).fillna(0) == 0)
    exits = (pos == 0) & (pos.shift(1).fillna(0) == 1)
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "rsi_mr",
        "name_prefix": "Alpha-RSI",
        "description": "Template RSI mean reversion",
        "params_space": {
            "rsi_period": [5, 7, 10, 14, 21],
            "oversold": [15, 20, 25, 30],
            "overbought": [70, 75, 80, 85],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    period = int(params.get("rsi_period", 14))
    lo = float(params.get("oversold", 30))
    hi = float(params.get("overbought", 70))
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    entries = (rsi < lo) & (rsi.shift(1) >= lo)
    exits = (rsi > hi) & (rsi.shift(1) <= hi)
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "momentum",
        "name_prefix": "Alpha-MOM",
        "description": "Template momentum breakout",
        "params_space": {
            "lookback": [5, 10, 20, 40, 60, 90],
            "thresh": [0.01, 0.02, 0.03, 0.05, 0.08, 0.10],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    lb = int(params.get("lookback", 20))
    th = float(params.get("thresh", 0.05))
    mom = close / close.shift(lb) - 1.0
    entries = (mom > th) & (mom.shift(1) <= th)
    exits = (mom < 0) & (mom.shift(1) >= 0)
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "bollinger",
        "name_prefix": "Alpha-BB",
        "description": "Template Bollinger mean reversion",
        "params_space": {
            "bb_period": [10, 20, 30],
            "bb_std": [1.5, 2.0, 2.5],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    n = int(params.get("bb_period", 20))
    k = float(params.get("bb_std", 2.0))
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    lower = mid - k * std
    upper = mid + k * std
    entries = (close < lower) & (close.shift(1) >= lower.shift(1))
    exits = (close > mid) & (close.shift(1) <= mid.shift(1))
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "donchian",
        "name_prefix": "Alpha-DON",
        "description": "Template Donchian channel breakout",
        "params_space": {
            "channel": [10, 20, 40, 55],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    n = int(params.get("channel", 20))
    hh = close.rolling(n).max().shift(1)
    ll = close.rolling(n).min().shift(1)
    entries = (close > hh) & (close.shift(1) <= hh.shift(1))
    exits = (close < ll) & (close.shift(1) >= ll.shift(1))
    return entries.fillna(False), exits.fillna(False)
''',
    },
    # ---------- HYBRID templates ----------
    {
        "type": "hybrid_trend_mom",
        "name_prefix": "Hybrid-TrendMom",
        "description": "Hybrid: trend (MA) filter + momentum entry",
        "params_space": {
            "fast": [10, 15, 20],
            "slow": [50, 80, 100],
            "lookback": [10, 20, 40],
            "thresh": [0.02, 0.03, 0.05, 0.08],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 50))
    if fast >= slow:
        fast, slow = 10, 50
    lb = int(params.get("lookback", 20))
    th = float(params.get("thresh", 0.05))
    f = close.rolling(fast).mean()
    s = close.rolling(slow).mean()
    uptrend = f > s
    mom = close / close.shift(lb) - 1.0
    entries = uptrend & (mom > th) & (mom.shift(1) <= th)
    exits = (~uptrend) | ((mom < 0) & (mom.shift(1) >= 0))
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_squeeze_break",
        "name_prefix": "Hybrid-Squeeze",
        "description": "Hybrid: vol squeeze + N-day high breakout + SMA trend",
        "params_space": {
            "vol_window": [15, 20, 30],
            "squeeze_threshold": [0.015, 0.02, 0.03, 0.04],
            "breakout_window": [10, 15, 20, 30],
            "trend_ma": [20, 50, 100],
            "exit_window": [10, 15, 20],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    vol_window = int(params.get("vol_window", 20))
    squeeze_threshold = float(params.get("squeeze_threshold", 0.03))
    breakout_window = int(params.get("breakout_window", 15))
    trend_ma = int(params.get("trend_ma", 50))
    exit_window = int(params.get("exit_window", 10))
    rolling_std = close.rolling(vol_window).std()
    normalized_vol = rolling_std / close.replace(0, 1e-9)
    is_squeeze = normalized_vol < squeeze_threshold
    prev_high = close.shift(1).rolling(breakout_window).max()
    breakout = close > prev_high
    sma = close.rolling(trend_ma).mean()
    entries = is_squeeze & breakout & (close > sma)
    exit_ma = close.rolling(exit_window).mean()
    exits = close < exit_ma
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_atr_break",
        "name_prefix": "Hybrid-ATRBreak",
        "description": "Hybrid: high breakout only when vol expanding + trend filter",
        "params_space": {
            "breakout_window": [10, 15, 20, 30],
            "vol_window": [10, 20, 30],
            "trend_ma": [20, 50, 100],
            "exit_window": [10, 15, 20],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    bw = int(params.get("breakout_window", 20))
    vw = int(params.get("vol_window", 20))
    trend_ma = int(params.get("trend_ma", 50))
    ew = int(params.get("exit_window", 10))
    prev_high = close.shift(1).rolling(bw).max()
    vol = close.rolling(vw).std()
    vol_avg = vol.rolling(vw).mean()
    sma = close.rolling(trend_ma).mean()
    entries = (close > prev_high) & (vol > vol_avg) & (close > sma)
    exits = close < close.rolling(ew).mean()
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_rsi_trend",
        "name_prefix": "Hybrid-RSITrend",
        "description": "Hybrid: RSI mean-reversion only in uptrend (MA filter)",
        "params_space": {
            "rsi_period": [5, 7, 10, 14],
            "oversold": [20, 25, 30],
            "overbought": [65, 70, 75],
            "trend_ma": [50, 100, 150],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    period = int(params.get("rsi_period", 14))
    lo = float(params.get("oversold", 30))
    hi = float(params.get("overbought", 70))
    trend_ma = int(params.get("trend_ma", 50))
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    uptrend = close > close.rolling(trend_ma).mean()
    entries = uptrend & (rsi < lo) & (rsi.shift(1) >= lo)
    exits = (rsi > hi) & (rsi.shift(1) <= hi)
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_dual_mom",
        "name_prefix": "Hybrid-DualMom",
        "description": "Hybrid: short+long momentum both positive, exit on short fade",
        "params_space": {
            "short_lb": [5, 10, 15],
            "long_lb": [40, 60, 90],
            "short_th": [0.01, 0.02, 0.03],
            "long_th": [0.03, 0.05, 0.08],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    short_lb = int(params.get("short_lb", 10))
    long_lb = int(params.get("long_lb", 60))
    if short_lb >= long_lb:
        short_lb, long_lb = 10, 60
    short_th = float(params.get("short_th", 0.02))
    long_th = float(params.get("long_th", 0.05))
    sm = close / close.shift(short_lb) - 1.0
    lm = close / close.shift(long_lb) - 1.0
    entries = (sm > short_th) & (lm > long_th) & ((sm.shift(1) <= short_th) | (lm.shift(1) <= long_th))
    exits = (sm < 0) & (sm.shift(1) >= 0)
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_bb_trend",
        "name_prefix": "Hybrid-BBTrend",
        "description": "Hybrid: Bollinger bounce only above long MA (trend-aligned MR)",
        "params_space": {
            "bb_period": [10, 20, 30],
            "bb_std": [1.5, 2.0, 2.5],
            "trend_ma": [50, 100, 150],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    n = int(params.get("bb_period", 20))
    k = float(params.get("bb_std", 2.0))
    trend_ma = int(params.get("trend_ma", 100))
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    lower = mid - k * std
    uptrend = close > close.rolling(trend_ma).mean()
    entries = uptrend & (close < lower) & (close.shift(1) >= lower.shift(1))
    exits = (close > mid) & (close.shift(1) <= mid.shift(1))
    return entries.fillna(False), exits.fillna(False)
''',
    },
    {
        "type": "hybrid_surge_trend",
        "name_prefix": "Hybrid-Surge",
        "description": "Hybrid: daily surge band + SMA50 (morning-surge style)",
        "params_space": {
            "buy_surge": [0.015, 0.02, 0.03],
            "buy_cap": [0.06, 0.08, 0.10, 0.12],
            "trend_ma": [20, 50, 100],
            "max_hold_days": [3, 5, 8, 10],
            "symbols": SYMBOLS_CORE,
        },
        "code": '''import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    buy_surge = float(params.get("buy_surge", 0.02))
    buy_cap = float(params.get("buy_cap", 0.10))
    trend_ma = int(params.get("trend_ma", 50))
    max_hold_days = int(params.get("max_hold_days", 5))
    day_ret = close / close.shift(1) - 1.0
    sma = close.rolling(trend_ma).mean()
    entries = pd.Series(False, index=close.index)
    exits = pd.Series(False, index=close.index)
    in_pos = False
    hold = 0
    for i in range(len(close)):
        px = float(close.iloc[i])
        if px <= 0 or pd.isna(px):
            continue
        if in_pos:
            hold += 1
            if hold >= max_hold_days or px < float(sma.iloc[i]) if not pd.isna(sma.iloc[i]) else False:
                exits.iloc[i] = True
                in_pos = False
                hold = 0
            continue
        dret = day_ret.iloc[i]
        st = sma.iloc[i]
        if pd.isna(dret) or pd.isna(st):
            continue
        if buy_surge <= float(dret) < buy_cap and px >= float(st):
            entries.iloc[i] = True
            in_pos = True
            hold = 0
    return entries.fillna(False), exits.fillna(False)
''',
    },
]


def sample_candidates(n: int = 4, rng: random.Random | None = None) -> list[dict]:
    """Sample candidates. Prefer hybrids (~60%) for intensive hybrid mining."""
    rng = rng or random.Random()
    hybrids = [t for t in TEMPLATES if str(t.get("type", "")).startswith("hybrid_")]
    singles = [t for t in TEMPLATES if not str(t.get("type", "")).startswith("hybrid_")]
    out: list[dict] = []
    for _ in range(n):
        pool = hybrids if (hybrids and rng.random() < 0.65) else (singles or TEMPLATES)
        tpl = rng.choice(pool or TEMPLATES)
        params: dict[str, Any] = {}
        for k, choices in tpl["params_space"].items():
            params[k] = rng.choice(choices)
        if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
            params["fast"], params["slow"] = 10, 40
        if "short_lb" in params and "long_lb" in params and params["short_lb"] >= params["long_lb"]:
            params["short_lb"], params["long_lb"] = 10, 60
        symbols = params.pop("symbols")
        sid = uuid.uuid4().hex[:6]
        out.append(
            {
                "source": "A_template",
                "name": f"{tpl['name_prefix']}-{sid}",
                "type": tpl["type"],
                "description": tpl["description"] + f" params={params}",
                "params": params,
                "symbols": symbols,
                "code": tpl["code"],
            }
        )
    return out
