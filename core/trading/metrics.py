"""Real-time trading metrics calculator."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from core.trading.portfolio import PortfolioManager


@dataclass
class DailySnapshot:
    date: str
    equity: float
    daily_return: float = 0.0
    cash: float = 0.0
    invested: float = 0.0


class RealtimeMetrics:
    """Calculate real-time trading performance metrics."""

    def __init__(self, portfolio: PortfolioManager) -> None:
        self._portfolio = portfolio
        self._daily_snapshots: list[DailySnapshot] = []
        self._last_snapshot_equity: float = portfolio.equity
        self._trades: list[dict[str, Any]] = []  # completed round-trips
        self._peak_equity: float = portfolio.equity

    def record_trade(self, symbol: str, pnl: float, entry_price: float, exit_price: float, qty: float) -> None:
        """Record a completed round-trip trade."""
        self._trades.append({
            "symbol": symbol,
            "pnl": pnl,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty": qty,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def take_snapshot(self, date: str | None = None) -> DailySnapshot:
        """Take a daily equity snapshot."""
        equity = self._portfolio.equity
        daily_return = ((equity / self._last_snapshot_equity) - 1) if self._last_snapshot_equity > 0 else 0

        snap = DailySnapshot(
            date=date or datetime.utcnow().strftime("%Y-%m-%d"),
            equity=equity,
            daily_return=daily_return,
            cash=self._portfolio.cash,
            invested=self._portfolio.invested,
        )
        self._daily_snapshots.append(snap)
        self._last_snapshot_equity = equity

        if equity > self._peak_equity:
            self._peak_equity = equity

        return snap

    def calculate(self) -> dict[str, Any]:
        """Calculate all real-time metrics."""
        if not self._daily_snapshots:
            return self._empty_metrics()

        returns = pd.Series([s.daily_return for s in self._daily_snapshots])
        equity_series = pd.Series([s.equity for s in self._daily_snapshots])
        current_equity = self._portfolio.equity
        n_days = len(self._daily_snapshots)
        n_years = n_days / 252.0

        # Total return
        init_equity = self._daily_snapshots[0].equity
        total_return_pct = ((current_equity / init_equity) - 1) * 100 if init_equity > 0 else 0

        # CAGR
        cagr = ((current_equity / init_equity) ** (1 / n_years) - 1) if n_years > 0 and init_equity > 0 else 0

        # Sharpe
        if len(returns) > 1 and returns.std() > 0:
            sharpe = float((returns.mean() / returns.std()) * math.sqrt(252))
        else:
            sharpe = None

        # Max drawdown
        peak = equity_series.cummax()
        dd = (equity_series - peak) / peak
        max_drawdown_pct = float(abs(dd.min()) * 100) if len(dd) > 0 else 0

        # Win rate
        wins = [t for t in self._trades if t["pnl"] > 0]
        losses = [t for t in self._trades if t["pnl"] <= 0]
        total_trades = len(self._trades)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else None

        # Profit factor
        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        return {
            "equity": round(current_equity, 2),
            "total_return_pct": round(total_return_pct, 2),
            "cagr_pct": round(cagr * 100, 2),
            "sharpe": round(sharpe, 3) if sharpe else None,
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "daily_returns": [s.daily_return for s in self._daily_snapshots[-30:]],
            "equity_curve": [
                {"date": s.date, "equity": round(s.equity, 2)}
                for s in self._daily_snapshots[-365:]
            ],
            "peak_equity": round(self._peak_equity, 2),
            "n_trading_days": n_days,
        }

    def _empty_metrics(self) -> dict[str, Any]:
        return {
            "equity": round(self._portfolio.equity, 2),
            "total_return_pct": 0, "cagr_pct": 0, "sharpe": None,
            "max_drawdown_pct": 0, "win_rate_pct": None, "profit_factor": None,
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "gross_profit": 0, "gross_loss": 0, "daily_returns": [],
            "equity_curve": [], "peak_equity": round(self._portfolio.equity, 2),
            "n_trading_days": 0,
        }

    def reset(self) -> None:
        self._daily_snapshots.clear()
        self._trades.clear()
        self._last_snapshot_equity = self._portfolio.equity
        self._peak_equity = self._portfolio.equity
