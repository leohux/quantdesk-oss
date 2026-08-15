# QuantDesk — System Architecture

QuantDesk is a personal US equities quantitative trading platform with an event-driven core, pluggable execution engines, and a unified strategy interface shared by backtesting, paper trading, and (future) live trading.

## Live Trading Status

Live trading is now implemented as a **fail-closed IBKR path**:

- broker adapter: `core/execution/ibkr.py`
- live risk / readiness guard: `core/trading/live_guard.py`
- live API: `/api/live/*`
- UI console: `web/src/pages/LivePage.tsx`

The architecture deliberately separates:

1. **Paper / Shadow validation**
2. **Read-only live readiness**
3. **Armed live execution**

Only the third state may ever submit to a real broker, and it remains locked by default.

## Design Principles

| Principle | Description |
|---|---|
| **Decoupled** | Modules communicate through the `EventBus` — no direct imports between strategy and execution. |
| **Event-Driven** | All trading flows are expressed as events: `MarketData → Signal → Risk → Order → Fill`. |
| **Pluggable Execution** | The `ExecutionEngine` ABC allows swapping between `PaperExecutionEngine`, `AlpacaPaperClient`, `IBKR`, etc. without changing strategy code. |
| **Shared Strategy Interface** | The same `generate_signals(close, params) → (entries, exits)` function runs in backtests and live/paper engines. |
| **Pre-Trade Risk** | Every order passes through `RiskEngine` before reaching any broker. |

## Directory Structure

```
/app/
├── api/                        # FastAPI application
│   ├── main.py                 # App entry, auth middleware, REST endpoints
│   └── paper_routes.py         # /api/paper/* endpoints
├── backtest/                   # Backtest runner (vectorbt / pandas fallback)
│   └── runner.py
├── config/                     # Settings & strategy store (YAML/JSON)
│   ├── settings.py             # Pydantic env-based settings
│   └── store.py                # CRUD for strategies & app settings
├── core/                       # Core engine library
│   ├── events.py               # EventBus (pub/sub), Event, EventType
│   ├── backtest/
│   │   ├── engine.py           # run_backtest, vectorbt & pandas engines
│   │   └── metrics.py          # Sharpe, drawdown, monthly returns
│   ├── config/
│   │   ├── loader.py           # YAML config loader
│   │   ├── schema.py           # Config validation schemas
│   │   └── calendar.py         # US market calendar helpers
│   ├── data/
│   │   ├── base.py             # DataProvider ABC
│   │   ├── alpaca.py           # Alpaca data provider
│   │   ├── yfinance.py         # Yahoo Finance provider
│   │   └── manager.py          # Provider selection & caching
│   ├── execution/
│   │   ├── base.py             # ExecutionEngine ABC, Order, Position, Account
│   │   ├── paper.py            # PaperExecutionEngine (in-memory)
│   │   ├── alpaca.py           # Alpaca paper/live client
│   │   └── ibkr.py             # Interactive Brokers stub
│   ├── infra/
│   │   ├── structured_logging.py  # JSON logging, AuditLogger
│   │   └── resilience.py       # Retry / circuit-breaker helpers
│   ├── monitoring/
│   │   └── __init__.py
│   ├── notification/
│   │   ├── base.py             # Notifier ABC
│   │   ├── telegram.py         # Telegram notifier
│   │   └── manager.py          # Queue-based NotificationManager
│   ├── risk/
│   │   ├── position.py         # Position-level risk
│   │   ├── portfolio.py        # Portfolio-level risk
│   │   └── stoploss.py         # Stop loss strategies (fixed, trailing, ATR)
│   ├── strategy/
│   │   ├── base.py             # Strategy ABC, CodeStrategy, SignalResult
│   │   ├── engine.py           # compile_strategy, run_signal_fn
│   │   └── registry.py         # Strategy registration
│   └── trading/
│       ├── engine.py           # TradingEngine (event-driven main loop)
│       ├── market_data.py      # MarketDataStream, HistoricalReplayStream
│       ├── risk_engine.py      # RiskEngine, RiskLimits
│       ├── order_manager.py    # OrderManager
│       ├── portfolio.py        # PortfolioManager
│       ├── metrics.py          # RealtimeMetrics
│       ├── logger.py           # TradeLogger (structured event log)
│       └── data_adapter.py     # Data adapter for trading
├── data/
│   ├── loader.py               # OHLCV data loading
│   └── store/                  # Persisted data (strategies, settings)
├── strategies/                 # Built-in strategy templates
│   ├── engine.py               # TEMPLATES dict, validate_strategy_code
│   └── ma_cross.py             # MA crossover strategy
├── tests/
│   └── test_paper_trading.py
└── web/                        # Frontend (Vite + React)
    └── dist/                   # Built frontend assets
```

## Core Modules

### `core/events.py` — EventBus
Synchronous pub/sub bus. Modules subscribe to `EventType` enums and handle `Event` dataclasses. The global singleton is accessed via `get_event_bus()`.

### `core/strategy/` — Strategy Engine
- `Strategy` ABC with `generate_signals(close, params) → SignalResult`
- `CodeStrategy` compiles user-provided Python code into a callable
- `compile_strategy()` / `run_signal_fn()` handle code execution in a sandboxed namespace

### `core/data/` — Data Providers
- `DataProvider` ABC with `get_bars(symbol, start, end) → DataFrame`
- Implementations: `yfinance`, `alpaca`
- `normalize_ohlcv()` ensures consistent `Open/High/Low/Close/Volume` columns

### `core/execution/` — Execution Engines
- `ExecutionEngine` ABC: `submit_order`, `get_positions`, `get_account`, `close_position`
- `PaperExecutionEngine`: in-memory, immediate fills, no broker
- `AlpacaPaperClient` / `IBKR`: real broker integrations

### `core/risk/` — Risk Management
- `RiskEngine`: pre-trade checks (position concentration, daily loss, drawdown, cash reserve, exposure)
- `RiskLimits`: configurable limits dataclass
- `StopLossManager`: fixed%, trailing%, ATR-based stop losses

### `core/trading/` — Trading Engine
- `TradingEngine`: orchestrates the full event loop
- `PortfolioManager`: multi-position cash & equity tracking
- `OrderManager`: order lifecycle (create → fill/cancel/reject)
- `RealtimeMetrics`: Sharpe, CAGR, drawdown, win rate, profit factor
- `TradeLogger`: structured JSON event logging

### `core/notification/` — Notifications
- `Notifier` ABC with `send()`, `send_trade_alert()`, `send_risk_alert()`
- `NotificationManager`: queue-based async dispatch to multiple backends
- `TelegramNotifier`: Telegram bot integration

### `core/infra/` — Infrastructure
- `JsonFormatter`: structured JSON log lines
- `AuditLogger`: dedicated trade audit log file
- `ContextFilter`: thread-local context injection

### `config/` — Configuration
- `Settings`: Pydantic `BaseSettings` from `.env`
- `store.py`: YAML-based CRUD for strategies and app settings

## Module Dependency Graph

```
                        ┌──────────────┐
                        │   api/       │  FastAPI REST + WebSocket
                        │  main.py     │
                        │  paper_routes│
                        └──────┬───────┘
                               │ uses
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌──────────┐   ┌──────────────┐  ┌───────────┐
        │ backtest/ │   │ core/trading/ │  │ config/   │
        │ runner    │   │ engine       │  │ settings  │
        └────┬─────┘   └──────┬───────┘  │ store     │
             │                │           └───────────┘
             ▼                ▼
      ┌──────────────────────────────────────────┐
      │              core/strategy/               │
      │  base.py (Strategy ABC)                   │
      │  engine.py (compile, run_signal_fn)       │
      └──────────────────────┬───────────────────┘
                             │ uses
      ┌──────────────────────┼──────────────────────┐
      ▼                      ▼                      ▼
┌──────────┐        ┌──────────────┐        ┌──────────┐
│ core/data │        │ core/events  │        │ core/risk│
│ providers │        │  EventBus   │        │ stoploss │
└──────────┘        └──────┬───────┘        │ position │
                           │ pub/sub        └──────────┘
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     ┌────────────┐ ┌──────────────┐ ┌──────────────┐
     │core/execut.│ │core/trading/ │ │core/notific. │
     │ paper      │ │ risk_engine  │ │ manager      │
     │ alpaca     │ │ order_mgr   │ │ telegram     │
     │ ibkr       │ │ portfolio   │ └──────────────┘
     └────────────┘ │ metrics     │
                    │ logger      │
                    └─────────────┘
```

## Key Data Flow

```
MarketData ──► Strategy ──► Signal ──► RiskEngine ──► Order ──► Fill ──► Portfolio
   Bar           generate_    entry/     check_order    submit    fill_    open_
   objects       signals()    exit       pass/reject    order     order    position
```

The `TradingEngine.process_bar()` method drives this entire pipeline for each incoming market data bar. The same `generate_signals()` function is used by both the backtest engine and the live trading engine.
