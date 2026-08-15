"""
Wait for the broker to say what actually happened.
==================================================
Alpaca paper fills market orders instantly and completely, so every runner was
written to treat "order submitted" as "shares acquired at the price I saw on my
last quote". It books the sleeve claim, the journal row and the buying-power
deduction straight off the signal price.

None of that holds on a real broker. Orders queue, fill partially, fill at a
different price, or get rejected outright — and every one of those leaves the
ledger describing a position that does not exist.

This asks the broker what the order did before anyone writes it down. Callers
get the *actual* filled quantity and average price, and a clear answer to the
only three questions that matter:

    fill.filled       -> something was bought/sold; book exactly this much
    fill.dead         -> nothing filled and nothing ever will; drop the claim
    fill.pending      -> still resting; keep the claim as a lock, book nothing

A pending entry is normal for the MiMo dip limits, which are meant to sit and
wait. Those are protected by `open_order_symbols` during reconcile, and if they
never fill the claim ages out and the journal row is retired as stale.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any  # noqa: F401 — used in annotations

logger = logging.getLogger(__name__)

# Statuses the broker will not move away from on its own.
TERMINAL = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
    "stopped",
    "suspended",
    "replaced",
}

DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_POLL_SEC = 1.0


@dataclass
class FillResult:
    order_id: str
    status: str
    filled_qty: float
    avg_price: float | None
    terminal: bool
    requested_qty: float = 0.0
    error: str | None = None

    @property
    def filled(self) -> bool:
        return self.filled_qty > 0

    @property
    def partial(self) -> bool:
        return 0 < self.filled_qty < self.requested_qty - 1e-9

    @property
    def dead(self) -> bool:
        """Terminal with nothing filled — the claim must be given back."""
        return self.terminal and self.filled_qty <= 0

    @property
    def pending(self) -> bool:
        return not self.terminal and self.filled_qty <= 0

    def price_or(self, fallback: float) -> float:
        """Fill price when the broker gave one, else the caller's estimate."""
        return float(self.avg_price) if self.avg_price else float(fallback)

    def describe(self) -> str:
        if self.error:
            return f"fill unknown ({self.error})"
        if self.dead:
            return f"no fill ({self.status})"
        if self.pending:
            return f"resting ({self.status})"
        px = f"@ ${self.avg_price:.2f}" if self.avg_price else "@ unknown price"
        tag = " PARTIAL" if self.partial else ""
        return f"filled {self.filled_qty:g}/{self.requested_qty:g} {px}{tag}"


def await_fill(
    client: Any,
    order: dict[str, Any],
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    poll_sec: float = DEFAULT_POLL_SEC,
) -> FillResult:
    """Poll one order until it stops moving or the timeout expires.

    Never raises: a broker lookup that fails returns a result carrying the
    submitted quantity, so the caller degrades to the old optimistic behaviour
    rather than losing track of an order that may well be live.
    """
    order_id = str(order.get("id") or "")
    requested = float(order.get("qty") or 0)
    status = str(order.get("status") or "submitted").lower()
    filled_qty = float(order.get("filled_qty") or 0)
    avg_price = order.get("filled_avg_price")

    if not order_id or order_id == "dry-run":
        return FillResult(order_id, status, filled_qty, avg_price, True, requested)

    deadline = time.time() + timeout_sec
    last_error: str | None = None
    while True:
        if status in TERMINAL:
            break
        if time.time() >= deadline:
            break
        time.sleep(poll_sec)
        try:
            snap = client.get_order(order_id)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("fill poll failed for %s: %s", order_id, exc)
            break
        status = str(snap.get("status") or status).lower()
        filled_qty = float(snap.get("filled_qty") or 0)
        avg_price = snap.get("filled_avg_price")
        requested = float(snap.get("qty") or requested)

    if last_error:
        # Unknown state. Assume the submission stands so the position is not
        # silently dropped, and let the next reconcile correct it.
        return FillResult(
            order_id, status, filled_qty, avg_price, False, requested, error=last_error
        )
    return FillResult(
        order_id, status, filled_qty, avg_price, status in TERMINAL, requested
    )


def reprice_protection(
    client: Any,
    symbol: str,
    qty: float,
    fill_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    *,
    signal_price: float | None = None,
    slip_tol: float = 0.002,
) -> str | None:
    """Re-arm SL/TP from the real fill when the signal price slipped.

    Bracket legs are sized at submit time off the scan quote. After a market
    fill at a different price those legs no longer match the intended risk.
    Returns a short description when legs were rewritten, else None.
    """
    if qty <= 0 or fill_price <= 0:
        return None
    if signal_price and abs(fill_price / signal_price - 1.0) < slip_tol:
        return None  # close enough; leave the original legs alone

    stop = round(fill_price * (1.0 + float(stop_loss_pct)), 2)
    take = round(fill_price * (1.0 + float(take_profit_pct)), 2)
    if not (0 < stop < fill_price < take):
        return None

    try:
        client.cancel_open_orders(symbol)
        time.sleep(0.5)
        client.oco_exit(symbol, int(qty), take, stop)
    except Exception as exc:
        logger.warning("reprice_protection %s failed: %s", symbol, exc)
        return f"reprice FAILED ({exc})"
    return f"repriced SL=${stop} TP=${take} from fill ${fill_price:.2f}"
