from __future__ import annotations

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from backtest.runner import enrich_chart_metrics, run_ma_backtest, run_signal_backtest
from backtest.parallel import cpu_budget, parallel_backtest_symbols
from config.settings import get_settings
from core.auth import role_required
from core.execution.base import OrderSide
from core.execution.ibkr import IBKRExecutionEngine
from core.trading.live_guard import LiveTradingGuard
from core.trading.live_oms_service import LiveOMSService
from config.store import (
    create_strategy,
    delete_strategy,
    get_app_settings,
    get_strategy,
    list_strategies,
    save_app_settings,
    update_strategy,
)
from data.loader import load_ohlcv
from execution.alpaca_client import AlpacaPaperClient
from strategies.engine import TEMPLATES, validate_strategy_code
from strategies.ma_cross import latest_signal, ma_crossover_signals

settings = get_settings()
WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"

# Bootstrap access token: env ACCESS_TOKEN or auto-create in settings store
def _access_token() -> str:
    from core.auth import get_static_access_token

    return get_static_access_token()


ACCESS_TOKEN = _access_token()

PUBLIC_PATHS = {
    "/api/health",
    "/health",
    "/api/auth/status",
    "/api/auth/jwt-login",
    "/api/auth/jwt-refresh",
    "/docs",
    "/openapi.json",
    "/redoc",
}


class AccessTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path in PUBLIC_PATHS
            or path.startswith("/assets/")
            or not path.startswith("/api/")
        ):
            return await call_next(request)

        # Allow login endpoint
        if path == "/api/auth/login":
            return await call_next(request)

        auth = request.headers.get("x-access-token") or request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        auth = (auth or "").strip()
        if not auth:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        try:
            from core.auth import authenticate_token

            user = authenticate_token(auth)
            if user:
                request.state.user = user
                return await call_next(request)
        except Exception:
            pass

        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


app = FastAPI(
    title="QuantDesk API",
    description="Personal US equities quant platform",
    version="0.3.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AccessTokenMiddleware)


class BacktestRequest(BaseModel):
    strategy_id: str | None = None
    code: str | None = None
    params: dict[str, Any] | None = None
    symbols: list[str] = Field(default_factory=lambda: ["AAPL"])
    symbol: str | None = None
    start: str = "2020-01-01"
    end: str | None = None
    fast: int = Field(default=20, ge=2)
    slow: int = Field(default=60, ge=3)
    init_cash: float = Field(default=100_000, gt=0)
    fees: float = Field(default=0.0005, ge=0)
    slippage_bps: float = Field(default=2, ge=0)


class OrderRequest(BaseModel):
    symbol: str
    qty: float = Field(gt=0)
    side: Literal["buy", "sell"]


class SignalRequest(BaseModel):
    symbol: str = "AAPL"
    fast: int = 20
    slow: int = 60
    start: str = "2020-01-01"


class StrategyPatch(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None
    code: str | None = None
    metrics: dict[str, Any] | None = None


class StrategyCreate(BaseModel):
    name: str
    type: str = "ma_cross"
    description: str = ""
    params: dict[str, Any] | None = None
    code: str | None = None


class SettingsPatch(BaseModel):
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_mode: str | None = None
    polygon_api_key: str | None = None
    telegram_bot_token: str | None = None
    webhook_url: str | None = None
    postgres_url: str | None = None
    duckdb_path: str | None = None
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    access_token: str | None = None


class LoginRequest(BaseModel):
    token: str


class ValidateCodeRequest(BaseModel):
    code: str


class LivePreviewRequest(BaseModel):
    symbol: str
    qty: float = Field(gt=0)
    side: Literal["buy", "sell"]
    price: float = Field(gt=0)
    strategy_id: str | None = None


class LiveSubmitRequest(LivePreviewRequest):
    arming_token: str | None = None


def _paper() -> AlpacaPaperClient:
    return AlpacaPaperClient()


def _live_engine() -> IBKRExecutionEngine:
    return IBKRExecutionEngine()


def _live_guard() -> LiveTradingGuard:
    return LiveTradingGuard(_live_engine())


def _live_oms() -> LiveOMSService:
    return LiveOMSService()


def _require_live_arming(body: LiveSubmitRequest) -> None:
    if not settings.live_arming_token or body.arming_token != settings.live_arming_token:
        raise HTTPException(status_code=403, detail="Live arming token required")


@app.get("/api/health")
@app.get("/health")
def health() -> dict[str, Any]:
    live = _live_guard().readiness()
    return {
        "ok": True,
        "mode": "paper",
        "has_alpaca_keys": settings.has_alpaca_keys,
        "data_provider": settings.data_provider,
        "domain": "quantdesk.example.com",
        "version": "0.3.0",
        "auth_required": True,
        "live": {
            "state": live["state"],
            "engine": live["engine"],
            "submission_unlocked": live["live_submission_unlocked"],
        },
    }


@app.get("/api/auth/status")
def auth_status() -> dict[str, Any]:
    return {"auth_required": True}


@app.post("/api/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    if body.token.strip() != _access_token():
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"ok": True, "token": body.token.strip()}


@app.get("/api/auth/bootstrap")
def auth_bootstrap(x_admin_setup: str | None = Header(default=None)) -> dict[str, Any]:
    """One-time helper: only works if SETUP_TOKEN env matches."""
    setup = os.getenv("SETUP_TOKEN", "quantdesk-setup")
    if x_admin_setup != setup:
        raise HTTPException(status_code=401, detail="Forbidden")
    return {"access_token": _access_token()}


# ── JWT Auth endpoints (admin/viewer login with username+password) ──
from pydantic import BaseModel as _BaseModel

class _JWTLoginRequest(_BaseModel):
    password: str

class _JWTRefreshRequest(_BaseModel):
    refresh_token: str

@app.post("/api/auth/jwt-login")
def jwt_login(body: _JWTLoginRequest):
    from core.auth import USERS_DB, create_access_token, create_refresh_token

    login_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not login_password:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD is not configured (fail-closed)",
        )
    if body.password != login_password:
        raise HTTPException(status_code=401, detail="Incorrect password")

    user = USERS_DB["admin"]
    token_data = {"sub": user["username"], "role": user["role"]}
    return {
        "access_token": create_access_token(token_data),
        "refresh_token": create_refresh_token(token_data),
        "token_type": "bearer",
        "role": user["role"],
    }

@app.post("/api/auth/jwt-refresh")
def jwt_refresh(body: _JWTRefreshRequest):
    from core.auth import verify_token, USERS_DB, create_access_token
    payload = verify_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    username = payload.get("sub")
    user = USERS_DB.get(username) if username else None
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return {"access_token": create_access_token({"sub": user["username"], "role": user["role"]}), "token_type": "bearer"}



@app.get("/api/account")
@app.get("/account")
def get_account() -> dict[str, Any]:
    try:
        return _paper().account()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _positions_with_weights(client: AlpacaPaperClient, equity: float) -> list[dict[str, Any]]:
    positions = client.positions()
    eq = max(float(equity or 0), 1e-9)
    for p in positions:
        mv = float(p.get("market_value") or 0)
        p["weight_pct"] = round(mv / eq * 100, 2)
    return positions


@app.get("/api/positions")
@app.get("/positions")
def get_positions() -> list[dict[str, Any]]:
    try:
        from core.portfolio.pnl import portfolio_pnl

        client = _paper()
        account = client.account()
        positions = _positions_with_weights(client, float(account.get("equity") or 0))
        ownership = (portfolio_pnl(positions) or {}).get("ownership") or {}
        for p in positions:
            owner = ownership.get(str(p.get("symbol", "")).upper())
            p["strategy_id"] = owner["strategy_id"] if owner else None
            p["strategy_name"] = owner["strategy_name"] if owner else None
        return positions
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/orders")
@app.get("/orders")
def get_orders(status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
    try:
        return _paper().orders(status=status, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/orders")
@app.post("/orders")
def place_order(body: OrderRequest) -> dict[str, Any]:
    try:
        return _paper().market_order(body.symbol, body.qty, body.side)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/positions/{symbol}")
@app.delete("/positions/{symbol}")
def close_position(symbol: str) -> dict[str, Any]:
    try:
        return _paper().close_position(symbol)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    try:
        from core.portfolio.pnl import portfolio_pnl, record_equity_snapshot

        client = _paper()
        account = client.account()
        positions = _positions_with_weights(client, float(account.get("equity") or 0))
        orders = client.orders(status="all", limit=20)
        strategies = list_strategies()
        enabled = [s for s in strategies if s.get("enabled")]
        invested = sum(float(p.get("market_value") or 0) for p in positions)
        equity = float(account.get("equity") or 0)
        cash = float(account.get("cash") or 0)
        # Intraday P&L is equity drift since the prior close, not the lifetime
        # unrealized P&L of open positions.
        last_equity = float(account.get("last_equity") or 0)
        today_pnl = equity - last_equity if last_equity else 0.0
        unrealized_pnl = sum(float(p.get("unrealized_pl") or 0) for p in positions)
        pnl = portfolio_pnl(positions, strategies)
        record_equity_snapshot(equity, cash, last_equity, source="dashboard")
        # Attach sleeve owner onto each position for the UI column.
        ownership = pnl.get("ownership") or {}
        for p in positions:
            owner = ownership.get(str(p.get("symbol", "")).upper())
            if owner:
                p["strategy_id"] = owner["strategy_id"]
                p["strategy_name"] = owner["strategy_name"]
            else:
                p["strategy_id"] = None
                p["strategy_name"] = None
        return {
            "account": account,
            "positions": positions,
            "orders": orders,
            "strategies": strategies,
            "strategy_pnl": pnl.get("by_strategy") or [],
            "summary": {
                "cash": cash,
                "buying_power": float(account.get("buying_power") or 0),
                "equity": equity,
                "positions_count": len(positions),
                "invested_pct": round(invested / equity * 100, 2) if equity else 0,
                "today_pnl": today_pnl,
                "today_pnl_pct": round(today_pnl / last_equity * 100, 2) if last_equity else 0,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": round(unrealized_pnl / equity * 100, 2) if equity else 0,
                "realized_pnl": float(pnl.get("realized_pnl") or 0),
                "today_realized_pnl": float(pnl.get("today_realized_pnl") or 0),
                "running_strategies": len(enabled),
                "mode": "paper",
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/journal")
def journal(
    status: str | None = None,
    limit: int = 100,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    from core.portfolio.pnl import enrich_open_journal, journal_summary, list_journal

    rows = list_journal(status=status, limit=limit, strategy_id=strategy_id)
    # Floating P&L for open lots — journal table alone has no marks.
    try:
        positions = _paper().positions()
    except Exception:
        positions = []
    rows = enrich_open_journal(rows, positions)
    # Lifetime totals (same SQL as dashboard) — independent of list filter/limit.
    summary = journal_summary(strategy_id=strategy_id)
    return {
        "trades": rows,
        "count": len(rows),
        "realized_pnl": summary["realized_pnl"],
        "today_realized_pnl": summary["today_realized_pnl"],
        "closed_trades": summary["closed_trades"],
        "open_trades": summary["open_trades"],
    }


@app.get("/api/equity/curve")
def equity_curve_api(days: int = 30) -> dict[str, Any]:
    from core.portfolio.pnl import equity_curve, record_equity_snapshot

    try:
        acct = _paper().account()
        record_equity_snapshot(
            float(acct.get("equity") or 0),
            float(acct.get("cash") or 0),
            float(acct.get("last_equity") or 0),
            source="curve",
        )
    except Exception:
        pass
    points = equity_curve(days=days)
    return {"days": days, "points": points}


@app.get("/api/live/status")
def live_status() -> dict[str, Any]:
    return _live_guard().readiness()


@app.get("/api/live/readiness")
def live_readiness() -> dict[str, Any]:
    guard = _live_guard()
    engine = _live_engine()
    account = engine.get_account() if engine.is_connected() else None
    positions = engine.get_positions() if engine.is_connected() else []
    orders = engine.get_orders(status="open", limit=100) if engine.is_connected() else []
    return {
        **guard.readiness(),
        "account": account.__dict__ if account else None,
        "positions": [p.__dict__ for p in positions],
        "orders": [o.__dict__ for o in orders],
        "settings": {
            "quant_mode": settings.quant_mode,
            "ibkr_host": settings.ibkr_host,
            "ibkr_port": settings.ibkr_port,
            "ibkr_gateway_mode": settings.ibkr_gateway_mode,
            "ibkr_trading_mode": settings.ibkr_trading_mode,
            "ibkr_read_only": settings.ibkr_read_only,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_execution_armed": settings.live_execution_armed,
            "live_allowed_symbols": settings.live_allowed_symbol_list,
        },
    }


@app.get("/api/live/account")
def live_account() -> dict[str, Any]:
    try:
        return _live_engine().get_account().__dict__
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/live/positions")
def live_positions() -> list[dict[str, Any]]:
    try:
        return [p.__dict__ for p in _live_engine().get_positions()]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/live/orders")
def live_orders(status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
    try:
        return [o.__dict__ for o in _live_engine().get_orders(status=status, limit=limit)]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/live/risk")
def live_risk() -> dict[str, Any]:
    return {
        "quant_mode": settings.quant_mode,
        "live_submission_unlocked": settings.live_submission_unlocked,
        "limits": {
            "max_order_value_usd": settings.live_max_order_value_usd,
            "max_position_pct": settings.live_max_position_pct,
            "max_exposure_pct": settings.live_max_exposure_pct,
            "max_daily_loss_pct": settings.live_max_daily_loss_pct,
            "max_drawdown_pct": settings.live_max_drawdown_pct,
            "min_cash_pct": settings.live_min_cash_pct,
            "max_open_orders": settings.live_max_open_orders,
            "reduce_only": settings.live_only_reduce_existing,
            "allow_short": settings.live_allow_short,
            "allow_margin": settings.live_allow_margin,
            "allow_extended_hours": settings.live_allow_extended_hours,
        },
    }


@app.get("/api/live/audit")
def live_audit(limit: int = 200) -> list[dict[str, Any]]:
    path = Path(settings.live_audit_log_path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


@app.get("/api/live/reconcile")
def live_reconcile() -> dict[str, Any]:
    engine = _live_engine()
    connected = engine.is_connected()
    orders = [o.__dict__ for o in engine.get_orders(status="all", limit=100)] if connected else []
    payload = _live_oms().reconcile(engine) if connected else {"blocked_on_discrepancy": True, "discrepancies": [{"kind": "disconnected", "detail": "engine not connected"}], "local_orders": []}
    return {"connected": connected, "broker_orders": orders, **payload}


@app.post("/api/live/order-preview")
def live_order_preview(body: LivePreviewRequest) -> dict[str, Any]:
    guard = _live_guard()
    try:
        preview = guard.preview_order(
            symbol=body.symbol,
            side=body.side,
            qty=body.qty,
            price=body.price,
            strategy_id=body.strategy_id,
        )
        return preview.to_dict()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/live/orders")
def live_submit_order(
    body: LiveSubmitRequest,
    user: dict = Depends(role_required("admin")),
) -> dict[str, Any]:
    _require_live_arming(body)
    guard = _live_guard()
    try:
        client_order_id = f"live-{body.symbol.upper()}-{int(datetime.utcnow().timestamp())}"
        preview = guard.preview_order(
            symbol=body.symbol,
            side=body.side,
            qty=body.qty,
            price=body.price,
            strategy_id=body.strategy_id,
        )
        if not settings.live_submission_unlocked:
            raise HTTPException(status_code=403, detail="Live submission locked")
        if not preview.allowed:
            raise HTTPException(status_code=403, detail=preview.to_dict())
        oms = _live_oms()
        tracking = oms.submit_market(
            engine=_live_engine(),
            symbol=body.symbol.upper(),
            side=body.side,
            qty=body.qty,
            client_order_id=client_order_id,
        )
        out = preview.to_dict()
        out["submitted"] = True
        out["client_order_id"] = client_order_id
        out["oms"] = tracking
        return out
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/live/orders/{order_id}")
def live_cancel_order(order_id: str, user: dict = Depends(role_required("admin"))) -> dict[str, Any]:
    if not settings.live_submission_unlocked:
        raise HTTPException(status_code=403, detail="Live cancellation locked")
    ok = _live_engine().cancel_order(order_id)
    return {"ok": ok, "order_id": order_id, "requested_by": user.get("sub")}


@app.delete("/api/live/positions/{symbol}")
def live_close_live_position(symbol: str, user: dict = Depends(role_required("admin"))) -> dict[str, Any]:
    if not settings.live_submission_unlocked:
        raise HTTPException(status_code=403, detail="Live close-position locked")
    order = _live_engine().close_position(symbol)
    return {"ok": True, "order": order.__dict__, "requested_by": user.get("sub")}


@app.get("/api/strategies")
def get_strategies(scope: str | None = None) -> list[dict[str, Any]]:
    if scope == "manual":
        return list_strategies(manual_only=True)
    if scope == "mined":
        return list_strategies(mined_only=True)
    return list_strategies()


@app.get("/api/strategies/templates")
def strategy_templates() -> dict[str, str]:
    return TEMPLATES


@app.get("/api/strategies/{strategy_id}")
def get_one_strategy(strategy_id: str) -> dict[str, Any]:
    try:
        return get_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Strategy not found") from exc


@app.post("/api/strategies")
def post_strategy(body: StrategyCreate) -> dict[str, Any]:
    try:
        return create_strategy(
            name=body.name,
            strategy_type=body.type,
            description=body.description,
            params=body.params,
            code=body.code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/strategies/{strategy_id}")
def patch_strategy(strategy_id: str, body: StrategyPatch) -> dict[str, Any]:
    try:
        return update_strategy(strategy_id, body.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Strategy not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/strategies/{strategy_id}")
def remove_strategy(strategy_id: str) -> dict[str, Any]:
    try:
        delete_strategy(strategy_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/strategies/validate")
def validate_code(body: ValidateCodeRequest) -> dict[str, Any]:
    try:
        validate_strategy_code(body.code)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings")
def read_settings() -> dict[str, Any]:
    return get_app_settings()


@app.put("/api/settings")
def write_settings(body: SettingsPatch) -> dict[str, Any]:
    return save_app_settings(body.model_dump(exclude_none=True))


@app.post("/api/backtest")
@app.post("/backtest")
def backtest(body: BacktestRequest) -> dict[str, Any]:
    symbols = [s.upper() for s in (body.symbols or [])]
    if body.symbol:
        symbols = [body.symbol.upper()]
    if not symbols:
        symbols = ["AAPL"]

    code = body.code
    params = dict(body.params or {})
    if body.strategy_id:
        try:
            strat = get_strategy(body.strategy_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found") from exc
        code = code or strat.get("code")
        merged = {**strat.get("params", {}), **params}
        params = merged
        if not symbols or symbols == ["AAPL"]:
            syms = strat.get("params", {}).get("symbols")
            if isinstance(syms, list) and syms:
                symbols = [str(s).upper() for s in syms]

    if not code:
        # legacy MA path
        if body.fast >= body.slow:
            raise HTTPException(status_code=400, detail="fast must be < slow")
        code = TEMPLATES["ma_cross"]
        params = {"fast": body.fast, "slow": body.slow, **params}

    try:
        validate_strategy_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    fees = body.fees + (body.slippage_bps / 10000.0)
    cash_each = body.init_cash / max(len(symbols), 1)

    # Multi-symbol: fan out across ~80% host CPUs
    if len(symbols) > 1:
        per_symbol, errors = parallel_backtest_symbols(
            symbols=symbols,
            code=code,
            params=params,
            start=body.start,
            end=body.end,
            init_cash_each=cash_each,
            fees=fees,
            workers=cpu_budget(),
        )
    else:
        per_symbol = []
        errors = []
        sym = symbols[0]
        try:
            ohlcv = load_ohlcv(sym, start=body.start, end=body.end)
            result = run_signal_backtest(
                ohlcv,
                code=code,
                params=params,
                init_cash=cash_each,
                fees=fees,
            )
            result["symbol"] = sym
            result["bars"] = len(ohlcv)
            result["start"] = str(ohlcv.index[0].date())
            result["end"] = str(ohlcv.index[-1].date())
            per_symbol.append(result)
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)})

    if not per_symbol:
        raise HTTPException(status_code=500, detail={"errors": errors})

    primary = per_symbol[0]
    if len(per_symbol) == 1:
        out = dict(primary)
        out["symbols"] = symbols
        out["strategy_id"] = body.strategy_id
        out["errors"] = errors
        if body.strategy_id:
            try:
                update_strategy(
                    body.strategy_id,
                    {
                        "metrics": {
                            "sharpe": out.get("sharpe"),
                            "total_return_pct": out.get("total_return_pct"),
                            "max_drawdown_pct": out.get("max_drawdown_pct"),
                        }
                    },
                )
            except Exception:
                pass
        return out

    date_map: dict[str, float] = {}
    for item in per_symbol:
        for pt in item.get("equity_curve", []):
            date_map[pt["date"]] = date_map.get(pt["date"], 0.0) + float(pt["equity"])
    equity_curve = [{"date": d, "equity": v} for d, v in sorted(date_map.items())]
    end_value = equity_curve[-1]["equity"] if equity_curve else body.init_cash
    total_return_pct = (end_value / body.init_cash - 1) * 100
    sharpes = [x["sharpe"] for x in per_symbol if x.get("sharpe") is not None]
    max_dds = [x["max_drawdown_pct"] for x in per_symbol]
    charts = enrich_chart_metrics(equity_curve)
    out = {
        "engine": primary.get("engine"),
        "symbols": symbols,
        "strategy_id": body.strategy_id,
        "params": params,
        "init_cash": body.init_cash,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max(max_dds) if max_dds else 0,
        "sharpe": sum(sharpes) / len(sharpes) if sharpes else None,
        "sortino": primary.get("sortino"),
        "calmar": primary.get("calmar"),
        "win_rate_pct": primary.get("win_rate_pct"),
        "profit_factor": primary.get("profit_factor"),
        "trades": sum(int(x.get("trades") or 0) for x in per_symbol),
        "end_value": end_value,
        "equity_curve": equity_curve[-365:],
        "buy_hold_return_pct": primary.get("buy_hold_return_pct"),
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
        "errors": errors,
        **charts,
    }
    if body.strategy_id:
        try:
            update_strategy(
                body.strategy_id,
                {
                    "metrics": {
                        "sharpe": out.get("sharpe"),
                        "total_return_pct": out.get("total_return_pct"),
                        "max_drawdown_pct": out.get("max_drawdown_pct"),
                    }
                },
            )
        except Exception:
            pass
    return out


@app.post("/api/signal")
@app.post("/signal")
def signal(body: SignalRequest) -> dict[str, Any]:
    if body.fast >= body.slow:
        raise HTTPException(status_code=400, detail="fast must be < slow")
    try:
        ohlcv = load_ohlcv(body.symbol, start=body.start)
        frame = ma_crossover_signals(ohlcv["Close"], fast=body.fast, slow=body.slow)
        out = latest_signal(frame)
        out["symbol"] = body.symbol.upper()
        out["as_of"] = str(frame.index[-1].date())
        return out
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc



# ── WebSocket routes (direct registration, bypasses _IncludedRouter bug) ──
from core.ws import ws_endpoint as _ws_endpoint

@app.websocket("/ws/orders")
async def ws_orders(websocket: WebSocket):
    await _ws_endpoint(websocket, "orders")

@app.websocket("/ws/positions")
async def ws_positions(websocket: WebSocket):
    await _ws_endpoint(websocket, "positions")

@app.websocket("/ws/pnl")
async def ws_pnl(websocket: WebSocket):
    await _ws_endpoint(websocket, "pnl")

@app.websocket("/ws/health")
async def ws_health(websocket: WebSocket):
    await _ws_endpoint(websocket, "health")

@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await _ws_endpoint(websocket, "logs")

@app.websocket("/ws/strategies")
async def ws_strategies(websocket: WebSocket):
    await _ws_endpoint(websocket, "strategies")

@app.websocket("/ws/all")
async def ws_all(websocket: WebSocket):
    await _ws_endpoint(websocket, "all")


# ── Startup: DB init + WebSocket broadcaster ──
@app.on_event("startup")
async def on_startup():
    try:
        from core.db import create_all
        await create_all()
        import logging
        logging.getLogger(__name__).info("Database tables initialized")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("DB init failed (will use JSON fallback): %s", exc)
    try:
        from core.ws import start_broadcaster
        start_broadcaster(app)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("WS broadcaster start failed: %s", exc)


if WEB_DIST.exists():
    assets = WEB_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        candidate = WEB_DIST / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")

