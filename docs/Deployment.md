# QuantDesk — Deployment Guide

## Docker Setup

QuantDesk runs as a multi-container Docker application with three services.

### Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `quantdesk` | Custom build | `18080:8000` | FastAPI application + React frontend |
| `postgres` | `postgres:16-alpine` | `5432:5432` | Persistent storage |
| `redis` | `redis:7-alpine` | `6379:6379` | Caching / pub-sub |

### docker-compose.yml

```yaml
services:
  quantdesk:
    build: .
    container_name: quantdesk
    restart: unless-stopped
    stop_grace_period: 30s
    ports:
      - "18080:8000"
    env_file:
      - .env
    volumes:
      - ./data/store:/app/data/store
      - ./data/cache:/app/data/cache
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - quantdesk-net

  postgres:
    image: postgres:16-alpine
    container_name: quantdesk-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: quantdesk
      POSTGRES_PASSWORD: quantdesk
      POSTGRES_DB: quantdesk
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U quantdesk"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  redis:
    image: redis:7-alpine
    container_name: quantdesk-redis
    restart: unless-stopped
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 5s
```

### Build & Run

```bash
# Build and start all services
docker compose up -d --build

# View logs
docker compose logs -f quantdesk

# Stop
docker compose down

# Stop and remove volumes
docker compose down -v
```

### Dockerfile (multi-stage)

```dockerfile
# Stage 1: Build frontend
FROM node:22-alpine AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY backtest ./backtest
COPY config ./config
COPY data ./data
COPY execution ./execution
COPY core ./core
COPY strategies ./strategies
COPY tests ./tests
COPY scripts ./scripts
COPY --from=web-build /app/web/dist ./web/dist

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## Environment Variables

Create a `.env` file in the project root:

```bash
# ── Alpaca API ──────────────────────────
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# ── Data Provider ───────────────────────
DATA_PROVIDER=auto          # auto | alpaca | yfinance
DEFAULT_SYMBOL=AAPL

# ── API Server ──────────────────────────
API_HOST=0.0.0.0
API_PORT=8000
CORS_ORIGINS=*              # comma-separated origins or *

# ── Authentication ──────────────────────
ACCESS_TOKEN=               # auto-generated if empty
SETUP_TOKEN=quantdesk-setup # one-time bootstrap token

# ── Database ────────────────────────────
POSTGRES_URL=postgresql://quantdesk:quantdesk@postgres:5432/quantdesk

# ── Telegram Notifications ──────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── Webhook ─────────────────────────────
WEBHOOK_URL=

# ── Strategy Defaults ──────────────────
FAST_MA=20
SLOW_MA=60
RISK_PER_TRADE_PCT=2.0
MAX_POSITION_PCT=20.0
```

### Variable Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `ALPACA_API_KEY` | No | `""` | Alpaca API key for paper/live trading |
| `ALPACA_SECRET_KEY` | No | `""` | Alpaca secret key |
| `ALPACA_BASE_URL` | No | `https://paper-api.alpaca.markets` | Alpaca base URL |
| `DATA_PROVIDER` | No | `auto` | Market data provider (`auto` tries Alpaca, falls back to yfinance) |
| `ACCESS_TOKEN` | No | auto-generated | API access token |
| `SETUP_TOKEN` | No | `quantdesk-setup` | Bootstrap token for first-time setup |
| `POSTGRES_URL` | No | — | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token for notifications |
| `CORS_ORIGINS` | No | `*` | Allowed CORS origins |

### IBKR Live (locked by default)

Public template now includes:

```bash
IBKR_HOST=ibkr-gateway
IBKR_PORT=4002
IBKR_CLIENT_ID=101
IBKR_GATEWAY_MODE=paper
IBKR_TRADING_MODE=paper
IBKR_READ_ONLY=true
LIVE_TRADING_ENABLED=false
LIVE_EXECUTION_ARMED=false
```

Recommended deployment model:

- run IB Gateway on the same server as QuantDesk
- keep it on the internal Docker bridge only
- do **not** expose 4001 / 4002 publicly
- keep `IBKR_READ_ONLY=true` until Paper/Shadow validation is complete

### IB Gateway profile

The compose file now includes an `ibkr` profile:

```bash
docker compose --profile ibkr up -d ibkr-gateway
```

This starts the private Gateway container without enabling live trading.

---

## Config Profiles

### Development

```bash
# .env.development
DATA_PROVIDER=yfinance
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ACCESS_TOKEN=dev-token-123
CORS_ORIGINS=http://localhost:5173,http://localhost:3000
```

Run locally without Docker:
```bash
cd /app
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### Paper Trading

```bash
# .env.paper
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DATA_PROVIDER=alpaca
ACCESS_TOKEN=<strong-random-token>
```

### Production

```bash
# .env.production
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # change for live
DATA_PROVIDER=alpaca
ACCESS_TOKEN=<strong-random-token>
CORS_ORIGINS=https://quant.yourdomain.com
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## Health Checks

### Docker Health Check

The container has a built-in health check:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
```

### API Health Endpoint

```
GET /api/health
```

**Response:**
```json
{
  "ok": true,
  "mode": "paper",
  "has_alpaca_keys": true,
  "data_provider": "auto",
  "version": "0.3.0",
  "auth_required": true
}
```

### Service Health Checks

**PostgreSQL:**
```bash
docker compose exec postgres pg_isready -U quantdesk
```

**Redis:**
```bash
docker compose exec redis redis-cli ping
```

### Monitoring Container Status

```bash
# Check all service health
docker compose ps

# View resource usage
docker stats quantdesk quantdesk-postgres quantdesk-redis

# Tail application logs
docker compose logs -f --tail=100 quantdesk
```

---

## Monitoring Setup

### Structured Logging

QuantDesk outputs JSON-formatted log lines to stdout. Each line contains:

```json
{
  "timestamp": "2024-01-15T10:30:00",
  "level": "INFO",
  "logger": "quantdesk.trading",
  "message": "[order_filled] ...",
  "module": "engine",
  "function": "_on_fill",
  "line": 217,
  "order_id": "abc123",
  "symbol": "AAPL"
}
```

### Trade Audit Log

A dedicated `audit.log` file is written to `/app/data/store/audit.log` with all order, fill, and risk events in JSON-lines format.

### Log Aggregation

For production, pipe Docker logs to a log aggregator:

```bash
# Example: Promtail/Loki
docker compose logs -f quantdesk | promtail --config.file=promtail.yml

# Example: Filebeat
# Mount /var/lib/docker/containers for Filebeat to read
```

### Metrics Endpoint

```
GET /api/paper/metrics
```

Returns real-time performance metrics including Sharpe ratio, drawdown, win rate, and equity curve.

### Telegram Alerts

Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to receive:
- Trade execution alerts
- Risk rejection alerts
- System error alerts

Alerts are dispatched asynchronously via the `NotificationManager` queue.

### Prometheus (Optional)

To add Prometheus metrics, expose a `/metrics` endpoint using `prometheus_client`:

```python
from prometheus_client import make_asgi_app
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
```

---

## Volumes & Data Persistence

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data/store` | `/app/data/store` | Strategy configs, settings, audit logs |
| `./data/cache` | `/app/data/cache` | Cached market data |
| Named volume `postgres-data` | `/var/lib/postgresql/data` | PostgreSQL data |
| Named volume `redis-data` | `/data` | Redis data |

### Backup

```bash
# Backup PostgreSQL
docker compose exec postgres pg_dump -U quantdesk quantdesk > backup.sql

# Backup strategy store
cp -r ./data/store ./backups/store-$(date +%Y%m%d)
```
