from __future__ import annotations

import ast
from typing import Any, Callable

import pandas as pd

# Only allow these names when executing user strategy code
_FORBIDDEN = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "builtins",
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
}


TEMPLATE_MA = '''"""MA Crossover strategy.
Must define: generate_signals(close, params) -> (entries, exits)
entries/exits are boolean Series aligned with close.
"""
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

TEMPLATE_RSI = '''"""RSI mean-reversion.
Buy when RSI < oversold, sell when RSI > overbought.
"""
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

TEMPLATE_MOMENTUM = '''"""Price momentum breakout.
Enter when close > rolling high lookback, exit on MA cross down.
"""
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

TEMPLATE_BOLLINGER = '''"""Bollinger band mean reversion.
Buy below lower band, sell above middle/upper.
"""
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

TEMPLATES = {
    "ma_cross": TEMPLATE_MA,
    "rsi": TEMPLATE_RSI,
    "momentum": TEMPLATE_MOMENTUM,
    "bollinger": TEMPLATE_BOLLINGER,
}


def validate_strategy_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN:
                    raise ValueError(f"Import not allowed: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _FORBIDDEN:
                raise ValueError(f"Import not allowed: {node.module}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"eval", "exec", "open", "__import__", "compile"}:
                raise ValueError(f"Call not allowed: {node.func.id}")

    has_fn = any(
        isinstance(n, ast.FunctionDef) and n.name == "generate_signals" for n in tree.body
    )
    if not has_fn:
        raise ValueError("Strategy must define generate_signals(close, params)")


def compile_strategy(code: str) -> Callable[[pd.Series, dict], tuple[pd.Series, pd.Series]]:
    validate_strategy_code(code)

    def _safe_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root == "pandas":
            return pd
        raise ImportError(f"Import not allowed: {name}")

    safe_builtins = {
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "len": len,
        "range": range,
        "float": float,
        "int": int,
        "bool": bool,
        "str": str,
        "dict": dict,
        "list": list,
        "tuple": tuple,
        "enumerate": enumerate,
        "zip": zip,
        "True": True,
        "False": False,
        "None": None,
        "__import__": _safe_import,
    }
    namespace: dict[str, Any] = {"__builtins__": safe_builtins, "pd": pd}
    exec(compile(code, "<strategy>", "exec"), namespace)  # noqa: S102
    fn = namespace.get("generate_signals")
    if not callable(fn):
        raise ValueError("generate_signals not found")
    return fn


def run_signal_fn(
    close: pd.Series,
    code: str,
    params: dict[str, Any] | None = None,
) -> tuple[pd.Series, pd.Series]:
    fn = compile_strategy(code)
    entries, exits = fn(close.astype(float), params or {})
    entries = pd.Series(entries, index=close.index).fillna(False).astype(bool)
    exits = pd.Series(exits, index=close.index).fillna(False).astype(bool)
    return entries, exits
