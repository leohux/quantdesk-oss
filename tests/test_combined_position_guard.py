from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.portfolio.combined_position_guard import (
    check_combined_buy,
    combined_cap_pct,
)


class FakeClient:
    def __init__(
        self,
        *,
        equity: float = 100_000.0,
        positions: list | None = None,
        orders: list | None = None,
        account_exc: Exception | None = None,
        positions_exc: Exception | None = None,
        orders_exc: Exception | None = None,
    ) -> None:
        self._equity = equity
        self._positions = positions or []
        self._orders = orders or []
        self._account_exc = account_exc
        self._positions_exc = positions_exc
        self._orders_exc = orders_exc

    def account(self) -> dict:
        if self._account_exc:
            raise self._account_exc
        return {"equity": self._equity, "buying_power": self._equity, "cash": self._equity}

    def positions(self) -> list:
        if self._positions_exc:
            raise self._positions_exc
        return list(self._positions)

    def orders(self, status: str = "all", limit: int = 50, **kwargs) -> list:
        if self._orders_exc:
            raise self._orders_exc
        if status.lower() == "open":
            openish = {
                "open",
                "new",
                "accepted",
                "partially_filled",
                "pending_new",
                "held",
            }
            return [o for o in self._orders if str(o.get("status", "")).lower() in openish]
        return list(self._orders)


class CombinedPositionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["COMBINED_POSITION_CAP_PCT"] = "10"

    def tearDown(self) -> None:
        os.environ.pop("COMBINED_POSITION_CAP_PCT", None)

    def test_combined_cap_pct_default_and_env(self) -> None:
        self.assertEqual(combined_cap_pct(), 10.0)
        os.environ["COMBINED_POSITION_CAP_PCT"] = "12"
        self.assertEqual(combined_cap_pct(), 12.0)

    def test_empty_book_allows(self) -> None:
        client = FakeClient()
        # 5% of equity
        r = check_combined_buy(client, "AAPL", 5_000.0, ref_price=100.0)
        self.assertTrue(r.allowed)
        self.assertIsNone(r.reason)
        self.assertAlmostEqual(r.combined_pct or 0, 5.0, places=4)

    def test_under_cap_with_position_and_open_buy(self) -> None:
        client = FakeClient(
            positions=[{"symbol": "MSFT", "market_value": 4_000.0, "side": "long"}],
            orders=[
                {
                    "symbol": "MSFT",
                    "side": "buy",
                    "status": "accepted",
                    "qty": 20.0,
                    "filled_qty": 0.0,
                    "limit_price": 100.0,  # 2_000
                }
            ],
        )
        # 4% + 2% + 3% = 9%
        r = check_combined_buy(client, "MSFT", 3_000.0, ref_price=100.0)
        self.assertTrue(r.allowed)
        self.assertAlmostEqual(r.combined_pct or 0, 9.0, places=4)

    def test_over_cap_rejected(self) -> None:
        client = FakeClient(
            positions=[{"symbol": "NVDA", "market_value": 5_000.0}],
            orders=[
                {
                    "symbol": "NVDA",
                    "side": "buy",
                    "status": "new",
                    "qty": 30.0,
                    "filled_qty": 0.0,
                    "limit_price": 100.0,  # 3_000
                }
            ],
        )
        # 5% + 3% + 4% = 12% > 10%
        r = check_combined_buy(client, "NVDA", 4_000.0, ref_price=100.0)
        self.assertFalse(r.allowed)
        self.assertIsNotNone(r.reason)
        assert r.reason is not None
        self.assertIn("combined_position_cap_exceeded", r.reason)
        self.assertIn("10.00%", r.reason)
        self.assertIn("12.00%", r.reason)

    def test_account_failure_rejects(self) -> None:
        client = FakeClient(account_exc=RuntimeError("boom"))
        r = check_combined_buy(client, "AAPL", 1_000.0, ref_price=10.0)
        self.assertFalse(r.allowed)
        self.assertEqual(r.reason, "account_fetch_failed")

    def test_zero_equity_rejects(self) -> None:
        client = FakeClient(equity=0.0)
        r = check_combined_buy(client, "AAPL", 1_000.0, ref_price=10.0)
        self.assertFalse(r.allowed)
        self.assertEqual(r.reason, "equity_non_positive")

    def test_sell_open_orders_ignored(self) -> None:
        client = FakeClient(
            positions=[{"symbol": "AAPL", "market_value": 2_000.0}],
            orders=[
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "status": "accepted",
                    "qty": 50.0,
                    "filled_qty": 0.0,
                    "stop_price": 90.0,  # SL leg — must not count
                },
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "status": "accepted",
                    "qty": 50.0,
                    "filled_qty": 0.0,
                    "limit_price": 120.0,  # TP leg
                },
            ],
        )
        # 2% + 0 open buys + 5% = 7%
        r = check_combined_buy(client, "AAPL", 5_000.0, ref_price=100.0)
        self.assertTrue(r.allowed)
        self.assertAlmostEqual(r.open_buy_notional, 0.0)
        self.assertAlmostEqual(r.combined_pct or 0, 7.0, places=4)

    def test_partial_fill_uses_remaining_qty(self) -> None:
        client = FakeClient(
            orders=[
                {
                    "symbol": "SOFI",
                    "side": "buy",
                    "status": "partially_filled",
                    "qty": 100.0,
                    "filled_qty": 60.0,  # remaining 40
                    "limit_price": 10.0,  # 400 = 0.4%
                }
            ]
        )
        r = check_combined_buy(client, "SOFI", 500.0, ref_price=10.0)
        self.assertTrue(r.allowed)
        self.assertAlmostEqual(r.open_buy_notional, 400.0)
        self.assertAlmostEqual(r.combined_pct or 0, 0.9, places=4)

    def test_multiple_open_buys_are_summed(self) -> None:
        client = FakeClient(
            orders=[
                {
                    "symbol": "PLTR",
                    "side": "buy",
                    "status": "accepted",
                    "qty": 30.0,
                    "filled_qty": 0.0,
                    "limit_price": 100.0,  # 3_000 = 3%
                },
                {
                    "symbol": "PLTR",
                    "side": "buy",
                    "status": "new",
                    "qty": 40.0,
                    "filled_qty": 0.0,
                    "limit_price": 100.0,  # 4_000 = 4%
                },
            ]
        )
        # 0 + 3% + 4% + 4% = 11% > 10%
        r = check_combined_buy(client, "PLTR", 4_000.0, ref_price=100.0)
        self.assertFalse(r.allowed)
        self.assertAlmostEqual(r.open_buy_notional, 7_000.0)
        self.assertAlmostEqual(r.combined_pct or 0, 11.0, places=4)
        assert r.reason is not None
        self.assertIn("combined_position_cap_exceeded", r.reason)

    def test_missing_limit_and_ref_price_rejects(self) -> None:
        client = FakeClient(
            orders=[
                {
                    "symbol": "AMD",
                    "side": "buy",
                    "status": "accepted",
                    "qty": 10.0,
                    "filled_qty": 0.0,
                    "limit_price": None,
                }
            ]
        )
        r = check_combined_buy(client, "AMD", 1_000.0, ref_price=None)
        self.assertFalse(r.allowed)
        self.assertEqual(r.reason, "open_order_missing_price")

    def test_missing_limit_uses_ref_price(self) -> None:
        client = FakeClient(
            orders=[
                {
                    "symbol": "AMD",
                    "side": "buy",
                    "status": "accepted",
                    "qty": 10.0,
                    "filled_qty": 0.0,
                    "limit_price": None,
                }
            ]
        )
        # open 10 * 50 = 500; proposed 500 → 1%
        r = check_combined_buy(client, "AMD", 500.0, ref_price=50.0)
        self.assertTrue(r.allowed)
        self.assertAlmostEqual(r.open_buy_notional, 500.0)


if __name__ == "__main__":
    unittest.main()
