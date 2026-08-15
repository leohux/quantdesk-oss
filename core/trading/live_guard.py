"""Live-trading fail-closed guardrails, previews, and readiness checks."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import get_settings
from core.execution.base import Account, ExecutionEngine, OrderSide, Position


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LiveCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class LivePreview:
    allowed: bool
    symbol: str
    side: str
    qty: float
    estimated_price: float
    estimated_notional: float
    checks: list[LiveCheck]
    mode: str
    submitted: bool = False
    client_order_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(x) for x in self.checks]
        return d


class LiveAuditLogger:
    """Immutable append-only audit log for live-readiness and live-order actions."""

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(path or settings.live_audit_log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **payload: Any) -> dict[str, Any]:
        entry = {
            "timestamp": _now(),
            "event": event,
            **payload,
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


class LiveTradingGuard:
    """Fail-closed checks for any broker that could submit live orders."""

    def __init__(self, engine: ExecutionEngine) -> None:
        self.settings = get_settings()
        self.engine = engine
        self.audit = LiveAuditLogger()

    def _config_hash(self) -> str:
        raw = {
            "quant_mode": self.settings.quant_mode,
            "ibkr_trading_mode": self.settings.ibkr_trading_mode,
            "ibkr_gateway_mode": self.settings.ibkr_gateway_mode,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "live_execution_armed": self.settings.live_execution_armed,
            "live_allowed_symbols": self.settings.live_allowed_symbol_list,
            "live_max_order_value_usd": self.settings.live_max_order_value_usd,
            "live_max_position_pct": self.settings.live_max_position_pct,
            "live_max_exposure_pct": self.settings.live_max_exposure_pct,
            "live_min_cash_pct": self.settings.live_min_cash_pct,
        }
        return hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    def readiness(self) -> dict[str, Any]:
        checks = [
            LiveCheck("engine_connected", self.engine.is_connected(), "broker connectivity"),
            LiveCheck(
                "gateway_mode_match",
                self.settings.ibkr_gateway_mode.lower() == self.settings.ibkr_trading_mode.lower()
                or self.settings.ibkr_gateway_mode.lower() == "mock",
                f"gateway={self.settings.ibkr_gateway_mode} trading_mode={self.settings.ibkr_trading_mode}",
            ),
            LiveCheck(
                "live_locked",
                not self.settings.live_submission_unlocked,
                "default safe state",
            ),
            LiveCheck(
                "account_whitelist_configured",
                bool(self.settings.ibkr_allowed_account_list),
                f"allowed_accounts={self.settings.ibkr_allowed_account_list}",
            ),
            LiveCheck(
                "read_only_default",
                bool(self.settings.ibkr_read_only),
                f"ibkr_read_only={self.settings.ibkr_read_only}",
            ),
        ]
        label = "LOCKED"
        if self.engine.mode == "paper" and self.engine.is_connected():
            label = "PAPER_CONNECTED"
        if self.settings.quant_mode == "live_locked" and self.engine.is_connected():
            label = "SHADOW_READY"
        if self.settings.live_submission_unlocked:
            label = "LIVE_ARMED"
        account = {}
        try:
            acct = self.engine.get_account()
            account = asdict(acct)
        except Exception as exc:  # pragma: no cover - defensive
            account = {"error": str(exc)}
        return {
            "state": label,
            "mode": self.engine.mode,
            "engine": self.engine.name,
            "config_hash": self._config_hash(),
            "checks": [asdict(x) for x in checks],
            "account": account,
            "live_submission_unlocked": self.settings.live_submission_unlocked,
        }

    def _check_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        account: Account,
        positions: list[Position],
        price: float,
        active_orders_count: int,
        strategy_id: str | None = None,
        require_unlocked: bool = False,
    ) -> LivePreview:
        side = side.lower().strip()
        symbol = symbol.upper().strip()
        notional = float(price) * float(qty)
        position = next((p for p in positions if p.symbol == symbol), None)
        position_value = float(position.market_value) if position else 0.0
        equity = float(account.equity)
        invested = sum(float(p.market_value) for p in positions)
        remaining_cash = float(account.cash) - (notional if side == "buy" else 0.0)
        checks: list[LiveCheck] = [
            LiveCheck("engine_connected", self.engine.is_connected(), "broker connectivity"),
            LiveCheck(
                "account_mode",
                account.mode in ("paper", "live"),
                f"account_mode={account.mode}",
            ),
            LiveCheck(
                "account_whitelist",
                not self.settings.ibkr_allowed_account_list
                or getattr(account, "account_id", None) in self.settings.ibkr_allowed_account_list
                or self.settings.ibkr_account.upper() in self.settings.ibkr_allowed_account_list,
                f"configured_account={self.settings.ibkr_account}",
            ),
            LiveCheck(
                "gateway_mode",
                self.settings.ibkr_gateway_mode.lower() in {"paper", "live", "mock"},
                f"gateway={self.settings.ibkr_gateway_mode}",
            ),
            LiveCheck(
                "live_lock",
                self.settings.live_submission_unlocked if require_unlocked else True,
                "submission lock must be armed for broker writes" if require_unlocked else "preview mode",
            ),
            LiveCheck(
                "allowed_symbol",
                not self.settings.live_allowed_symbol_list or symbol in self.settings.live_allowed_symbol_list,
                f"allowed_symbols={self.settings.live_allowed_symbol_list or 'ALL'}",
            ),
            LiveCheck(
                "allowed_side",
                side in self.settings.live_allowed_side_list,
                f"allowed_sides={self.settings.live_allowed_side_list}",
            ),
            LiveCheck(
                "max_order_value",
                notional <= self.settings.live_max_order_value_usd,
                f"notional=${notional:,.2f} max=${self.settings.live_max_order_value_usd:,.2f}",
            ),
            LiveCheck(
                "max_open_orders",
                active_orders_count < self.settings.live_max_open_orders,
                f"active={active_orders_count} max={self.settings.live_max_open_orders}",
            ),
        ]
        if side == "buy":
            new_pos_pct = ((position_value + notional) / equity * 100.0) if equity else 0.0
            exposure_pct = ((invested + notional) / equity * 100.0) if equity else 0.0
            cash_pct = (remaining_cash / equity * 100.0) if equity else 0.0
            checks.extend(
                [
                    LiveCheck("cash_available", remaining_cash >= 0, f"remaining_cash=${remaining_cash:,.2f}"),
                    LiveCheck(
                        "max_position_pct",
                        new_pos_pct <= self.settings.live_max_position_pct,
                        f"new_position={new_pos_pct:.2f}% max={self.settings.live_max_position_pct:.2f}%",
                    ),
                    LiveCheck(
                        "max_exposure_pct",
                        exposure_pct <= self.settings.live_max_exposure_pct,
                        f"exposure={exposure_pct:.2f}% max={self.settings.live_max_exposure_pct:.2f}%",
                    ),
                    LiveCheck(
                        "min_cash_pct",
                        cash_pct >= self.settings.live_min_cash_pct,
                        f"cash_pct={cash_pct:.2f}% min={self.settings.live_min_cash_pct:.2f}%",
                    ),
                ]
            )
        else:
            owned_qty = float(position.qty) if position else 0.0
            checks.extend(
                [
                    LiveCheck(
                        "reduce_only_sell",
                        (not self.settings.live_only_reduce_existing) or owned_qty >= qty,
                        f"owned={owned_qty} requested={qty}",
                    ),
                    LiveCheck(
                        "short_disabled",
                        not self.settings.live_allow_short,
                        "initial live phase forbids naked shorting",
                    ),
                ]
            )

        allowed = all(c.passed for c in checks)
        preview = LivePreview(
            allowed=allowed,
            symbol=symbol,
            side=side,
            qty=float(qty),
            estimated_price=float(price),
            estimated_notional=notional,
            checks=checks,
            mode=self.engine.mode,
        )
        self.audit.log(
            "live_order_preview",
            symbol=symbol,
            side=side,
            qty=qty,
            allowed=allowed,
            strategy_id=strategy_id,
            mode=self.engine.mode,
            config_hash=self._config_hash(),
            checks=[asdict(x) for x in checks],
        )
        return preview

    def preview_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        strategy_id: str | None = None,
    ) -> LivePreview:
        account = self.engine.get_account()
        positions = self.engine.get_positions()
        active_orders = self.engine.get_orders(status="open", limit=200)
        return self._check_order(
            symbol=symbol,
            side=side,
            qty=qty,
            account=account,
            positions=positions,
            price=price,
            active_orders_count=len(active_orders),
            strategy_id=strategy_id,
            require_unlocked=False,
        )

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        client_order_id: str,
        strategy_id: str | None = None,
    ) -> LivePreview:
        account = self.engine.get_account()
        positions = self.engine.get_positions()
        active_orders = self.engine.get_orders(status="open", limit=200)
        preview = self._check_order(
            symbol=symbol,
            side=side,
            qty=qty,
            account=account,
            positions=positions,
            price=price,
            active_orders_count=len(active_orders),
            strategy_id=strategy_id,
            require_unlocked=True,
        )
        preview.client_order_id = client_order_id
        if not preview.allowed:
            self.audit.log(
                "live_order_rejected",
                client_order_id=client_order_id,
                symbol=symbol.upper(),
                side=side.lower(),
                qty=qty,
                reason="guard_rejected",
                checks=[asdict(x) for x in preview.checks],
            )
            return preview

        order = self.engine.submit_order(
            symbol=symbol.upper(),
            qty=float(qty),
            side=OrderSide(side.lower()),
            client_order_id=client_order_id,
        )
        preview.submitted = True
        self.audit.log(
            "live_order_submitted",
            client_order_id=client_order_id,
            broker_order_id=order.id,
            symbol=symbol.upper(),
            side=side.lower(),
            qty=qty,
            estimated_price=price,
            strategy_id=strategy_id,
        )
        return preview
