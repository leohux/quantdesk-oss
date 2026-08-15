# QuantDesk — Paper Trading Guide

Paper trading runs the full event-driven trading pipeline with simulated execution — no real money, no broker risk. It uses the same strategy code, risk engine, and order management as live trading.

## Architecture

Paper trading is built on the event-driven `TradingEngine`:

```
HistoricalReplayStream → TradingEngine → [EventBus] → Portfolio
         │                    │
         │  Bar-by-bar        │  MarketData → Strategy → Signal
         │  replay            │  → RiskEngine → Order → Fill
         │                    │  → PortfolioManager → Metrics
         │                    │
    ┌────┴────┐          ┌────┴────────────┐
    │ Data    │          │ Components:      │
    │ Loader  │          │ • RiskEngine     │
    │ (OHLCV) │          │ • OrderManager   │
    └─────────┘          │ • PortfolioManager│
                         │ • TradeLogger    │
                         │ • RealtimeMetrics│
                         └─────────────────┘
```

### Key Components

| Component | Class | Location |
|---|---|---|
| Trading Engine | `TradingEngine` | `/app/core/trading/engine.py` |
| Market Data | `HistoricalReplayStream` | `/app/core/trading/market_data.py` |
| Risk Engine | `RiskEngine` | `/app/core/trading/risk_engine.py` |
| Order Manager | `OrderManager` | `/app/core/trading/order_manager.py` |
| Portfolio | `PortfolioManager` | `/app/core/trading/portfolio.py` |
| Metrics | `RealtimeMetrics` | `/app/core/trading/metrics.py` |
| Logger | `TradeLogger` | `/app/core/trading/logger.py` |
| Event Bus | `EventBus` | `/app/core/events.py` |

### Design: Shared Pipeline

Paper trading and live trading use the **identical** event flow. The only difference is the data source:

- **Paper**: `HistoricalReplayStream` replays historical OHLCV bars
- **Live**: `LiveDataStream` (Phase 4) provides real-time bars from broker WebSocket

The strategy code (`generate_signals(close, params)`) is never modified.

---

## Starting a Paper Trading Session

### Via REST API

```bash
curl -X POST http://localhost:18080/api/paper/start \
  -H "x-access-token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "def generate(close, params):\n    fast = close.rolling(params.get(\"fast\", 20)).mean()\n    slow = close.rolling(params.get(\"slow\", 60)).mean()\n    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))\n    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))\n    return entries, exits",
    "params": {"fast": 20, "slow": 60},
    "symbols": ["AAPL", "MSFT"],
    "start": "2023-01-01",
    "init_cash": 100000,
    "sizing_pct": 10.0,
    "max_position_pct": 20.0,
    "max_daily_loss_pct": 5.0,
    "max_drawdown_pct": 20.0
  }'
```

### What Happens

1. **Engine created**: Fresh `TradingEngine` with the specified `init_cash` and `RiskLimits`
2. **Strategy added**: `StrategyConfig` with code, params, symbols, sizing
3. **Data loaded**: `HistoricalReplayStream` loads OHLCV for each symbol via `data.loader.load_ohlcv()`
4. **Replay runs**: `engine.run_replay()` processes all bars sequentially
5. **Events fire**: For each bar, the full pipeline runs:
   - `process_bar(bar)` → updates prices
   - Strategy generates signals
   - Risk engine checks orders
   - Orders fill immediately (market orders)
   - Portfolio updates, metrics recorded, logs written
6. **Results returned**: Final metrics, portfolio state, and exposure

### Via Python (Direct)

```python
from core.trading.engine import TradingEngine, StrategyConfig
from core.trading.risk_engine import RiskLimits
from core.trading.market_data import HistoricalReplayStream

# Create engine
engine = TradingEngine(
    init_cash=100_000.0,
    risk_limits=RiskLimits(
        max_position_pct=20.0,
        max_daily_loss_pct=5.0,
        max_drawdown_pct=20.0,
    ),
)

# Add strategy
engine.add_strategy(StrategyConfig(
    code=your_strategy_code,
    params={"fast": 20, "slow": 60},
    symbols=["AAPL"],
    sizing_pct=10.0,
    stop_loss_pct=5.0,
))

# Load data
stream = HistoricalReplayStream()
stream.load_data("AAPL", ohlcv_dataframe)
engine.set_data_stream(stream)

# Run
result = engine.run_replay()
print(result["metrics"])
```

---

## Risk Engine Configuration

The `RiskEngine` performs **pre-trade risk checks** on every order before submission. If any check fails, the order is rejected and logged.

### RiskLimits

```python
from core.trading.risk_engine import RiskLimits

limits = RiskLimits(
    max_position_pct=20.0,       # Max % of equity in a single position
    max_daily_loss_pct=5.0,      # Daily loss limit before trading halts
    max_drawdown_pct=20.0,       # Max drawdown from peak equity
    max_exposure_pct=100.0,      # Max total market exposure
    min_cash_pct=5.0,            # Minimum cash reserve
    max_order_value_pct=25.0,    # Max single order value as % of equity
    max_open_orders=10,          # Max concurrent active orders
)
```

### Risk Checks (in order)

1. **Trading halted?** — If daily loss or drawdown limit was hit, all orders rejected
2. **Daily loss** — If unrealized+realized daily loss ≥ `max_daily_loss_pct`, halt trading
3. **Max drawdown** — If drawdown from peak ≥ `max_drawdown_pct`, halt trading
4. **Open orders** — Reject if active orders ≥ `max_open_orders`
5. **Sufficient cash** — Buy orders must not exceed available cash
6. **Order size** — Single order value ≤ `max_order_value_pct` of equity
7. **Position concentration** — Total position value ≤ `max_position_pct` of equity
8. **Total exposure** — Market exposure ≤ `max_exposure_pct`
9. **Cash reserve** — Remaining cash after order ≥ `min_cash_pct`
10. **Sell validation** — Must have sufficient position to sell

### API Configuration

Set risk limits when starting a session:

```json
{
  "max_position_pct": 15.0,
  "max_daily_loss_pct": 3.0,
  "max_drawdown_pct": 15.0
}
```

### Checking Risk Status

```bash
curl http://localhost:18080/api/paper/risk \
  -H "x-access-token: YOUR_TOKEN"
```

**Response:**
```json
{
  "halted": false,
  "halt_reason": "",
  "peak_equity": 112500.00,
  "current_drawdown_pct": 2.5,
  "daily_pnl": 350.00,
  "limits": {
    "max_position_pct": 20.0,
    "max_daily_loss_pct": 5.0,
    "max_drawdown_pct": 20.0,
    "max_exposure_pct": 100.0,
    "min_cash_pct": 5.0
  }
}
```

---

## Logging and Monitoring

### Structured Trade Logs

The `TradeLogger` records every trading event as a structured entry:

| Event Type | Description |
|---|---|
| `engine_start` | Trading engine started |
| `engine_stop` | Trading engine stopped |
| `signal_generated` | Strategy emitted a signal |
| `order_submitted` | Order created |
| `order_filled` | Order executed |
| `order_cancelled` | Order cancelled |
| `order_rejected` | Order rejected by execution |
| `risk_rejected` | Order rejected by risk engine |
| `risk_halt` | Trading halted (daily loss / drawdown) |
| `position_opened` | New position opened |
| `position_closed` | Position closed |
| `error` | Error event |

### Querying Logs

```bash
# All logs
curl http://localhost:18080/api/paper/logs \
  -H "x-access-token: YOUR_TOKEN"

# Filter by event type
curl "http://localhost:18080/api/paper/logs?event_type=risk_rejected&limit=20" \
  -H "x-access-token: YOUR_TOKEN"
```

### JSON File Logging

If `log_file` is specified in `TradeLogger`, events are appended as JSON-lines:

```jsonl
{"event": "order_filled", "timestamp": "2024-01-15T10:30:00", "order_id": "abc", "symbol": "AAPL", "side": "buy", "qty": 50, "price": 185.50, "amount": 9275.00}
{"event": "risk_rejected", "timestamp": "2024-01-15T10:31:00", "order_id": "def", "symbol": "TSLA", "reason": "Position concentration: 25.3% (max 20.0%)"}
```

### Monitoring via API

| Endpoint | Purpose |
|---|---|
| `GET /api/paper/status` | Full engine state, bar count, strategies, risk |
| `GET /api/paper/account` | Equity, cash, invested, exposure |
| `GET /api/paper/positions` | All open positions with PnL |
| `GET /api/paper/orders` | Order history |
| `GET /api/paper/equity-curve` | Equity curve data points |
| `GET /api/paper/risk` | Risk engine status and limits |
| `GET /api/paper/logs` | Structured event logs |

---

## Metrics Available

The `RealtimeMetrics` class calculates performance metrics from the equity curve and trade history.

### Accessing Metrics

```bash
curl http://localhost:18080/api/paper/metrics \
  -H "x-access-token: YOUR_TOKEN"
```

### Metrics Output

```json
{
  "equity": 112500.00,
  "total_return_pct": 12.50,
  "cagr_pct": 8.20,
  "sharpe": 1.23,
  "max_drawdown_pct": 8.50,
  "win_rate_pct": 55.0,
  "profit_factor": 1.65,
  "total_trades": 24,
  "winning_trades": 13,
  "losing_trades": 11,
  "gross_profit": 15200.00,
  "gross_loss": 7700.00,
  "daily_returns": [0.005, -0.002, 0.008, ...],
  "equity_curve": [
    {"date": "2023-01-03", "equity": 100000},
    {"date": "2023-01-04", "equity": 100500}
  ],
  "peak_equity": 115000.00,
  "n_trading_days": 252
}
```

### Metric Definitions

| Metric | Formula | Description |
|---|---|---|
| `equity` | cash + invested | Current portfolio value |
| `total_return_pct` | (equity / init_cash - 1) × 100 | Total return since start |
| `cagr_pct` | (equity / init)^(1/years) - 1 | Compound annual growth rate |
| `sharpe` | mean(daily_returns) / std(daily_returns) × √252 | Risk-adjusted return |
| `max_drawdown_pct` | max((peak - trough) / peak) × 100 | Worst peak-to-trough decline |
| `win_rate_pct` | winning_trades / total_trades × 100 | Percentage of profitable trades |
| `profit_factor` | gross_profit / gross_loss | Ratio of profits to losses |
| `total_trades` | count | Total completed round-trip trades |
| `peak_equity` | max(equity) | Highest equity reached |

### Equity Curve

The equity curve is sampled every bar (daily for daily data). Access via:

```bash
curl http://localhost:18080/api/paper/equity-curve \
  -H "x-access-token: YOUR_TOKEN"
```

Returns an array of `{ "timestamp": "...", "equity": ..., "cash": ..., "invested": ... }` points, suitable for charting.

---

## Managing Sessions

### Stop

```bash
curl -X POST http://localhost:18080/api/paper/stop \
  -H "x-access-token: YOUR_TOKEN"
```

### Reset

```bash
curl -X POST http://localhost:18080/api/paper/reset \
  -H "x-access-token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"init_cash": 50000}'
```

### Status

```bash
curl http://localhost:18080/api/paper/status \
  -H "x-access-token: YOUR_TOKEN"
```

**Response:**
```json
{
  "state": "running",
  "bars_processed": 250,
  "strategies": 1,
  "symbols": ["AAPL"],
  "portfolio": { "equity": 105000, "cash": 45000, ... },
  "risk": { "halted": false, "current_drawdown_pct": 2.5, ... },
  "active_orders": 0,
  "error": ""
}
```
