from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from core.auth import create_access_token
from core.execution.base import OrderSide
from core.execution.ibkr import IBKRExecutionEngine
from core.trading.live_guard import LiveTradingGuard
from core.trading.live_oms_service import LiveOMSService


class LiveLockedTests(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()
        self._tmp = tempfile.mkdtemp(prefix="quantdesk-test-")
        os.environ["LIVE_AUDIT_LOG_PATH"] = os.path.join(self._tmp, "live_audit.jsonl")
        os.environ["LIVE_OMS_STATE_PATH"] = os.path.join(self._tmp, "live_oms_state.json")
        os.environ["IBKR_GATEWAY_MODE"] = "mock"
        os.environ["IBKR_TRADING_MODE"] = "paper"
        os.environ["IBKR_ALLOWED_ACCOUNTS"] = "DU-QUANTDESK"
        os.environ["IBKR_ACCOUNT"] = "DU-QUANTDESK"
        os.environ["LIVE_ALLOWED_SYMBOLS"] = "AAPL,NVDA,MSFT"
        os.environ["LIVE_ALLOWED_SIDES"] = "buy,sell"
        os.environ["LIVE_TRADING_ENABLED"] = "false"
        os.environ["LIVE_EXECUTION_ARMED"] = "false"
        os.environ["LIVE_MAX_ORDER_VALUE_USD"] = "1000"
        self.engine = IBKRExecutionEngine(gateway_mode="mock")
        self.guard = LiveTradingGuard(self.engine)

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mock_engine_connected(self):
        self.assertTrue(self.engine.is_connected())
        account = self.engine.get_account()
        self.assertEqual(account.mode, "paper")
        self.assertEqual(account.broker, "ibkr")

    def test_preview_respects_symbol_and_order_limits(self):
        preview = self.guard.preview_order(
            symbol="AAPL",
            side="buy",
            qty=5,
            price=150,
        )
        self.assertTrue(preview.allowed)
        blocked = self.guard.preview_order(
            symbol="TSLA",
            side="buy",
            qty=20,
            price=200,
        )
        self.assertFalse(blocked.allowed)
        failed = {c.name for c in blocked.checks if not c.passed}
        self.assertIn("allowed_symbol", failed)
        self.assertIn("max_order_value", failed)

    def test_submit_fails_closed_when_live_lock_off(self):
        preview = self.guard.submit_market_order(
            symbol="AAPL",
            side="buy",
            qty=1,
            price=100,
            client_order_id="test-1",
        )
        self.assertFalse(preview.allowed)
        self.assertFalse(preview.submitted)

    def test_live_oms_reconcile_mock(self):
        oms = LiveOMSService(state_path="/tmp/live_oms_test.json")
        result = oms.reconcile(self.engine)
        self.assertIn("discrepancies", result)
        self.assertFalse(result["blocked_on_discrepancy"])

    def test_live_api_submit_rejected_when_locked(self):
        import api.main as api_main

        get_settings.cache_clear()
        token = create_access_token({"sub": "admin", "role": "admin"})
        client = TestClient(api_main.app)
        with patch.object(api_main, "_live_engine", return_value=self.engine), patch.object(
            api_main, "_live_guard", return_value=self.guard
        ):
            res = client.post(
                "/api/live/orders",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "qty": 1,
                    "side": "buy",
                    "price": 100,
                    "arming_token": "wrong",
                },
            )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
