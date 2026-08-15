from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.portfolio.account_guard import (
    check_account_buy,
    max_gross_pct,
    min_cash_pct,
)


class FakeClient:
    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        cash: float = 100_000.0,
        positions: list | None = None,
        account_exc: Exception | None = None,
        positions_exc: Exception | None = None,
    ) -> None:
        self._equity = equity
        self._cash = cash
        self._positions = positions or []
        self._account_exc = account_exc
        self._positions_exc = positions_exc

    def account(self) -> dict:
        if self._account_exc:
            raise self._account_exc
        return {"equity": self._equity, "cash": self._cash, "buying_power": self._cash}

    def positions(self) -> list:
        if self._positions_exc:
            raise self._positions_exc
        return list(self._positions)


class AccountGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["MAX_GROSS_EXPOSURE_PCT"] = "150"
        os.environ.pop("MIN_CASH_PCT", None)

    def tearDown(self) -> None:
        os.environ.pop("MAX_GROSS_EXPOSURE_PCT", None)
        os.environ.pop("MIN_CASH_PCT", None)

    def test_defaults_match_1_5x(self) -> None:
        self.assertEqual(max_gross_pct(), 150.0)
        self.assertEqual(min_cash_pct(), -50.0)

    def test_empty_book_allows(self) -> None:
        r = check_account_buy(FakeClient(), 10_000.0)
        self.assertTrue(r.allowed)
        self.assertAlmostEqual(r.gross_pct or 0, 10.0, places=4)

    def test_blocks_gross_over_cap(self) -> None:
        client = FakeClient(
            cash=-40_000.0,
            positions=[{"symbol": "AAPL", "market_value": 140_000.0}],
        )
        r = check_account_buy(client, 15_000.0)
        self.assertFalse(r.allowed)
        self.assertIn("gross_exposure", r.reason or "")

    def test_blocks_cash_below_floor(self) -> None:
        os.environ["MIN_CASH_PCT"] = "-20"
        client = FakeClient(
            cash=-15_000.0,
            positions=[{"symbol": "AAPL", "market_value": 115_000.0}],
        )
        r = check_account_buy(client, 10_000.0)
        self.assertFalse(r.allowed)
        self.assertIn("cash", r.reason or "")

    def test_fail_closed_on_account_error(self) -> None:
        r = check_account_buy(FakeClient(account_exc=RuntimeError("down")), 1_000.0)
        self.assertFalse(r.allowed)
        self.assertEqual(r.reason, "account_fetch_failed")


if __name__ == "__main__":
    unittest.main()
