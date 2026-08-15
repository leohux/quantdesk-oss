"""
Account-level gross / cash hard cap (cross-process).
====================================================
Per-strategy caps cannot see another runner's book. This guard reads broker
equity, cash and position market value and rejects a new BUY when the order
would push:

    gross / equity  > MAX_GROSS_EXPOSURE_PCT / 100
    cash / equity   < MIN_CASH_PCT / 100

Default 150 / -50 matches a 1.5x book (cash floor is 100 − gross cap when
MIN_CASH_PCT is unset). Exits must not call this.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AccountCapResult:
    allowed: bool
    reason: str | None
    gross_pct: float | None
    cash_pct: float | None
    gross_cap_pct: float
    min_cash_pct: float
    equity: float
    cash: float
    gross: float
    proposed_notional: float


def max_gross_pct() -> float:
    return float(os.environ.get("MAX_GROSS_EXPOSURE_PCT", "150"))


def min_cash_pct() -> float:
    raw = os.environ.get("MIN_CASH_PCT")
    if raw is not None and str(raw).strip() != "":
        return float(raw)
    return 100.0 - max_gross_pct()


def check_account_buy(client: Any, proposed_notional: float) -> AccountCapResult:
    """Return whether a BUY of ``proposed_notional`` stays under gross/cash caps."""
    gross_cap = max_gross_pct()
    cash_floor = min_cash_pct()
    proposed = float(proposed_notional or 0.0)

    try:
        account = client.account()
        equity = float(account.get("equity") or 0)
        cash = float(account.get("cash") or 0)
    except Exception:
        logger.error("account_guard: account() failed", exc_info=True)
        return AccountCapResult(
            allowed=False,
            reason="account_fetch_failed",
            gross_pct=None,
            cash_pct=None,
            gross_cap_pct=gross_cap,
            min_cash_pct=cash_floor,
            equity=0.0,
            cash=0.0,
            gross=0.0,
            proposed_notional=proposed,
        )
    if equity <= 0:
        return AccountCapResult(
            allowed=False,
            reason="equity_non_positive",
            gross_pct=None,
            cash_pct=None,
            gross_cap_pct=gross_cap,
            min_cash_pct=cash_floor,
            equity=equity,
            cash=cash,
            gross=0.0,
            proposed_notional=proposed,
        )

    try:
        positions = client.positions()
    except Exception:
        logger.error("account_guard: positions() failed", exc_info=True)
        return AccountCapResult(
            allowed=False,
            reason="positions_fetch_failed",
            gross_pct=None,
            cash_pct=None,
            gross_cap_pct=gross_cap,
            min_cash_pct=cash_floor,
            equity=equity,
            cash=cash,
            gross=0.0,
            proposed_notional=proposed,
        )

    gross = sum(abs(float(p.get("market_value") or 0)) for p in (positions or []))
    after_gross = gross + proposed
    after_cash = cash - proposed
    after_gross_pct = after_gross / equity * 100.0
    after_cash_pct = after_cash / equity * 100.0

    if after_gross_pct > gross_cap:
        return AccountCapResult(
            allowed=False,
            reason=(
                f"gross_exposure {after_gross_pct:.1f}% > {gross_cap:.0f}%"
            ),
            gross_pct=after_gross_pct,
            cash_pct=after_cash_pct,
            gross_cap_pct=gross_cap,
            min_cash_pct=cash_floor,
            equity=equity,
            cash=cash,
            gross=gross,
            proposed_notional=proposed,
        )
    if after_cash_pct < cash_floor:
        return AccountCapResult(
            allowed=False,
            reason=(
                f"cash {after_cash_pct:.1f}% < floor {cash_floor:.0f}%"
            ),
            gross_pct=after_gross_pct,
            cash_pct=after_cash_pct,
            gross_cap_pct=gross_cap,
            min_cash_pct=cash_floor,
            equity=equity,
            cash=cash,
            gross=gross,
            proposed_notional=proposed,
        )

    return AccountCapResult(
        allowed=True,
        reason=None,
        gross_pct=after_gross_pct,
        cash_pct=after_cash_pct,
        gross_cap_pct=gross_cap,
        min_cash_pct=cash_floor,
        equity=equity,
        cash=cash,
        gross=gross,
        proposed_notional=proposed,
    )
