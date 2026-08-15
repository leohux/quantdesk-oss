"""Backtest engine - orchestrates strategy backtesting with vectorbt."""
from __future__ import annotations

from typing import Any

import pandas as pd

from core.backtest.metrics import (
    calculate_annual_returns,
    calculate_drawdown_curve,
    calculate_metrics,
    calculate_monthly_returns,
)
from core.strategy.engine import run_signal_fn


def run_backtest(
    ohlcv: pd.DataFrame,
    code: str,
    params: dict[str, Any] | None = None,
    init_cash: float = 100_000.0,
    fees: float = 0.0005,
    slippage: float = 0.0005,
) -> dict[str, Any]:
    """Run a backtest for a strategy on a single symbol.

    Tries vectorbt first, falls back to pandas.
    """
    close = ohlcv["Close"].astype(float)
    entries, exits = run_signal_fn(close, code, params)

    total_fees = fees + slippage

    try:
        return _vectorbt_backtest(close, entries, exits, init_cash, total_fees, params or {})
    except Exception:
        return _pandas_backtest(close, entries, exits, init_cash, total_fees, params or {})


def _vectorbt_backtest(
    close: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    init_cash: float,
    fees: float,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run backtest using vectorbt."""
    import vectorbt as vbt

    pf = vbt.Portfolio.from_signals(
        close,
        entries=entries,
        exits=exits,
        init_cash=init_cash,
        fees=fees,
        freq="1D",
    )
    stats = pf.stats()
    equity = pf.value()

    equity_series = pd.Series(equity.values, index=equity.index)
    total_return_pct = float(stats.get("Total Return [%]", 0.0))
    trades = int(stats.get("Total Trades", 0) or 0)

    # Count wins from trades
    trade_returns = pf.trades.records_readable if hasattr(pf.trades, 'records_readable') else None
    wins = 0
    if trade_returns is not None and len(trade_returns) > 0:
        wins = int((trade_returns.get("Return", pd.Series()) > 0).sum()) if "Return" in trade_returns.columns else 0

    metrics = calculate_metrics(equity_series, trades, wins, total_return_pct, init_cash)

    return {
        "engine": "vectorbt",
        "params": params,
        "init_cash": init_cash,
        "buy_hold_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
        "equity_curve": [
            {"date": d.strftime("%Y-%m-%d"), "equity": float(v)}
            for d, v in equity.items()
        ][-365:],
        "drawdown_curve": calculate_drawdown_curve(equity_series),
        "annual_returns": calculate_annual_returns(equity_series),
        "monthly_returns": calculate_monthly_returns(equity_series),
        **metrics,
    }


def _pandas_backtest(
    close: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    init_cash: float,
    fees: float,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run backtest using pure pandas (fallback)."""
    cash = init_cash
    shares = 0.0
    equity_curve = []
    trades = 0
    wins = 0
    entry_price = None

    for dt in close.index:
        price = float(close.loc[dt])
        if bool(entries.loc[dt]) and shares == 0:
            shares = (cash * (1 - fees)) / price
            cash = 0.0
            entry_price = price
            trades += 1
        elif bool(exits.loc[dt]) and shares > 0:
            cash = shares * price * (1 - fees)
            if entry_price and price > entry_price:
                wins += 1
            shares = 0.0
            entry_price = None
        equity = cash + shares * price
        equity_curve.append({"date": pd.Timestamp(dt).strftime("%Y-%m-%d"), "equity": equity})

    equity_series = pd.Series(
        {pd.Timestamp(p["date"]): p["equity"] for p in equity_curve}
    )
    total_return_pct = (equity_curve[-1]["equity"] / init_cash - 1) * 100 if equity_curve else 0

    metrics = calculate_metrics(equity_series, trades, wins, total_return_pct, init_cash)

    return {
        "engine": "pandas",
        "params": params,
        "init_cash": init_cash,
        "buy_hold_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
        "equity_curve": equity_curve[-365:],
        "drawdown_curve": calculate_drawdown_curve(equity_series),
        "annual_returns": calculate_annual_returns(equity_series),
        "monthly_returns": calculate_monthly_returns(equity_series),
        **metrics,
    }


def run_portfolio_backtest(
    symbols: list[str],
    ohlcv_map: dict[str, pd.DataFrame],
    code: str,
    params: dict[str, Any] | None = None,
    init_cash: float = 100_000.0,
    fees: float = 0.0005,
    slippage: float = 0.0005,
) -> dict[str, Any]:
    """Run backtest across multiple symbols (portfolio)."""
    cash_each = init_cash / max(len(symbols), 1)
    per_symbol: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for sym in symbols:
        ohlcv = ohlcv_map.get(sym)
        if ohlcv is None:
            errors.append({"symbol": sym, "error": "No data"})
            continue
        try:
            result = run_backtest(ohlcv, code, params, cash_each, fees, slippage)
            result["symbol"] = sym
            result["bars"] = len(ohlcv)
            result["start"] = str(ohlcv.index[0].date())
            result["end"] = str(ohlcv.index[-1].date())
            per_symbol.append(result)
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)})

    if not per_symbol:
        return {"errors": errors}

    # Merge equity curves
    date_map: dict[str, float] = {}
    for item in per_symbol:
        for pt in item.get("equity_curve", []):
            date_map[pt["date"]] = date_map.get(pt["date"], 0.0) + float(pt["equity"])
    equity_curve = [{"date": d, "equity": v} for d, v in sorted(date_map.items())]

    equity_series = pd.Series(
        {pd.Timestamp(p["date"]): p["equity"] for p in equity_curve}
    )
    total_return_pct = (equity_curve[-1]["equity"] / init_cash - 1) * 100
    sharpes = [x["sharpe"] for x in per_symbol if x.get("sharpe") is not None]
    max_dds = [x["max_drawdown_pct"] for x in per_symbol]
    total_trades = sum(int(x.get("trades") or 0) for x in per_symbol)
    total_wins = sum(int(x.get("max_consecutive_wins") or 0) for x in per_symbol)

    metrics = calculate_metrics(equity_series, total_trades, total_wins, total_return_pct, init_cash)

    return {
        "engine": per_symbol[0].get("engine"),
        "symbols": symbols,
        "params": params,
        "init_cash": init_cash,
        "buy_hold_return_pct": per_symbol[0].get("buy_hold_return_pct"),
        "per_symbol": [
            {
                "symbol": x["symbol"],
                "total_return_pct": x.get("total_return_pct"),
                "sharpe": x.get("sharpe"),
                "max_drawdown_pct": x.get("max_drawdown_pct"),
                "trades": x.get("trades"),
            }
            for x in per_symbol
        ],
        "equity_curve": equity_curve[-365:],
        "drawdown_curve": calculate_drawdown_curve(equity_series),
        "annual_returns": calculate_annual_returns(equity_series),
        "monthly_returns": calculate_monthly_returns(equity_series),
        "errors": errors,
        **metrics,
    }
