"""
Order Management System (OMS) - Professional-grade order lifecycle management.

Provides order state machine, idempotent order submission, broker ID mapping,
reconciliation, and thread-safe order tracking on top of OrderManager.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set

from core.trading.order_manager import Order, OrderManager, OrderSide, OrderType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. OrderState enum
# ---------------------------------------------------------------------------

class OrderState(Enum):
    """Lifecycle states of an order."""
    NEW = auto()
    PENDING_VALIDATION = auto()
    SUBMITTED = auto()
    ACKNOWLEDGED = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    PENDING_CANCEL = auto()
    CANCELLED = auto()
    REJECTED = auto()
    EXPIRED = auto()


# ---------------------------------------------------------------------------
# 2. OrderStateMachine
# ---------------------------------------------------------------------------

# Allowed transitions: from_state -> set of valid to_states
_VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
    OrderState.NEW: {OrderState.PENDING_VALIDATION, OrderState.SUBMITTED, OrderState.REJECTED},
    OrderState.PENDING_VALIDATION: {OrderState.SUBMITTED, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.PENDING_CANCEL},
    OrderState.ACKNOWLEDGED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.PENDING_CANCEL, OrderState.EXPIRED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.PENDING_CANCEL, OrderState.EXPIRED},
    OrderState.FILLED: set(),  # terminal
    OrderState.PENDING_CANCEL: {OrderState.CANCELLED, OrderState.FILLED, OrderState.PARTIALLY_FILLED},
    OrderState.CANCELLED: set(),  # terminal
    OrderState.REJECTED: set(),  # terminal
    OrderState.EXPIRED: set(),  # terminal
}


class OrderStateMachine:
    """Enforces valid order lifecycle transitions with optional callbacks."""

    def __init__(
        self,
        initial_state: OrderState = OrderState.NEW,
        on_transition: Optional[Callable[[OrderState, OrderState], None]] = None,
    ) -> None:
        self._state = initial_state
        self._on_transition = on_transition
        self._history: List[Dict[str, Any]] = [
            {"from": None, "to": initial_state, "ts": datetime.now(timezone.utc).isoformat()}
        ]

    @property
    def state(self) -> OrderState:
        return self._state

    @property
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    def get_valid_transitions(self, state: Optional[OrderState] = None) -> Set[OrderState]:
        """Return set of states reachable from *state* (default: current)."""
        return set(_VALID_TRANSITIONS.get(state or self._state, set()))

    def transition(self, new_state: OrderState) -> None:
        """Attempt a state transition; raises ValueError on invalid move."""
        if new_state == self._state:
            return  # idempotent no-op
        allowed = _VALID_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Invalid transition {self._state.name} -> {new_state.name}. "
                f"Valid targets: {[s.name for s in allowed]}"
            )
        old_state = self._state
        self._state = new_state
        record = {
            "from": old_state,
            "to": new_state,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._history.append(record)
        if self._on_transition:
            try:
                self._on_transition(old_state, new_state)
            except Exception:
                logger.exception("on_transition callback failed")


# ---------------------------------------------------------------------------
# Internal tracked order wrapper
# ---------------------------------------------------------------------------

@dataclass
class TrackedOrder:
    """Internal representation holding full lifecycle metadata."""
    order: Order
    state_machine: OrderStateMachine
    client_order_id: str
    broker_order_id: Optional[str] = None
    fills: List[Dict[str, Any]] = field(default_factory=list)
    total_filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    total_commission: float = 0.0
    reject_reason: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def state(self) -> OrderState:
        return self.state_machine.state

    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.order.symbol,
            "side": self.order.side.value,
            "qty": self.order.qty,
            "order_type": self.order.order_type.value,
            "limit_price": self.order.limit_price,
            "state": self.state.name,
            "state_history": self.state_machine.history,
            "fills": self.fills,
            "total_filled_qty": self.total_filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "total_commission": self.total_commission,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrackedOrder":
        order = Order(
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            qty=d["qty"],
            order_type=OrderType(d["order_type"]),
            limit_price=d.get("limit_price"),
        )
        sm = OrderStateMachine(initial_state=OrderState[d["state"]])
        tracked = cls(
            order=order,
            state_machine=sm,
            client_order_id=d["client_order_id"],
            broker_order_id=d.get("broker_order_id"),
            fills=d.get("fills", []),
            total_filled_qty=d.get("total_filled_qty", 0.0),
            avg_fill_price=d.get("avg_fill_price", 0.0),
            total_commission=d.get("total_commission", 0.0),
            reject_reason=d.get("reject_reason"),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )
        return tracked


# ---------------------------------------------------------------------------
# 3. OMS
# ---------------------------------------------------------------------------

class OMS:
    """
    Order Management System – thread-safe, idempotent order lifecycle manager.

    Sits on top of OrderManager and adds:
      - State machine enforcement
      - client_order_id <-> broker_order_id mapping
      - Fill tracking
      - Serialization for persistence
      - Callback hooks for event-bus integration
    """

    def __init__(
        self,
        order_manager: Optional[OrderManager] = None,
        on_state_change: Optional[Callable[[str, OrderState, OrderState], None]] = None,
    ) -> None:
        self._order_manager = order_manager or OrderManager()
        self._lock = threading.Lock()
        # client_order_id -> TrackedOrder
        self._orders: Dict[str, TrackedOrder] = {}
        # broker_order_id -> client_order_id (reverse lookup)
        self._broker_to_client: Dict[str, str] = {}
        self._on_state_change = on_state_change

    # -- helpers ---------------------------------------------------------------

    def _make_transition_callback(self, client_order_id: str) -> Callable[[OrderState, OrderState], None]:
        def _cb(old: OrderState, new: OrderState) -> None:
            self._orders[client_order_id].updated_at = datetime.now(timezone.utc).isoformat()
            if self._on_state_change:
                try:
                    self._on_state_change(client_order_id, old, new)
                except Exception:
                    logger.exception("on_state_change callback failed for %s", client_order_id)
        return _cb

    # -- public API ------------------------------------------------------------

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        order_type: OrderType,
        limit_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> TrackedOrder:
        """Submit a new order. Idempotent: duplicate client_order_id raises ValueError."""
        with self._lock:
            cid = client_order_id or f"clt-{uuid.uuid4().hex[:16]}"
            if cid in self._orders:
                raise ValueError(f"Duplicate client_order_id: {cid}")

            order = Order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                limit_price=limit_price,
            )
            sm = OrderStateMachine(initial_state=OrderState.NEW)
            tracked = TrackedOrder(order=order, state_machine=sm, client_order_id=cid)

            # Register in _orders BEFORE transitions so callbacks can find it
            self._orders[cid] = tracked

            # Wire callback now that it's registered
            sm._on_transition = self._make_transition_callback(cid)

            # Validate then submit
            sm.transition(OrderState.PENDING_VALIDATION)
            sm.transition(OrderState.SUBMITTED)
            # Register with underlying OrderManager for fill/cancel tracking
            # We use create_order to get a properly registered order, then
            # store the manager's order reference alongside our tracked state.
            om_order = self._order_manager.create_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                limit_price=limit_price,
            )
            tracked.order = om_order  # swap to manager-registered instance
            logger.info("OMS: submitted order %s (%s %s %s)", cid, side.value, qty, symbol)
            return tracked

    def ack_order(self, client_order_id: str, broker_order_id: str) -> None:
        """Broker acknowledges the order, providing its broker-side ID."""
        with self._lock:
            tracked = self._get_tracked(client_order_id)
            tracked.state_machine.transition(OrderState.ACKNOWLEDGED)
            tracked.broker_order_id = broker_order_id
            self._broker_to_client[broker_order_id] = client_order_id
            logger.info("OMS: ack order %s -> broker %s", client_order_id, broker_order_id)

    def fill_order(
        self,
        client_order_id: str,
        qty: float,
        price: float,
        commission: float = 0.0,
    ) -> None:
        """Record a (partial) fill."""
        with self._lock:
            tracked = self._get_tracked(client_order_id)
            fill = {
                "qty": qty,
                "price": price,
                "commission": commission,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            tracked.fills.append(fill)
            tracked.total_commission += commission

            # Update average fill price
            prev_qty = tracked.total_filled_qty
            prev_notional = tracked.avg_fill_price * prev_qty
            new_qty = prev_qty + qty
            if new_qty > 0:
                tracked.avg_fill_price = (prev_notional + price * qty) / new_qty
            tracked.total_filled_qty = new_qty

            # Decide new state
            if new_qty >= tracked.order.qty:
                tracked.state_machine.transition(OrderState.FILLED)
            else:
                tracked.state_machine.transition(OrderState.PARTIALLY_FILLED)

            logger.info(
                "OMS: fill order %s qty=%.4f px=%.4f filled=%.4f/%.4f",
                client_order_id, qty, price, new_qty, tracked.order.qty,
            )

    def cancel_order(self, client_order_id: str) -> None:
        """Request cancellation of an order."""
        with self._lock:
            tracked = self._get_tracked(client_order_id)
            tracked.state_machine.transition(OrderState.PENDING_CANCEL)
            tracked.state_machine.transition(OrderState.CANCELLED)
            logger.info("OMS: cancel order %s", client_order_id)

    def reject_order(self, client_order_id: str, reason: str) -> None:
        """Mark an order as rejected (e.g. by risk check or broker)."""
        with self._lock:
            tracked = self._get_tracked(client_order_id)
            tracked.reject_reason = reason
            tracked.state_machine.transition(OrderState.REJECTED)
            logger.info("OMS: reject order %s reason=%s", client_order_id, reason)

    def expire_order(self, client_order_id: str) -> None:
        """Mark an order as expired."""
        with self._lock:
            tracked = self._get_tracked(client_order_id)
            tracked.state_machine.transition(OrderState.EXPIRED)
            logger.info("OMS: expire order %s", client_order_id)

    # -- queries ---------------------------------------------------------------

    def get_order(self, client_order_id: str) -> TrackedOrder:
        with self._lock:
            return self._get_tracked(client_order_id)

    def get_active_orders(self) -> List[TrackedOrder]:
        """Return orders in non-terminal states."""
        terminal = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
        with self._lock:
            return [t for t in self._orders.values() if t.state not in terminal]

    def get_order_by_broker_id(self, broker_id: str) -> Optional[TrackedOrder]:
        with self._lock:
            cid = self._broker_to_client.get(broker_id)
            if cid is None:
                return None
            return self._orders.get(cid)

    def get_all_orders(self) -> List[TrackedOrder]:
        with self._lock:
            return list(self._orders.values())

    # -- serialization ---------------------------------------------------------

    def save_state(self) -> Dict[str, Any]:
        """Serialize entire OMS state to a dict for persistence."""
        with self._lock:
            return {
                "orders": {cid: t.to_dict() for cid, t in self._orders.items()},
                "broker_to_client": dict(self._broker_to_client),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }

    def load_state(self, state: Dict[str, Any]) -> None:
        """Restore OMS from a previously saved state dict."""
        with self._lock:
            self._orders.clear()
            self._broker_to_client.clear()
            for cid, order_dict in state.get("orders", {}).items():
                tracked = TrackedOrder.from_dict(order_dict)
                # Re-wire transition callback
                tracked.state_machine._on_transition = self._make_transition_callback(cid)
                self._orders[cid] = tracked
            self._broker_to_client.update(state.get("broker_to_client", {}))
            logger.info("OMS: loaded %d orders from state", len(self._orders))

    # -- internal --------------------------------------------------------------

    def _get_tracked(self, client_order_id: str) -> TrackedOrder:
        tracked = self._orders.get(client_order_id)
        if tracked is None:
            raise KeyError(f"Unknown client_order_id: {client_order_id}")
        return tracked


# ---------------------------------------------------------------------------
# 4. OrderReconciler
# ---------------------------------------------------------------------------

@dataclass
class Discrepancy:
    """A mismatch between local and broker order state."""
    kind: str  # "missing_on_broker", "missing_locally", "state_mismatch"
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    symbol: Optional[str] = None
    local_state: Optional[OrderState] = None
    broker_state: Optional[str] = None
    detail: str = ""


class OrderReconciler:
    """
    Reconcile local OMS orders against broker-reported orders.

    Used after reconnection to detect:
      - Orders present locally but missing on the broker
      - Orders present on the broker but missing locally
      - State mismatches between local and broker views
    """

    @staticmethod
    def reconcile(
        local_orders: List[TrackedOrder],
        broker_orders: List[Dict[str, Any]],
    ) -> List[Discrepancy]:
        """
        Compare local tracked orders with broker-reported order snapshots.

        Parameters
        ----------
        local_orders : list of TrackedOrder
            Orders known to the local OMS.
        broker_orders : list of dict
            Each dict should contain at minimum:
              - "broker_order_id": str
              - "symbol": str  (optional)
              - "state": str   (optional, broker-side state name)

        Returns
        -------
        list of Discrepancy
        """
        discrepancies: List[Discrepancy] = []

        # Index broker orders by broker_order_id
        broker_map: Dict[str, Dict[str, Any]] = {}
        for bo in broker_orders:
            bid = bo.get("broker_order_id")
            if bid:
                broker_map[bid] = bo

        # Index local orders by broker_order_id
        local_by_broker: Dict[str, TrackedOrder] = {}
        for lo in local_orders:
            if lo.broker_order_id:
                local_by_broker[lo.broker_order_id] = lo

        # 1. Local orders with broker_id but not found on broker
        for lo in local_orders:
            if lo.broker_order_id and lo.broker_order_id not in broker_map:
                discrepancies.append(Discrepancy(
                    kind="missing_on_broker",
                    client_order_id=lo.client_order_id,
                    broker_order_id=lo.broker_order_id,
                    symbol=lo.order.symbol,
                    local_state=lo.state,
                    detail=f"Order {lo.client_order_id} (broker: {lo.broker_order_id}) not found on broker",
                ))
            elif lo.broker_order_id and lo.broker_order_id in broker_map:
                # 2. State mismatch
                broker_state_str = broker_map[lo.broker_order_id].get("state", "")
                if broker_state_str:
                    try:
                        broker_state = OrderState[broker_state_str.upper()]
                    except KeyError:
                        broker_state = None
                    if broker_state and broker_state != lo.state:
                        discrepancies.append(Discrepancy(
                            kind="state_mismatch",
                            client_order_id=lo.client_order_id,
                            broker_order_id=lo.broker_order_id,
                            symbol=lo.order.symbol,
                            local_state=lo.state,
                            broker_state=broker_state_str,
                            detail=(
                                f"State mismatch for {lo.client_order_id}: "
                                f"local={lo.state.name}, broker={broker_state_str}"
                            ),
                        ))

        # 3. Broker orders not found locally
        known_broker_ids = {lo.broker_order_id for lo in local_orders if lo.broker_order_id}
        for bid, bo in broker_map.items():
            if bid not in known_broker_ids:
                discrepancies.append(Discrepancy(
                    kind="missing_locally",
                    broker_order_id=bid,
                    symbol=bo.get("symbol"),
                    broker_state=bo.get("state"),
                    detail=f"Broker order {bid} not found in local OMS",
                ))

        return discrepancies
