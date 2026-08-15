# QuantDesk — REST API Documentation

Base URL: `http://localhost:8000` (Docker container exposes on port `18080`)

## Authentication

All `/api/*` endpoints require token-based authentication (except public paths).

### Headers

```
x-access-token: <your-token>
# or
Authorization: Bearer <your-token>
```

### Public Paths (no auth required)

- `GET /api/health`
- `GET /health`
- `GET /api/auth/status`
- `POST /api/auth/login`
- `GET /docs`, `/openapi.json`, `/redoc`

### Bootstrap Token

On first run, an access token is auto-generated and stored in settings. Retrieve it via:

```
GET /api/auth/bootstrap
Header: x-admin-setup: <SETUP_TOKEN env var, default "quantdesk-setup">
```

**Response:**
```json
{"access_token": "your-generated-token"}
```

### Login

```
POST /api/auth/login
Content-Type: application/json

{"token": "your-access-token"}
```

**Response:**
```json
{"ok": true, "token": "your-access-token"}
```

---

## Health & Status

### `GET /api/health`

```json
{
  "ok": true,
  "mode": "paper",
  "has_alpaca_keys": true,
  "data_provider": "auto",
  "domain": "quantdesk.example.com",
  "version": "0.3.0",
  "auth_required": true
}
```

---

## Backtest

### `POST /api/backtest`

Run a strategy backtest on historical data.

**Request:**
```json
{
  "strategy_id": "my-ma-cross",
  "code": "def generate(close, params):\n    fast = close.rolling(params.get('fast', 20)).mean()\n    slow = close.rolling(params.get('slow', 60)).mean()\n    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))\n    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))\n    return entries, exits",
  "params": {"fast": 20, "slow": 60},
  "symbols": ["AAPL", "MSFT"],
  "start": "2020-01-01",
  "end": "2024-01-01",
  "init_cash": 100000,
  "fees": 0.0005,
  "slippage_bps": 2
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `strategy_id` | string | null | Reference to saved strategy |
| `code` | string | null | Python strategy code (overrides saved) |
| `params` | object | {} | Strategy parameters |
| `symbols` | string[] | ["AAPL"] | Ticker symbols |
| `start` | string | "2020-01-01" | Start date |
| `end` | string | null | End date (null = today) |
| `init_cash` | float | 100000 | Initial capital |
| `fees` | float | 0.0005 | Commission rate |
| `slippage_bps` | float | 2 | Slippage in basis points |

**Response:**
```json
{
  "engine": "vectorbt",
  "symbols": ["AAPL"],
  "strategy_id": "my-ma-cross",
  "params": {"fast": 20, "slow": 60},
  "init_cash": 100000,
  "total_return_pct": 85.32,
  "max_drawdown_pct": 15.4,
  "sharpe": 1.23,
  "sortino": 1.85,
  "calmar": 0.95,
  "win_rate_pct": 55.0,
  "profit_factor": 1.65,
  "trades": 24,
  "end_value": 185320.00,
  "equity_curve": [
    {"date": "2020-01-02", "equity": 100000},
    {"date": "2020-01-03", "equity": 100450}
  ],
  "buy_hold_return_pct": 120.5,
  "per_symbol": [],
  "errors": []
}
```

---

## Strategy CRUD

### `GET /api/strategies`

List all saved strategies.

**Response:** `[{ "id": "...", "name": "...", "type": "...", "enabled": true, ... }]`

### `GET /api/strategies/{strategy_id}`

Get a single strategy by ID.

### `POST /api/strategies`

Create a new strategy.

**Request:**
```json
{
  "name": "My MA Cross",
  "type": "custom",
  "description": "Moving average crossover",
  "params": {"fast": 20, "slow": 60},
  "code": "def generate(close, params):\n    ..."
}
```

**Response:** `{ "id": "abc123", "name": "My MA Cross", ... }`

### `PATCH /api/strategies/{strategy_id}`

Update strategy fields (partial update).

**Request:**
```json
{
  "enabled": true,
  "params": {"fast": 15},
  "name": "Updated Name",
  "metrics": {"sharpe": 1.5, "total_return_pct": 45.0}
}
```

### `DELETE /api/strategies/{strategy_id}`

Delete a strategy.

**Response:** `{ "ok": true }`

### `POST /api/strategies/validate`

Validate strategy code without running it.

**Request:** `{ "code": "def generate(close, params): ..." }`

**Response:** `{ "ok": true }` or `400 { "detail": "error message" }`

### `GET /api/strategies/templates`

Get available strategy templates.

---

## Paper Trading (`/api/paper/*`)

### `POST /api/paper/start`

Start a paper trading session with historical replay.

**Request:**
```json
{
  "code": "def generate(close, params):\n    fast = close.rolling(params.get('fast', 20)).mean()\n    slow = close.rolling(params.get('slow', 60)).mean()\n    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))\n    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))\n    return entries, exits",
  "params": {"fast": 20, "slow": 60},
  "symbols": ["AAPL"],
  "start": "2023-01-01",
  "end": null,
  "init_cash": 100000,
  "sizing_pct": 10.0,
  "stop_loss_pct": 5.0,
  "max_position_pct": 20.0,
  "max_daily_loss_pct": 5.0,
  "max_drawdown_pct": 20.0
}
```

**Response:**
```json
{
  "ok": true,
  "state": "stopped",
  "bars_processed": 500,
  "metrics": { "equity": 112500, "total_return_pct": 12.5, "sharpe": 1.1, ... },
  "portfolio": { "equity": 112500, "cash": 50000, ... },
  "exposure": { "equity": 112500, "invested": 62500, ... }
}
```

### `POST /api/paper/stop`

Stop the running paper trading engine.

**Response:** `{ "ok": true, "state": "stopped" }`

### `POST /api/paper/reset`

Reset the engine to a fresh state.

**Request (optional):** `{ "init_cash": 100000 }`

**Response:** `{ "ok": true, "init_cash": 100000 }`

### `GET /api/paper/status`

Get full engine status.

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

### `GET /api/paper/account`

Get paper account state.

### `GET /api/paper/positions`

Get all open positions.

### `GET /api/paper/orders?status=all&limit=50`

Get order history. Filter by status: `all`, `filled`, `rejected`, `cancelled`.

### `GET /api/paper/transactions?limit=50`

Get transaction history.

### `GET /api/paper/metrics`

Get real-time performance metrics (Sharpe, CAGR, drawdown, win rate, etc.).

### `GET /api/paper/risk`

Get risk engine status and current limits.

### `GET /api/paper/logs?event_type=order_filled&limit=50`

Get structured trading event logs. Filter by event type.

### `GET /api/paper/equity-curve`

Get equity curve data points.

---

## Account, Positions & Orders (Alpaca)

These endpoints interact with the configured Alpaca paper trading account.

### `GET /api/account`

Get Alpaca account info.

**Response:**
```json
{
  "equity": "100000.00",
  "cash": "50000.00",
  "buying_power": "100000.00",
  "portfolio_value": "100000.00"
}
```

### `GET /api/positions`

Get all open Alpaca positions with portfolio weights.

**Response:**
```json
[
  {
    "symbol": "AAPL",
    "qty": "10",
    "market_value": "1850.00",
    "unrealized_pl": "50.00",
    "unrealized_plpc": "0.0278",
    "weight_pct": 1.85
  }
]
```

### `GET /api/orders?status=all&limit=50`

Get Alpaca order history.

### `POST /api/orders`

Place a market order on Alpaca.

**Request:**
```json
{
  "symbol": "AAPL",
  "qty": 10,
  "side": "buy"
}
```

### `DELETE /api/positions/{symbol}`

Close an entire Alpaca position.

---

## Dashboard

### `GET /api/dashboard`

Aggregated dashboard data (account, positions, orders, strategies, summary).

**Response:**
```json
{
  "account": { ... },
  "positions": [ ... ],
  "orders": [ ... ],
  "strategies": [ ... ],
  "summary": {
    "cash": 50000,
    "buying_power": 100000,
    "equity": 100000,
    "positions_count": 3,
    "invested_pct": 50.0,
    "today_pnl": 250.00,
    "today_pnl_pct": 0.25,
    "running_strategies": 2,
    "mode": "paper"
  }
}
```

---

## Settings

### `GET /api/settings`

Get current app settings (API keys are masked).

### `PUT /api/settings`

Update settings.

**Request:**
```json
{
  "alpaca_api_key": "PK...",
  "alpaca_secret_key": "...",
  "alpaca_mode": "paper",
  "data_provider": "alpaca",
  "risk_per_trade_pct": 2.0,
  "max_position_pct": 20.0
}
```

---

## Error Responses

All errors follow the format:

```json
{"detail": "Error message"}
```

HTTP status codes:
- `400` — Bad request (validation error)
- `401` — Unauthorized (missing/invalid token)
- `404` — Not found
- `500` — Internal server error
