# QuantDesk — Event-Driven Flow

QuantDesk uses a synchronous pub/sub `EventBus` to decouple all trading components. The canonical event flow is:

```
MarketData → Strategy → Signal → Risk → Order → Fill → Portfolio Update
```

## Event Types

Defined in `/app/core/events.py`:

| EventType | Published When | Key Data Fields |
|---|---|---|
| `MARKET_DATA` | A new OHLCV bar arrives | `symbol`, `open`, `high`, `low`, `close`, `volume`, `timestamp` |
| `SIGNAL` | Strategy emits a buy/sell signal | `symbol`, `signal` (`buy`/`sell`), `price` |
| `RISK_CHECK` | Risk engine evaluates an order | `order_id`, `symbol`, `passed`, `reason` |
| `ORDER` | Order submitted to execution | `order_id`, `symbol`, `side`, `qty`, `price` |
| `FILL` | Order executed/filled | `order_id`, `symbol`, `side`, `qty`, `price`, `commission` |
| `EXECUTION` | Execution engine state change | Engine-specific data |
| `ERROR` | Error in any component | `message`, `error`, `context` |

## Event Data Structure

```python
from core.events import Event, EventType

event = Event(
    type=EventType.SIGNAL,
    data={"symbol": "AAPL", "signal": "buy", "price": 185.50},
    timestamp=datetime.utcnow(),  # auto-set if omitted
    source="ma_cross_strategy",
)

# Access data like a dict
price = event["price"]        # raises KeyError
price = event.get("price")    # returns None if missing
```

## EventBus Pub/Sub Pattern

The `EventBus` is a simple synchronous dispatcher. All handlers run in the publishing thread.

### Subscribing

```python
from core.events import EventBus, EventType, get_event_bus

bus = get_event_bus()

def on_fill(event):
    print(f"Filled: {event['symbol']} x{event['qty']} @ ${event['price']}")

bus.subscribe(EventType.FILL, on_fill)
```

### Publishing

```python
bus.publish(Event(EventType.FILL, {
    "order_id": "abc123",
    "symbol": "AAPL",
    "side": "buy",
    "qty": 100,
    "price": 185.50,
    "commission": 1.86,
}))
```

### Unsubscribing

```python
bus.unsubscribe(EventType.FILL, on_fill)
```

### Error Handling

Handler exceptions are caught and logged — one failing handler does not block others:

```python
def publish(self, event: Event) -> None:
    handlers = self._handlers.get(event.type, [])
    for handler in handlers:
        try:
            handler(event)
        except Exception:
            logger.exception("Event handler error for %s", event.type)
```

## Detailed Event Flow

### 1. Market Data → Strategy

The `TradingEngine.process_bar()` method receives a `Bar` object and updates portfolio prices:

```python
def process_bar(self, bar: Bar) -> None:
    self._latest_prices[bar.symbol] = bar.close
    self._portfolio.update_prices({bar.symbol: bar.close})
    self._check_limit_orders(bar)

    # Run all strategies that watch this symbol
    for config in self._strategies:
        if bar.symbol in config.symbols:
            self._run_strategy_signal(config, bar.symbol, bar.close, bar.timestamp)
```

### 2. Strategy → Signal

The strategy's `generate_signals()` is called with the price history. When a new entry or exit is detected:

```python
entries, exits = run_signal_fn(close, config.code, config.params)

if last_entry and prev_signal != "buy":
    # BUY signal
    self._event_bus.publish(Event(EventType.SIGNAL, {
        "symbol": symbol, "signal": "buy", "price": current_price,
    }))
```

### 3. Signal → Risk Check → Order

After emitting the signal, the engine calculates position size and submits an order through the risk engine:

```python
# Calculate size
allocation = equity * (config.sizing_pct / 100.0)
qty = int(allocation / current_price)

# Create order
order = self._order_manager.create_order(symbol, side, qty, ...)

# Risk check — must pass before submission
check = self._risk_engine.check_order(order, price, active_orders)
if not check:
    self._order_manager.reject_order(order.id, check.reject_reason)
    return

# Publish order event
self._event_bus.publish(Event(EventType.ORDER, {
    "order_id": order.id, "symbol": symbol,
    "side": side.value, "qty": qty, "price": price,
}))
```

### 4. Order → Fill

For market orders, fill happens immediately:

```python
def _fill_order(self, order, qty, price):
    commission = price * qty * 0.0001  # 1bps
    self._order_manager.fill_order(order.id, qty, price, commission)
    self._portfolio.open_position(order.symbol, qty, price, order.side, commission)

    self._event_bus.publish(Event(EventType.FILL, {
        "order_id": order.id, "symbol": order.symbol,
        "side": order.side.value, "qty": qty, "price": price,
        "commission": commission,
    }))
```

### 5. Fill → Portfolio + Metrics + Logging

Event handlers registered in `_setup_events()` process fills for logging and metrics:

```python
def _on_fill(self, event: Event) -> None:
    self._trade_logger.log_order_filled(
        event["order_id"], event["symbol"],
        event["side"], event["qty"], event["price"],
    )
    if event["side"] == "sell":
        self._metrics.record_trade(...)
```

## How Backtest and Paper Trading Share the Same Flow

Both modes use the **same strategy interface** — `generate_signals(close, params) → (entries, exits)`:

```
┌─────────────────────────────────────────────────────┐
│              shared: core/strategy/                  │
│  Strategy.generate_signals(close, params)            │
│  CodeStrategy → compile_strategy(code) → run_signal │
└──────────────┬──────────────────────┬───────────────┘
               │                      │
     ┌─────────▼──────────┐  ┌───────▼────────────┐
     │   Backtest Engine   │  │  TradingEngine      │
     │   (core/backtest/)  │  │  (core/trading/)    │
     │                     │  │                     │
     │  VectorBT/Pandas    │  │  EventBus-driven    │
     │  simulation         │  │  RiskEngine checks  │
     │  No risk engine     │  │  PortfolioManager   │
     │  Instant results    │  │  OrderManager       │
     └─────────────────────┘  │  TradeLogger        │
                              │  RealtimeMetrics    │
                              └─────────────────────┘
```

**Backtest** runs signals through a vectorized simulation (vectorbt or pandas loop). No risk engine, no event bus — just fast signal → equity curve.

**Paper Trading** runs signals bar-by-bar through the full event-driven pipeline with risk checks, order management, portfolio tracking, and structured logging. The `HistoricalReplayStream` feeds historical bars into `TradingEngine.process_bar()`, triggering the same `MarketData → Strategy → Signal → Risk → Order → Fill` flow as live trading.

This design means:
- **Test strategies in backtest** (fast, no side effects)
- **Validate in paper trading** (full risk pipeline, real order flow)
- **Go live** by swapping `HistoricalReplayStream` for `LiveDataStream` and `PaperExecutionEngine` for `AlpacaPaperClient` — strategy code stays identical
