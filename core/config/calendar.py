"""US market trading calendar with hardcoded holidays 2024-2027."""

from __future__ import annotations

import datetime as dt
from typing import List, Optional, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Hardcoded observed US market holidays 2024-2027
# These already reflect the "if falls on weekend, observed on Friday/Monday"
# shift rules used by the NYSE/NASDAQ.
# ---------------------------------------------------------------------------
_OBSERVED_HOLIDAYS: Set[dt.date] = {
    # 2024
    dt.date(2024, 1, 1),   # New Year's Day (Mon)
    dt.date(2024, 1, 15),  # MLK Day
    dt.date(2024, 2, 19),  # Presidents' Day
    dt.date(2024, 3, 29),  # Good Friday
    dt.date(2024, 5, 27),  # Memorial Day
    dt.date(2024, 6, 19),  # Juneteenth
    dt.date(2024, 7, 4),   # Independence Day (Thu)
    dt.date(2024, 9, 2),   # Labor Day
    dt.date(2024, 11, 28), # Thanksgiving
    dt.date(2024, 12, 25), # Christmas (Wed)
    # 2025
    dt.date(2025, 1, 1),   # New Year's Day (Wed)
    dt.date(2025, 1, 20),  # MLK Day
    dt.date(2025, 2, 17),  # Presidents' Day
    dt.date(2025, 4, 18),  # Good Friday
    dt.date(2025, 5, 26),  # Memorial Day
    dt.date(2025, 6, 19),  # Juneteenth (Thu)
    dt.date(2025, 7, 4),   # Independence Day (Fri)
    dt.date(2025, 9, 1),   # Labor Day
    dt.date(2025, 11, 27), # Thanksgiving
    dt.date(2025, 12, 25), # Christmas (Thu)
    # 2026
    dt.date(2026, 1, 1),   # New Year's Day (Thu)
    dt.date(2026, 1, 19),  # MLK Day
    dt.date(2026, 2, 16),  # Presidents' Day
    dt.date(2026, 4, 3),   # Good Friday
    dt.date(2026, 5, 25),  # Memorial Day
    dt.date(2026, 6, 19),  # Juneteenth (Fri)
    dt.date(2026, 7, 3),   # Independence Day (Fri, observed from Sat 7/4)
    dt.date(2026, 9, 7),   # Labor Day
    dt.date(2026, 11, 26), # Thanksgiving
    dt.date(2026, 12, 25), # Christmas (Fri)
    # 2027
    dt.date(2027, 1, 1),   # New Year's Day (Fri)
    dt.date(2027, 1, 18),  # MLK Day
    dt.date(2027, 2, 15),  # Presidents' Day
    dt.date(2027, 3, 26),  # Good Friday
    dt.date(2027, 5, 31),  # Memorial Day
    dt.date(2027, 6, 18),  # Juneteenth (Fri, observed from Sat 6/19)
    dt.date(2027, 7, 5),   # Independence Day (Mon, observed from Sun 7/4)
    dt.date(2027, 9, 6),   # Labor Day
    dt.date(2027, 11, 25), # Thanksgiving
    dt.date(2027, 12, 24), # Christmas (Fri, observed from Sat 12/25)
}

# Early-close (half) days: day before July 4th when July 4th is not on a
# Fri/Mon, day after Thanksgiving, Christmas Eve, New Year's Eve.
# We store the *date* of the early close.
_EARLY_CLOSE_DATES: Set[dt.date] = {
    # 2024
    dt.date(2024, 7, 3),   # day before Independence Day (Thu)
    dt.date(2024, 11, 29), # day after Thanksgiving
    dt.date(2024, 12, 24), # Christmas Eve
    dt.date(2024, 12, 31), # New Year's Eve
    # 2025
    dt.date(2025, 7, 3),   # day before Independence Day (Thu)
    dt.date(2025, 11, 28), # day after Thanksgiving
    dt.date(2025, 12, 24), # Christmas Eve (Wed)
    dt.date(2025, 12, 31), # New Year's Eve (Wed)
    # 2026
    dt.date(2026, 11, 27), # day after Thanksgiving
    dt.date(2026, 12, 24), # Christmas Eve (Thu)
    dt.date(2026, 12, 31), # New Year's Eve (Thu)
    # 2027
    dt.date(2027, 11, 26), # day after Thanksgiving
    dt.date(2027, 12, 24), # Christmas Eve (Fri)
    dt.date(2027, 12, 31), # New Year's Eve (Fri)
}

_MARKET_OPEN = dt.time(9, 30, tzinfo=ET)
_MARKET_CLOSE = dt.time(16, 0, tzinfo=ET)
_EARLY_CLOSE_TIME = dt.time(13, 0, tzinfo=ET)


class USMarketCalendar:
    """NYSE/NASDAQ US equity trading calendar (2024-2027)."""

    # ---- public API --------------------------------------------------------

    def is_trading_day(self, date: dt.date) -> bool:
        """Return True if *date* is a trading day (weekday & not a holiday)."""
        return date.weekday() < 5 and date not in _OBSERVED_HOLIDAYS

    def next_trading_day(self, date: dt.date) -> dt.date:
        """Return the next trading day on or after *date*."""
        d = date
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def prev_trading_day(self, date: dt.date) -> dt.date:
        """Return the previous trading day on or before *date*."""
        d = date
        while not self.is_trading_day(d):
            d -= dt.timedelta(days=1)
        return d

    def is_market_open(self, datetime_utc_or_local: dt.datetime) -> bool:
        """Check whether the market is currently open.

        Accepts either a timezone-aware datetime or a naive datetime
        (treated as Eastern Time).
        """
        dt_et = self._to_et(datetime_utc_or_local)
        d = dt_et.date()
        if not self.is_trading_day(d):
            return False
        t = dt_et.timetz() if dt_et.tzinfo else dt_et.replace(tzinfo=ET).timetz()
        open_t, close_t = self.market_open_time(d), self.market_close_time(d)
        return open_t <= t < close_t

    def is_early_close(self, date: dt.date) -> bool:
        """Return True if *date* is a half-day (1 PM ET close)."""
        return date in _EARLY_CLOSE_DATES

    def market_open_time(self, date: dt.date) -> dt.time:
        """Return market open time for *date* (always 09:30 ET)."""
        return _MARKET_OPEN

    def market_close_time(self, date: dt.date) -> dt.time:
        """Return market close time for *date* (13:00 ET on half-days, else 16:00 ET)."""
        return _EARLY_CLOSE_TIME if self.is_early_close(date) else _MARKET_CLOSE

    def trading_days_between(
        self, start: dt.date, end: dt.date
    ) -> List[dt.date]:
        """Return a list of trading days in [start, end] inclusive."""
        days: List[dt.date] = []
        d = self.next_trading_day(start)
        while d <= end:
            days.append(d)
            d += dt.timedelta(days=1)
            while not self.is_trading_day(d):
                d += dt.timedelta(days=1)
        return days

    def sessions_since(self, opened: dt.date, now: dt.date) -> int:
        """Trading sessions after *opened* through *now* (entry date = 0).

        Weekends and observed NYSE holidays do not increment. Matches
        backtest ``hold_days += 1`` per daily bar, not calendar midnights.
        """
        if now <= opened:
            return 0
        return len(self.trading_days_between(opened + dt.timedelta(days=1), now))

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _to_et(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is not None:
            return value.astimezone(ET)
        return value.replace(tzinfo=ET)


_CAL = USMarketCalendar()


def hold_trading_days(
    opened_at: dt.datetime,
    now: dt.datetime | None = None,
) -> int:
    """ET trading sessions since entry date (entry session = 0)."""
    now = now or dt.datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    opened = (
        opened_at.astimezone(ET)
        if opened_at.tzinfo
        else opened_at.replace(tzinfo=dt.timezone.utc).astimezone(ET)
    )
    return _CAL.sessions_since(opened.date(), now.date())
