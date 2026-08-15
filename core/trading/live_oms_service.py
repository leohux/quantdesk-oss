"""Persistent OMS wrapper for locked live / paper broker validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.settings import get_settings
from core.execution.base import ExecutionEngine, OrderSide
from core.trading.oms import OMS, OrderReconciler
from core.trading.order_manager import OrderType


class LiveOMSService:
    def __init__(self, state_path: str | Path | None = None) -> None:
        settings = get_settings()
        self._path = Path(state_path or settings.live_oms_state_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.oms = OMS()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
            self.oms.load_state(state)
        except Exception:
            return

    def save(self) -> None:
        self._path.write_text(
            json.dumps(self.oms.save_state(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def submit_market(
        self,
        *,
        engine: ExecutionEngine,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        tracked = self.oms.submit_order(
            symbol=symbol,
            side=OrderSide(side),
            qty=float(qty),
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
        )
        broker = engine.submit_order(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide(side),
            client_order_id=client_order_id,
        )
        self.oms.ack_order(client_order_id, broker.id)
        self.save()
        return {
            "client_order_id": client_order_id,
            "broker_order_id": broker.id,
            "local_state": tracked.state.value,
        }

    def reconcile(self, engine: ExecutionEngine) -> dict[str, Any]:
        broker_orders = [
            {
                "broker_order_id": o.id,
                "symbol": o.symbol,
                "state": o.status.value,
            }
            for o in engine.get_orders(status="all", limit=200)
        ]
        discrepancies = OrderReconciler.reconcile(
            self.oms.get_all_orders(),
            broker_orders,
        )
        self.save()
        return {
            "blocked_on_discrepancy": bool(discrepancies),
            "discrepancies": [
                {
                    "kind": d.kind,
                    "client_order_id": d.client_order_id,
                    "broker_order_id": d.broker_order_id,
                    "symbol": d.symbol,
                    "local_state": d.local_state.value if d.local_state else None,
                    "broker_state": d.broker_state,
                    "detail": d.detail,
                }
                for d in discrepancies
            ],
            "local_orders": [x.to_dict() for x in self.oms.get_all_orders()],
            "saved_state_path": str(self._path),
        }
