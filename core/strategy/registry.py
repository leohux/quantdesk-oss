"""Built-in strategy templates registry."""
from __future__ import annotations

from core.strategy.base import CodeStrategy, Strategy

# Built-in strategy code templates
TEMPLATE_MA = '''"""MA Crossover strategy."""
import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 60))
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    position = (fast_ma > slow_ma).astype(int)
    entries = (position == 1) & (position.shift(1).fillna(0) == 0)
    exits = (position == 0) & (position.shift(1).fillna(0) == 1)
    return entries.fillna(False), exits.fillna(False)
'''

TEMPLATE_RSI = '''"""RSI mean-reversion."""
import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    period = int(params.get("period", 14))
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    entries = (rsi < oversold) & (rsi.shift(1) >= oversold)
    exits = (rsi > overbought) & (rsi.shift(1) <= overbought)
    return entries.fillna(False), exits.fillna(False)
'''

TEMPLATE_MOMENTUM = '''"""Price momentum breakout."""
import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    lookback = int(params.get("lookback", 20))
    exit_ma = int(params.get("exit_ma", 50))
    roll_high = close.shift(1).rolling(lookback).max()
    ma = close.rolling(exit_ma).mean()
    entries = close > roll_high
    exits = close < ma
    return entries.fillna(False), exits.fillna(False)
'''

TEMPLATE_BOLLINGER = '''"""Bollinger band mean reversion."""
import pandas as pd

def generate_signals(close: pd.Series, params: dict):
    window = int(params.get("window", 20))
    num_std = float(params.get("num_std", 2))
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    lower = mid - num_std * std
    upper = mid + num_std * std
    entries = close < lower
    exits = close > mid
    return entries.fillna(False), exits.fillna(False)
'''

TEMPLATES: dict[str, str] = {
    "ma_cross": TEMPLATE_MA,
    "rsi": TEMPLATE_RSI,
    "momentum": TEMPLATE_MOMENTUM,
    "bollinger": TEMPLATE_BOLLINGER,
}


def get_template(name: str) -> str:
    """Get a strategy template by name."""
    if name not in TEMPLATES:
        raise KeyError(f"Template '{name}' not found. Available: {list(TEMPLATES)}")
    return TEMPLATES[name]


def create_strategy_from_template(name: str, params: dict | None = None) -> CodeStrategy:
    """Create a CodeStrategy from a built-in template."""
    code = get_template(name)
    return CodeStrategy(code, strategy_name=name)


def list_templates() -> dict[str, str]:
    """List all available templates."""
    return dict(TEMPLATES)
