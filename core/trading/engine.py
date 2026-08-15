"""Trading Engine - event-driven main loop.

Orchestrates: MarketData -> Strategy -> Risk -> Order -> Fill
Uses the same Strategy interface as backtesting.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd

from core.events import Event, EventBus, EventType, get_event_bus
from core.strategy.base import Strategy, CodeStrategy, SignalResult
from core.strategy.engine import run_signal_fn
from core.trading.logger import LogEventType, TradeLogger
from core.trading.market_data import Bar, HistoricalReplayStream, MarketDataStream
from core.trading.metrics import RealtimeMetrics
from core.trading.order_manager import Order, OrderManager, OrderSide, OrderStatus, OrderType
from core.trading.portfolio import PortfolioManager
from core.trading.risk_engine import RiskEngine, RiskLimits

logger = logging.getLogger(__name__)


class EngineState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class StrategyConfig:
    """Strategy configuration for the trading engine."""
    code: str
    params: dict[str, Any]
    symbols: list[str]
    sizing_pct: float = 10.0  # % of equity per position
    stop_loss_pct: float = 5.0


class TradingEngine:
    """Event-driven paper trading engine.

    Flow: MarketData -> Strategy Signal -> Risk Check -> Order -> Fill -> Portfolio Update
    """

    def __init__(
        self,
        init_cash: float = 100_000.0,
        risk_limits: RiskLimits | None = None,
        log_file: str | None = None,
    ) -> None:
        # Core components
        self._portfolio = PortfolioManager(init_cash)
        self._order_manager = OrderManager()
        self._risk_engine = RiskEngine(self._portfolio, risk_limits)
        self._trade_logger = TradeLogger(log_file)
        self._metrics = RealtimeMetrics(self._portfolio)
        self._event_bus = get_event_bus()

        # Market data
        self._data_stream: MarketDataStream | None = None
        self._latest_prices: dict[str, float] = {}

        # Strategy
        self._strategies: list[StrategyConfig] = []
        self._active_signals: dict[str, str] = {}  # symbol -> signal

        # State
        self._state = EngineState.IDLE
        self._bar_count = 0
        self._error: str = ""

        # Wire up event handlers
        self._setup_events()

    def _setup_events(self) -> None:
        self._event_bus.subscribe(EventType.SIGNAL, self._on_signal)
        self._event_bus.subscribe(EventType.ORDER, self._on_order)
        self._event_bus.subscribe(EventType.FILL, self._on_fill)

    # ── Public API ──────────────────────────────────────────

    def add_strategy(self, config: StrategyConfig) -> None:
        """Add a strategy to run."""
        self._strategies.append(config)
        logger.info("Added strategy for symbols: %s", config.symbols)

    def set_data_stream(self, stream: MarketDataStream) -> None:
        self._data_stream = stream

    def load_historical_data(
        self,
        provider: Any,
        symbols: list[str],
        start: str = "2020-01-01",
        end: str | None = None,
    ) -> None:
        """Load historical data for replay."""
        if not isinstance(self._data_stream, HistoricalReplayStream):
            self._data_stream = HistoricalReplayStream()

        for sym in symbols:
            self._data_stream.load_from_provider(sym, provider, start, end)

    def start(self) -> None:
        """Start the trading engine."""
        if self._state == EngineState.RUNNING:
            return

        self._state = EngineState.RUNNING
        self._trade_logger.log(LogEventType.ENGINE_START, strategies=len(self._strategies))
        logger.info("Trading engine started with %d strategies", len(self._strategies))

    def stop(self) -> None:
        """Stop the trading engine."""
        self._state = EngineState.STOPPED
        if self._data_stream:
            self._data_stream.stop()
        self._trade_logger.log(LogEventType.ENGINE_STOP, bars_processed=self._bar_count)
        logger.info("Trading engine stopped after %d bars", self._bar_count)

    def reset(self, init_cash: float | None = None) -> None:
        """Reset everything."""
        self.stop()
        self._portfolio.reset(init_cash)
        self._order_manager.reset()
        self._metrics.reset()
        self._trade_logger.reset()
        self._risk_engine = RiskEngine(self._portfolio, self._risk_engine.limits)
        self._bar_count = 0
        self._active_signals.clear()
        self._latest_prices.clear()
        self._state = EngineState.IDLE
        logger.info("Trading engine reset")

    def process_bar(self, bar: Bar) -> None:
        """Process a single market data bar."""
        if self._state != EngineState.RUNNING:
            return

        self._latest_prices[bar.symbol] = bar.close
        self._bar_count += 1

        # Update portfolio prices
        self._portfolio.update_prices({bar.symbol: bar.close})

        # Check pending limit orders
        self._check_limit_orders(bar)

        # Run strategies for this symbol
        for config in self._strategies:
            if bar.symbol in config.symbols:
                self._run_strategy_signal(config, bar.symbol, bar.close, bar.timestamp)

        # Snapshot equity periodically (every bar for now)
        self._portfolio.snapshot()
        self._risk_engine.update_peak()

    def run_replay(self, speed: float = 0.0) -> dict[str, Any]:
        """Run full historical replay. Returns final metrics."""
        if not isinstance(self._data_stream, HistoricalReplayStream):
            raise ValueError("No historical data loaded")

        self._data_stream.start()
        self.start()

        # Subscribe to all strategy symbols
        bar_handler = self.process_bar
        for config in self._strategies:
            for sym in config.symbols:
                self._data_stream.subscribe(sym, bar_handler)

        # Replay all bars
        count = self._data_stream.replay_all()

        # Take a final metrics snapshot
        self._metrics.take_snapshot()

        self.stop()

        return {
            "bars_processed": count,
            "metrics": self._metrics.calculate(),
            "portfolio": self._portfolio.to_account_dict(),
            "exposure": self._portfolio.get_exposure(),
        }

    # ── Event Handlers ──────────────────────────────────────

    def _on_signal(self, event: Event) -> None:
        """Handle strategy signal events."""
        pass  # Signals are handled directly in _run_strategy_signal

    def _on_order(self, event: Event) -> None:
        """Handle order events (logging)."""
        order_id = event.get("order_id", "")
        symbol = event.get("symbol", "")
        side = event.get("side", "")
        qty = event.get("qty", 0)
        self._trade_logger.log_order_submitted(order_id, symbol, side, qty)

    def _on_fill(self, event: Event) -> None:
        """Handle fill events (logging + metrics)."""
        order_id = event.get("order_id", "")
        symbol = event.get("symbol", "")
        side = event.get("side", "")
        qty = event.get("qty", 0)
        price = event.get("price", 0)
        self._trade_logger.log_order_filled(order_id, symbol, side, qty, price)

        # Record round-trip trade for metrics on sell (close)
        if side == "sell":
            pos = self._portfolio.get_position(symbol)
            # Position was already updated, get realized PnL from portfolio
            realized = self._portfolio.total_realized_pnl
            self._metrics.record_trade(
                symbol=symbol,
                pnl=realized,
                entry_price=price,  # approximate
                exit_price=price,
                qty=qty,
            )

    # ── Internal Logic ──────────────────────────────────────

    def _run_strategy_signal(
        self,
        config: StrategyConfig,
        symbol: str,
        current_price: float,
        timestamp: datetime,
    ) -> None:
        """Run strategy and act on signals."""
        # We need a price history to generate signals
        # Build from portfolio equity curve + current price
        # For now, use a simple window approach:
        # Collect recent prices from the data stream
        stream = self._data_stream
        if stream is None:
            return

        # Get historical data from the stream
        if isinstance(stream, HistoricalReplayStream):
            # Build close series from the replay data
            df = stream._data.get(symbol)
            if df is None:
                return
            idx = stream._current_idx.get(symbol, 0)
            if idx < 2:
                return
            close = df["Close"].iloc[:idx]
        else:
            return  # Live not implemented

        try:
            entries, exits = run_signal_fn(close, config.code, config.params)
        except Exception as exc:
            self._trade_logger.log_error(f"Strategy error for {symbol}: {exc}")
            return

        # Check latest signal
        if len(entries) == 0:
            return

        last_entry = bool(entries.iloc[-1])
        last_exit = bool(exits.iloc[-1])

        prev_signal = self._active_signals.get(symbol, "hold")

        if last_entry and prev_signal != "buy":
            # BUY signal
            self._active_signals[symbol] = "buy"
            self._trade_logger.log_signal(symbol, "buy", price=current_price)
            self._event_bus.publish(Event(EventType.SIGNAL, {
                "symbol": symbol, "signal": "buy", "price": current_price,
            }))

            # Calculate position size
            equity = self._portfolio.equity
            allocation = equity * (config.sizing_pct / 100.0)
            qty = int(allocation / current_price)
            if qty > 0:
                self._submit_order(symbol, OrderSide.BUY, qty, current_price)

        elif last_exit and prev_signal == "buy":
            # SELL signal - close entire position
            self._active_signals[symbol] = "sell"
            self._trade_logger.log_signal(symbol, "sell", price=current_price)
            self._event_bus.publish(Event(EventType.SIGNAL, {
                "symbol": symbol, "signal": "sell", "price": current_price,
            }))

            pos = self._portfolio.get_position(symbol)
            if pos and pos.qty > 0:
                self._submit_order(symbol, OrderSide.SELL, pos.qty, current_price)

    def _submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        price: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> None:
        """Create, risk-check, and submit an order."""
        order = self._order_manager.create_order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
        )

        # Risk check
        check = self._risk_engine.check_order(
            order, price, len(self._order_manager.get_active_orders())
        )

        if not check:
            self._order_manager.reject_order(order.id, check.reject_reason)
            self._trade_logger.log_risk_rejected(order.id, symbol, check.reject_reason)
            logger.warning("Risk rejected %s %s: %s", side.value, symbol, check.reject_reason)
            return

        # Publish order event
        self._event_bus.publish(Event(EventType.ORDER, {
            "order_id": order.id, "symbol": symbol,
            "side": side.value, "qty": qty, "price": price,
        }))

        # For market orders, fill immediately
        if order_type == OrderType.MARKET:
            self._fill_order(order, qty, price)
        # Limit orders wait for price match

    def _fill_order(self, order: Order, qty: float, price: float) -> None:
        """Fill an order and update portfolio."""
        commission = price * qty * 0.0001  # 1bps commission

        txn = self._order_manager.fill_order(order.id, qty, price, commission)

        # Update portfolio
        self._portfolio.open_position(
            order.symbol, qty, price, order.side, commission
        )

        # Publish fill event
        self._event_bus.publish(Event(EventType.FILL, {
            "order_id": order.id, "symbol": order.symbol,
            "side": order.side.value, "qty": qty, "price": price,
            "commission": commission,
        }))

    def _check_limit_orders(self, bar: Bar) -> None:
        """Check and fill pending limit orders against current bar."""
        for order in self._order_manager.get_active_orders():
            if order.order_type != OrderType.LIMIT:
                continue
            if order.symbol != bar.symbol:
                continue

            should_fill = False
            if order.side == OrderSide.BUY and bar.low <= (order.limit_price or 0):
                should_fill = True
            elif order.side == OrderSide.SELL and bar.high >= (order.limit_price or 0):
                should_fill = True

            if should_fill:
                fill_price = order.limit_price or bar.close
                self._fill_order(order, order.remaining_qty, fill_price)

    # ── State Getters ───────────────────────────────────────

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def portfolio(self) -> PortfolioManager:
        return self._portfolio

    @property
    def order_manager(self) -> OrderManager:
        return self._order_manager

    @property
    def risk_engine(self) -> RiskEngine:
        return self._risk_engine

    @property
    def trade_logger(self) -> TradeLogger:
        return self._trade_logger

    @property
    def metrics(self) -> RealtimeMetrics:
        return self._metrics

    def get_status(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "bars_processed": self._bar_count,
            "strategies": len(self._strategies),
            "symbols": list(set(s for c in self._strategies for s in c.symbols)),
            "portfolio": self._portfolio.to_account_dict(),
            "risk": self._risk_engine.get_status(),
            "active_orders": len(self._order_manager.get_active_orders()),
            "error": self._error,
        }
