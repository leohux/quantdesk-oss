"""Backtest metrics calculation."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def calculate_metrics(
    equity_curve: pd.Series,
    trades: int,
    wins: int,
    total_return_pct: float,
    init_cash: float,
) -> dict[str, Any]:
    """Calculate comprehensive backtest metrics from equity curve."""
    if equity_curve.empty:
        return _empty_metrics()

    returns = equity_curve.pct_change().dropna()
    trading_days = 252

    # Basic
    end_value = float(equity_curve.iloc[-1])
    n_years = len(equity_curve) / trading_days

    # CAGR
    if n_years > 0 and init_cash > 0:
        cagr = (end_value / init_cash) ** (1 / n_years) - 1
    else:
        cagr = 0.0

    # Volatility
    volatility = float(returns.std() * math.sqrt(trading_days)) if len(returns) > 1 else 0.0

    # Sharpe
    if len(returns) > 1 and returns.std() > 0:
        sharpe = float((returns.mean() / returns.std()) * math.sqrt(trading_days))
    else:
        sharpe = None

    # Sortino
    downside = returns[returns < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = float((returns.mean() / downside.std()) * math.sqrt(trading_days))
    else:
        sortino = None

    # Max Drawdown
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    max_drawdown_pct = float(abs(drawdown.min()) * 100)

    # Calmar
    calmar = float(cagr / abs(drawdown.min())) if max_drawdown_pct > 0 else None

    # Win rate & Profit Factor
    win_rate_pct = (wins / trades * 100) if trades > 0 else None

    # Expectancy (per trade)
    win_returns = returns[returns > 0]
    loss_returns = returns[returns < 0]
    avg_win = float(win_returns.mean()) if len(win_returns) > 0 else 0.0
    avg_loss = float(loss_returns.mean()) if len(loss_returns) > 0 else 0.0
    expectancy = (
        (win_rate_pct / 100 * avg_win) + ((1 - win_rate_pct / 100) * avg_loss)
        if win_rate_pct is not None
        else None
    )

    # Profit Factor
    gross_profit = float(win_returns.sum()) if len(win_returns) > 0 else 0.0
    gross_loss = float(abs(loss_returns.sum())) if len(loss_returns) > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # SQN (System Quality Number)
    if trades > 0 and len(returns) > 1:
        sqn = float(math.sqrt(trades) * returns.mean() / returns.std())
    else:
        sqn = None

    # Omega Ratio (threshold = 0)
    if len(returns) > 0:
        gains = returns[returns > 0].sum()
        losses = abs(returns[returns < 0].sum())
        omega = (gains / losses) if losses > 0 else None
    else:
        omega = None

    # Recovery Factor
    total_gain = end_value - init_cash
    recovery_factor = (total_gain / (max_drawdown_pct / 100 * init_cash)) if max_drawdown_pct > 0 else None

    # Ulcer Index
    dd_squared = drawdown ** 2
    ulcer_index = float(math.sqrt(dd_squared.mean()) * 100) if len(dd_squared) > 0 else 0.0

    # Max consecutive wins/losses
    streaks = _calculate_streaks(returns)
    max_consecutive_wins = streaks["max_wins"]
    max_consecutive_losses = streaks["max_losses"]

    return {
        "total_return_pct": total_return_pct,
        "cagr_pct": cagr * 100,
        "volatility_pct": volatility * 100,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "sqn": sqn,
        "omega_ratio": omega,
        "recovery_factor": recovery_factor,
        "ulcer_index": ulcer_index,
        "trades": trades,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "end_value": end_value,
    }


def calculate_drawdown_curve(equity: pd.Series) -> list[dict[str, Any]]:
    """Calculate drawdown curve for charting."""
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return [
        {"date": d.strftime("%Y-%m-%d"), "drawdown_pct": float(v * 100)}
        for d, v in dd.items()
    ][-365:]


def calculate_annual_returns(equity: pd.Series) -> list[dict[str, Any]]:
    """Calculate annual returns."""
    yearly = equity.resample("YE").last().pct_change().dropna()
    return [
        {"year": int(d.year), "return_pct": float(v * 100)}
        for d, v in yearly.items()
    ]


def calculate_monthly_returns(equity: pd.Series) -> list[dict[str, Any]]:
    """Calculate monthly returns."""
    monthly = equity.resample("ME").last().pct_change().dropna()
    return [
        {"year": int(d.year), "month": int(d.month), "return_pct": float(v * 100)}
        for d, v in monthly.items()
    ][-36:]


def _calculate_streaks(returns: pd.Series) -> dict[str, int]:
    """Calculate max consecutive wins and losses."""
    max_wins = max_losses = current_wins = current_losses = 0
    for r in returns:
        if r > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif r < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            current_wins = current_losses = 0
    return {"max_wins": max_wins, "max_losses": max_losses}


def _empty_metrics() -> dict[str, Any]:
    return {
        "total_return_pct": 0, "cagr_pct": 0, "volatility_pct": 0,
        "max_drawdown_pct": 0, "sharpe": None, "sortino": None,
        "calmar": None, "win_rate_pct": None, "profit_factor": None,
        "expectancy": None, "sqn": None, "omega_ratio": None,
        "recovery_factor": None, "ulcer_index": 0, "trades": 0,
        "max_consecutive_wins": 0, "max_consecutive_losses": 0,
        "avg_win": 0, "avg_loss": 0, "end_value": 0,
    }
