"""
Cross-process combined position cap (broker-layer).
===================================================
phase6_runner, quantdesk-intraday, and quantdesk-news-trader each open their
own AlpacaPaperClient against the same paper account. Per-process caps
(SINGLE_NAME_CAP_PCT / MAX_POSITION_PCT / PCT_EQUITY) cannot see another
process's open buys. This guard reads broker positions + open buy orders and
rejects a new BUY when:

    (position_mv + open_buy_notional + proposed_notional) / equity
        > COMBINED_POSITION_CAP_PCT / 100

Skip-only: never resize. Callers insert the check after a successful sleeve
claim and before market_order / bracket_order, and must release the claim on
reject.

Known residual race: two processes can both read stale open-order snapshots
in the gap between this check and submit_order. We accept that and rely on
sleeve reconcile — no distributed lock.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Status tokens seen on Alpaca open buys (normalized lowercase by the client).
_OPEN_BUY_STATUSES = frozenset(
    {
        "open",
        "new",
        "accepted",
        "partially_filled",
        "pending_new",
        "held",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "calculated",
    }
)


@dataclass
class CombinedCapResult:
    allowed: bool
    reason: str | None
    combined_pct: float | None
    cap_pct: float
    position_mv: float
    open_buy_notional: float
    proposed_notional: float
    equity: float


def combined_cap_pct() -> float:
    return float(os.environ.get("COMBINED_POSITION_CAP_PCT", "10"))


def check_combined_buy(
    client: Any,
    symbol: str,
    proposed_notional: float,
    *,
    ref_price: float | None = None,
) -> CombinedCapResult:
    """Return whether a BUY of ``proposed_notional`` is under the combined cap.

    Only for BUY paths. Sell legs / exits must not call this.
    """
    cap_pct = combined_cap_pct()
    sym = str(symbol or "").upper()
    proposed = float(proposed_notional or 0.0)

    try:
        account = client.account()
        equity = float(account.get("equity", 0) or 0)
    except Exception:
        logger.error(
            "combined_position_guard: account() failed for %s", sym, exc_info=True
        )
        return CombinedCapResult(
            allowed=False,
            reason="account_fetch_failed",
            combined_pct=None,
            cap_pct=cap_pct,
            position_mv=0.0,
            open_buy_notional=0.0,
            proposed_notional=proposed,
            equity=0.0,
        )
    if equity <= 0:
        logger.error("combined_position_guard: equity<=0 for %s", sym)
        return CombinedCapResult(
            allowed=False,
            reason="equity_non_positive",
            combined_pct=None,
            cap_pct=cap_pct,
            position_mv=0.0,
            open_buy_notional=0.0,
            proposed_notional=proposed,
            equity=equity,
        )

    try:
        positions = client.positions()
    except Exception:
        logger.error(
            "combined_position_guard: positions() failed for %s", sym, exc_info=True
        )
        return CombinedCapResult(
            allowed=False,
            reason="positions_fetch_failed",
            combined_pct=None,
            cap_pct=cap_pct,
            position_mv=0.0,
            open_buy_notional=0.0,
            proposed_notional=proposed,
            equity=equity,
        )

    position_mv = 0.0
    for p in positions or []:
        if str(p.get("symbol") or "").upper() != sym:
            continue
        position_mv = abs(float(p.get("market_value", 0) or 0))
        break

    try:
        # Prefer broker "open" filter; status allowlist below is defensive.
        orders = client.orders(status="open", limit=200)
    except TypeError:
        # Fake / older clients may only accept the no-arg form used in tests.
        try:
            orders = client.orders()
        except Exception:
            logger.error(
                "combined_position_guard: orders() failed for %s", sym, exc_info=True
            )
            return CombinedCapResult(
                allowed=False,
                reason="orders_fetch_failed",
                combined_pct=None,
                cap_pct=cap_pct,
                position_mv=position_mv,
                open_buy_notional=0.0,
                proposed_notional=proposed,
                equity=equity,
            )
    except Exception:
        logger.error(
            "combined_position_guard: orders() failed for %s", sym, exc_info=True
        )
        return CombinedCapResult(
            allowed=False,
            reason="orders_fetch_failed",
            combined_pct=None,
            cap_pct=cap_pct,
            position_mv=position_mv,
            open_buy_notional=0.0,
            proposed_notional=proposed,
            equity=equity,
        )

    open_buy_notional = 0.0
    for o in orders or []:
        if str(o.get("symbol") or "").upper() != sym:
            continue
        if str(o.get("side") or "").lower() != "buy":
            continue
        status = str(o.get("status") or "").lower()
        if status and status not in _OPEN_BUY_STATUSES:
            continue
        qty = float(o.get("qty", 0) or 0)
        filled_qty = float(o.get("filled_qty", 0) or 0)
        remaining_qty = qty - filled_qty
        if remaining_qty <= 0:
            continue
        price = o.get("limit_price")
        if price is None:
            price = ref_price
        if price is None:
            logger.warning(
                "combined_position_guard: open buy for %s missing "
                "limit_price and ref_price, rejecting",
                sym,
            )
            return CombinedCapResult(
                allowed=False,
                reason="open_order_missing_price",
                combined_pct=None,
                cap_pct=cap_pct,
                position_mv=position_mv,
                open_buy_notional=open_buy_notional,
                proposed_notional=proposed,
                equity=equity,
            )
        open_buy_notional += remaining_qty * float(price)

    combined = position_mv + open_buy_notional + proposed
    combined_pct = combined / equity * 100.0

    if combined_pct > cap_pct:
        return CombinedCapResult(
            allowed=False,
            reason=(
                f"combined_position_cap_exceeded: "
                f"{combined_pct:.2f}% > {cap_pct:.2f}%"
            ),
            combined_pct=combined_pct,
            cap_pct=cap_pct,
            position_mv=position_mv,
            open_buy_notional=open_buy_notional,
            proposed_notional=proposed,
            equity=equity,
        )

    return CombinedCapResult(
        allowed=True,
        reason=None,
        combined_pct=combined_pct,
        cap_pct=cap_pct,
        position_mv=position_mv,
        open_buy_notional=open_buy_notional,
        proposed_notional=proposed,
        equity=equity,
    )
