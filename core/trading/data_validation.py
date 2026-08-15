"""Data validation module for OHLCV market data quality checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Result of a full OHLCV validation pass."""
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


class GapType(str, Enum):
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    MARKET_CLOSURE = "market_closure"
    DATA_GAP = "data_gap"


@dataclass
class Gap:
    """A gap detected in the time series."""
    start: datetime
    end: datetime
    gap_days: int
    gap_type: GapType


@dataclass
class Spike:
    """A detected price spike."""
    timestamp: datetime
    expected_price: float
    actual_price: float
    pct_change: float


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class DataValidator:
    """Validates OHLCV DataFrames for data quality issues."""

    # Major US market holidays (month, day) – static approximation
    _US_HOLIDAYS_STATIC: set[tuple[int, int]] = {
        (1, 1),   # New Year's Day
        (7, 4),   # Independence Day
        (12, 25), # Christmas
    }

    def __init__(
        self,
        price_spike_threshold_pct: float = 10.0,
        max_gap_days: int = 3,
    ) -> None:
        self.price_spike_threshold_pct = price_spike_threshold_pct
        self.max_gap_days = max_gap_days

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_ohlcv(self, df: pd.DataFrame, symbol: str) -> ValidationResult:
        """Run all checks on *df* and return a consolidated result."""
        result = ValidationResult(
            stats={
                "symbol": symbol,
                "total_bars": len(df),
                "date_range": None,
                "gaps_found": 0,
                "spikes_found": 0,
                "duplicates_found": 0,
            }
        )

        if df.empty:
            result.add_error(f"[{symbol}] DataFrame is empty")
            return result

        # Ensure datetime index
        idx = self._ensure_datetime_index(df)
        result.stats["date_range"] = {
            "start": str(idx.min()),
            "end": str(idx.max()),
        }

        # 1. NaN / null detection ------------------------------------------------
        null_counts = df.isnull().sum()
        total_nulls = int(null_counts.sum())
        if total_nulls > 0:
            cols_with_nulls = [c for c, n in null_counts.items() if n > 0]
            result.add_error(
                f"[{symbol}] {total_nulls} null values in columns: {cols_with_nulls}"
            )

        # 2. Duplicate timestamps -------------------------------------------------
        dupes = self.detect_duplicate_ticks(df)
        result.stats["duplicates_found"] = len(dupes)
        if dupes:
            result.add_error(
                f"[{symbol}] {len(dupes)} duplicate timestamps detected"
            )

        # 3. OHLC consistency ----------------------------------------------------
        o, h, l, c = "Open", "High", "Low", "Close"
        if all(col in df.columns for col in (o, h, l, c)):
            bad_high = df[h] < df[[o, c]].max(axis=1)
            bad_low = df[l] > df[[o, c]].min(axis=1)
            n_bad = int(bad_high.sum() + bad_low.sum())
            if n_bad > 0:
                result.add_error(
                    f"[{symbol}] {n_bad} bars with OHLC inconsistency "
                    f"(High < max(Open,Close) or Low > min(Open,Close))"
                )

        # 4. Zero volume ----------------------------------------------------------
        if "Volume" in df.columns:
            zero_vol = int((df["Volume"] == 0).sum())
            if zero_vol > 0:
                result.add_warning(
                    f"[{symbol}] {zero_vol} bars with zero volume"
                )

        # 5. Gaps ----------------------------------------------------------------
        gaps = self.detect_gaps(df, max_gap_days=self.max_gap_days)
        data_gaps = [g for g in gaps if g.gap_type == GapType.DATA_GAP]
        result.stats["gaps_found"] = len(gaps)
        if data_gaps:
            result.add_error(
                f"[{symbol}] {len(data_gaps)} unexpected data gaps "
                f"(>{self.max_gap_days} trading days)"
            )

        # 6. Price spikes --------------------------------------------------------
        spikes = self.detect_price_spikes(df, self.price_spike_threshold_pct)
        result.stats["spikes_found"] = len(spikes)
        if spikes:
            result.add_warning(
                f"[{symbol}] {len(spikes)} price spikes "
                f"(>{self.price_spike_threshold_pct}%)"
            )

        return result

    def detect_gaps(
        self, df: pd.DataFrame, max_gap_days: int = 3
    ) -> list[Gap]:
        """Find and classify gaps in the time series."""
        idx = self._ensure_datetime_index(df)
        if len(idx) < 2:
            return []

        sorted_idx = idx.sort_values()
        gaps: list[Gap] = []

        for i in range(1, len(sorted_idx)):
            prev_dt = sorted_idx[i - 1]
            curr_dt = sorted_idx[i]
            delta = curr_dt - prev_dt
            gap_days = delta.days

            if gap_days <= 1:
                continue

            gap_type = self._classify_gap(prev_dt, curr_dt, max_gap_days)
            gaps.append(
                Gap(start=prev_dt, end=curr_dt, gap_days=gap_days, gap_type=gap_type)
            )

        return gaps

    def detect_duplicate_ticks(self, df: pd.DataFrame) -> list:
        """Return list of duplicate timestamp values."""
        idx = self._ensure_datetime_index(df)
        dup_mask = idx.duplicated(keep=False)
        return sorted(idx[dup_mask].unique().tolist())

    def detect_price_spikes(
        self, df: pd.DataFrame, threshold_pct: float = 10.0
    ) -> list[Spike]:
        """Detect bars where absolute return exceeds *threshold_pct*."""
        if "Close" not in df.columns or len(df) < 2:
            return []

        idx = self._ensure_datetime_index(df)
        close = df["Close"].values
        spikes: list[Spike] = []

        for i in range(1, len(close)):
            prev = close[i - 1]
            curr = close[i]
            if prev == 0:
                continue
            pct = abs((curr - prev) / prev) * 100.0
            if pct > threshold_pct:
                spikes.append(
                    Spike(
                        timestamp=idx[i],
                        expected_price=float(prev),
                        actual_price=float(curr),
                        pct_change=round(pct, 4),
                    )
                )

        return spikes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_datetime_index(df: pd.DataFrame) -> pd.DatetimeIndex:
        """Return a DatetimeIndex, converting if necessary."""
        if isinstance(df.index, pd.DatetimeIndex):
            return df.index
        # Try common column names
        for col in ("timestamp", "date", "datetime", "time"):
            if col in df.columns:
                return pd.DatetimeIndex(pd.to_datetime(df[col]))
        # Fall back to first column
        return pd.DatetimeIndex(pd.to_datetime(df.iloc[:, 0]))

    def _classify_gap(
        self,
        prev_dt: datetime,
        curr_dt: datetime,
        max_gap_days: int,
    ) -> GapType:
        """Classify a gap between two timestamps."""
        # Weekend check: Friday → Monday (2–3 days)
        if prev_dt.weekday() == 4 and curr_dt.weekday() == 0 and (curr_dt - prev_dt).days <= 3:
            return GapType.WEEKEND

        # Static holiday check (single-day holidays adjacent to weekends)
        gap_days = (curr_dt - prev_dt).days
        for offset_day in range(1, gap_days):
            check = prev_dt + timedelta(days=offset_day)
            if (check.month, check.day) in self._US_HOLIDAYS_STATIC:
                return GapType.HOLIDAY

        # Known market closures (Christmas Eve half-day range, etc.) – short gaps
        if gap_days <= max_gap_days:
            return GapType.MARKET_CLOSURE

        # Anything longer is a genuine data gap
        return GapType.DATA_GAP
