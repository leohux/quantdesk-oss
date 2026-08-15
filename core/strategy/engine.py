"""Safe strategy code compiler and executor.

Compiles user-written strategy code into callable functions
with a sandboxed execution environment.
"""
from __future__ import annotations

import ast
from typing import Any, Callable

import pandas as pd

# Forbidden imports for security
_FORBIDDEN = {
    "os", "sys", "subprocess", "socket", "pathlib", "shutil",
    "builtins", "__import__", "eval", "exec", "open", "compile",
}


def validate_strategy_code(code: str) -> None:
    """Validate strategy code for safety (no dangerous imports/calls)."""
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
        isinstance(n, ast.FunctionDef) and n.name == "generate_signals"
        for n in tree.body
    )
    if not has_fn:
        raise ValueError("Strategy must define generate_signals(close, params)")


def compile_strategy(code: str) -> Callable[[pd.Series, dict], tuple[pd.Series, pd.Series]]:
    """Compile strategy code string into a callable function."""
    validate_strategy_code(code)

    def _safe_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root == "pandas":
            return pd
        raise ImportError(f"Import not allowed: {name}")

    safe_builtins = {
        "abs": abs, "min": min, "max": max, "round": round,
        "len": len, "range": range, "float": float, "int": int,
        "bool": bool, "str": str, "dict": dict, "list": list,
        "tuple": tuple, "enumerate": enumerate, "zip": zip,
        "True": True, "False": False, "None": None,
        "__import__": _safe_import,
    }
    namespace: dict[str, Any] = {"__builtins__": safe_builtins, "pd": pd}
    exec(compile(code, "<strategy>", "exec"), namespace)
    fn = namespace.get("generate_signals")
    if not callable(fn):
        raise ValueError("generate_signals not found")
    return fn


def run_signal_fn(
    close: pd.Series,
    code: str,
    params: dict[str, Any] | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Compile and run strategy code, returning (entries, exits)."""
    fn = compile_strategy(code)
    entries, exits = fn(close.astype(float), params or {})
    entries = pd.Series(entries, index=close.index).fillna(False).astype(bool)
    exits = pd.Series(exits, index=close.index).fillna(False).astype(bool)
    return entries, exits
