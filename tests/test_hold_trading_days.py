from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config.calendar import USMarketCalendar, hold_trading_days

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh=10, mm=0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


class HoldTradingDaysTest(unittest.TestCase):
    def test_weekday_stretch_matches_old_calendar(self):
        # Mon entry → Thu morning is 3 sessions (Tue, Wed, Thu)
        opened = _et(2026, 8, 10)  # Monday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 10)), 0)
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 11)), 1)
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 12)), 2)
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 13)), 3)

    def test_thursday_entry_skips_weekend(self):
        opened = _et(2026, 8, 13)  # Thursday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 14)), 1)  # Friday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 15)), 1)  # Saturday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 16)), 1)  # Sunday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 17)), 2)  # Monday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 18)), 3)  # Tuesday
        # Old calendar bug: Monday would have been age=4 and already flattened

    def test_friday_entry_skips_weekend(self):
        opened = _et(2026, 8, 14)  # Friday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 17)), 1)  # Monday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 18)), 2)  # Tuesday
        self.assertEqual(hold_trading_days(opened, _et(2026, 8, 19)), 3)  # Wednesday

    def test_independence_day_2026_observed_friday(self):
        # 2026-07-03 Friday is observed Independence Day
        opened = _et(2026, 7, 2)  # Thursday
        self.assertEqual(hold_trading_days(opened, _et(2026, 7, 3)), 0)  # holiday
        self.assertEqual(hold_trading_days(opened, _et(2026, 7, 6)), 1)  # Monday

    def test_sessions_since_helper(self):
        cal = USMarketCalendar()
        self.assertEqual(
            cal.sessions_since(dt.date(2026, 8, 13), dt.date(2026, 8, 17)),
            2,
        )


if __name__ == "__main__":
    unittest.main()
