# QuantDesk — IBKR Live (Locked)

## Philosophy

QuantDesk now ships a **live-ready but fail-closed** IBKR path:

- IBKR Gateway runs privately inside Docker
- `/api/live/*` provides read-only readiness, risk, account, positions, orders, reconcile, and audit
- real-money order submission stays locked behind **three independent switches**

Default state:

```text
LIVE_TRADING_ENABLED=false
LIVE_EXECUTION_ARMED=false
IBKR_TRADING_MODE=paper
IBKR_GATEWAY_MODE=paper
IBKR_READ_ONLY=true
```

If any of these do not match the expected state, broker writes are rejected.

## Runtime states

| State | Meaning |
|---|---|
| `LOCKED` | Default. Live broker writes forbidden. |
| `PAPER_CONNECTED` | IBKR Paper Gateway reachable; still locked. |
| `SHADOW_READY` | Gateway reachable + live stack healthy, but broker writes still locked. |
| `LIVE_ARMED` | All locks explicitly armed. Intended for future canary only. |

## Security model

Real broker writes require:

1. admin JWT
2. short-lived arming token
3. `LIVE_TRADING_ENABLED=true`
4. `LIVE_EXECUTION_ARMED=true`
5. `IBKR_TRADING_MODE=live`
6. `IBKR_GATEWAY_MODE=live`
7. `IBKR_READ_ONLY=false`
8. whitelisted IBKR account

Missing any one of these should reject the order.

## API surface

Read-only endpoints:

- `GET /api/live/status`
- `GET /api/live/readiness`
- `GET /api/live/account`
- `GET /api/live/positions`
- `GET /api/live/orders`
- `GET /api/live/risk`
- `GET /api/live/audit`
- `GET /api/live/reconcile`
- `POST /api/live/order-preview`

Locked write endpoints:

- `POST /api/live/orders`
- `DELETE /api/live/orders/{order_id}`
- `DELETE /api/live/positions/{symbol}`

## Deployment notes

- Run IB Gateway on the server, private Docker network only
- do not expose 4001/4002 to the public internet
- use `.env.example` as the public template; keep real IBKR credentials in `.env` or secrets only

## Recovery / fault drill checklist

Before any future canary:

1. restart IB Gateway and verify `/api/live/reconcile`
2. verify read-only readiness stays available
3. verify duplicate `client_order_id` rejection
4. verify live writes still reject while locked
5. verify audit log is append-only and records preview / reject / reconcile
