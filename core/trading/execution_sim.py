"""Simulated Execution Engine - realistic execution modeling on top of paper broker.

Applies spread, slippage, partial fills, queue delay, rejections, and market
impact to every order.  Wraps ``PaperExecutionEngine`` from
``core.execution.paper`` so no real broker connection is needed.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.execution.base import (
    Account,
    ExecutionEngine,
    Order,
    OrderSide,
    OrderStatus,
    Position,
)
from core.execution.paper import PaperExecutionEngine

logger = logging.getLogger(__name__)


# ── Execution Profile ────────────────────────────────────────────────

@dataclass
class ExecutionProfile:
    """Tunable knobs for execution realism.

    All basis-point values are in **basis points** of the fill price
    (1 bps = 0.01%).
    """

    spread_bps: float = 5.0
    """Bid-ask spread in basis points."""

    slippage_bps: float = 2.0
    """Random market-impact slippage in basis points."""

    partial_fill_prob: float = 0.1
    """Probability that an order is only partially filled."""

    partial_fill_pct_min: float = 0.3
    """Minimum fill percentage when a partial fill occurs (0.3 = 30%)."""

    queue_delay_ms: float = 50.0
    """Simulated order-queue latency in milliseconds."""

    rejection_prob: float = 0.02
    """Probability that the exchange rejects the order outright."""

    market_impact_factor: float = 0.001
    """Additional price impact per dollar of notional order value."""


# ── Simulation Stats Tracker ─────────────────────────────────────────

@dataclass
class _SimulationStats:
    """Internal counter for simulation effects."""

    total_orders: int = 0
    rejected_count: int = 0
    partial_fill_count: int = 0
    spread_applied_count: int = 0
    slippage_applied_count: int = 0
    market_impact_applied_count: int = 0
    total_spread_cost: float = 0.0
    total_slippage_cost: float = 0.0
    total_market_impact_cost: float = 0.0
    total_queue_delay_ms: float = 0.0


# ── Simulated Execution Engine ───────────────────────────────────────

class SimulatedExecutionEngine(ExecutionEngine):
    """Wraps ``PaperExecutionEngine`` with realistic execution modeling.

    Every order submitted through this engine is subject to:

    * **Spread** – buy fills at *price + spread/2*, sell fills at
      *price − spread/2*.
    * **Slippage** – additional random slippage within ``slippage_bps``.
    * **Partial fills** – with ``partial_fill_prob`` the order is only
      partially filled (at least ``partial_fill_pct_min`` of requested
      quantity).
    * **Queue delay** – a simulated delay of ``queue_delay_ms`` before
      the fill is reported.
    * **Rejection** – with ``rejection_prob`` the order is rejected.
    * **Market impact** – the fill price moves against the trader by
      ``market_impact_factor × notional_value``.
    """

    def __init__(
        self,
        paper_engine: PaperExecutionEngine | None = None,
        profile: ExecutionProfile | None = None,
        seed: int | None = None,
    ) -> None:
        self._engine = paper_engine or PaperExecutionEngine()
        self._profile = profile or ExecutionProfile()
        self._stats = _SimulationStats()
        self._rng = random.Random(seed)

    # ── ExecutionEngine ABC properties ───────────────────────────

    @property
    def name(self) -> str:
        return "simulated"

    @property
    def mode(self) -> str:
        return "paper"

    # ── Delegated (no simulation needed) ─────────────────────────

    def get_account(self) -> Account:
        return self._engine.get_account()

    def get_positions(self) -> list[Position]:
        return self._engine.get_positions()

    def get_orders(
        self,
        status: str = "all",
        limit: int = 50,
    ) -> list[Order]:
        return self._engine.get_orders(status=status, limit=limit)

    def cancel_order(self, order_id: str) -> bool:
        return self._engine.cancel_order(order_id)

    def close_position(self, symbol: str) -> Order:
        """Close an entire position – still goes through simulation."""
        pos_map = {
            p.symbol: p
            for p in self._engine.get_positions()
        }
        pos = pos_map.get(symbol.upper())
        if pos is None:
            raise ValueError(f"No position in {symbol}")
        return self.submit_order(symbol, pos.qty, OrderSide.SELL)

    def is_connected(self) -> bool:
        return self._engine.is_connected()

    # ── Core simulated path ──────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        price: float | None = None,
    ) -> Order:
        """Submit an order with full execution simulation.

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        qty : float
            Requested quantity.
        side : OrderSide
            BUY or SELL.
        price : float, optional
            Reference price.  If *None*, uses the paper engine's last known
            price for the symbol.

        Returns
        -------
        Order
            The filled (or rejected) order.
        """
        self._stats.total_orders += 1
        profile = self._profile

        # Resolve the reference price from the paper engine's internal cache
        # when the caller doesn't supply one.
        if price is None:
            price = self._engine._prices.get(symbol.upper())
            if price is None:
                raise ValueError(
                    f"No price data for {symbol}. Set price first."
                )

        # ── 1. Rejection check ──────────────────────────────────
        if self._rng.random() < profile.rejection_prob:
            self._stats.rejected_count += 1
            logger.info(
                "[SIM] Order REJECTED (random rejection): %s %s %.0f @ %.2f",
                side.value, symbol, qty, price,
            )
            # Record as rejected in the paper engine's order list
            return self._record_rejected(symbol, qty, side, price)

        # ── 2. Spread adjustment ────────────────────────────────
        spread = price * (profile.spread_bps / 10_000.0)
        if side == OrderSide.BUY:
            base_price = price + spread / 2.0   # buy at ask
        else:
            base_price = price - spread / 2.0   # sell at bid

        self._stats.spread_applied_count += 1
        self._stats.total_spread_cost += spread * qty

        # ── 3. Slippage ─────────────────────────────────────────
        slip_bps = self._rng.uniform(0, profile.slippage_bps)
        slippage = price * (slip_bps / 10_000.0)
        if side == OrderSide.BUY:
            base_price += slippage               # price moves against us
        else:
            base_price -= slippage

        self._stats.slippage_applied_count += 1
        self._stats.total_slippage_cost += slippage * qty

        # ── 4. Market impact ────────────────────────────────────
        notional = price * qty
        impact = notional * profile.market_impact_factor
        impact_per_share = impact / qty if qty else 0.0
        if side == OrderSide.BUY:
            base_price += impact_per_share
        else:
            base_price -= impact_per_share

        self._stats.market_impact_applied_count += 1
        self._stats.total_market_impact_cost += impact

        # ── 5. Partial fill ─────────────────────────────────────
        fill_qty = qty
        if self._rng.random() < profile.partial_fill_prob:
            fill_pct = self._rng.uniform(profile.partial_fill_pct_min, 1.0)
            fill_qty = round(qty * fill_pct, 6)
            if fill_qty <= 0:
                fill_qty = qty  # fallback – shouldn't happen
            self._stats.partial_fill_count += 1
            logger.info(
                "[SIM] PARTIAL FILL: %s %s requested=%.0f filled=%.2f (%.0f%%)",
                side.value, symbol, qty, fill_qty, fill_pct * 100,
            )

        # ── 6. Queue delay ──────────────────────────────────────
        delay_sec = profile.queue_delay_ms / 1_000.0
        if delay_sec > 0:
            self._stats.total_queue_delay_ms += profile.queue_delay_ms
            # Actually sleep a tiny bit for realism, but cap at 5ms
            # so tests don't crawl.
            actual_sleep = min(delay_sec, 0.005)
            time.sleep(actual_sleep)

        # ── 7. Submit to paper engine ───────────────────────────
        logger.info(
            "[SIM] FILL %s %s qty=%.2f price=%.4f (ref=%.4f, "
            "spread=%.4f, slip=%.4f, impact=%.4f)",
            side.value, symbol, fill_qty, base_price, price,
            spread, slippage, impact_per_share,
        )
        return self._engine.submit_order(symbol, fill_qty, side, price=base_price)

    # ── Simulation stats ─────────────────────────────────────────

    def get_simulation_stats(self) -> dict[str, Any]:
        """Return a summary dict of all simulation effects applied so far."""
        s = self._stats
        return {
            "total_orders": s.total_orders,
            "rejected_count": s.rejected_count,
            "partial_fill_count": s.partial_fill_count,
            "spread_applied_count": s.spread_applied_count,
            "slippage_applied_count": s.slippage_applied_count,
            "market_impact_applied_count": s.market_impact_applied_count,
            "total_spread_cost": round(s.total_spread_cost, 4),
            "total_slippage_cost": round(s.total_slippage_cost, 4),
            "total_market_impact_cost": round(s.total_market_impact_cost, 4),
            "total_queue_delay_ms": round(s.total_queue_delay_ms, 2),
            "rejection_rate": (
                round(s.rejected_count / s.total_orders, 4)
                if s.total_orders
                else 0.0
            ),
            "partial_fill_rate": (
                round(s.partial_fill_count / s.total_orders, 4)
                if s.total_orders
                else 0.0
            ),
        }

    # ── Helpers ──────────────────────────────────────────────────

    def _record_rejected(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        price: float,
    ) -> Order:
        """Create a rejected ``Order`` without touching the paper engine."""
        order = Order(
            id=f"sim-rej-{self._stats.rejected_count}",
            symbol=symbol.upper(),
            side=side,
            qty=qty,
            status=OrderStatus.REJECTED,
            filled_qty=0.0,
            filled_price=None,
            submitted_at=datetime.utcnow().isoformat(),
            filled_at=None,
            metadata={"rejection_reason": "simulated_random_rejection"},
        )
        # Append to the paper engine's order list so callers can see it
        # via get_orders().
        self._engine._orders.append(order)
        return order
