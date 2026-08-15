"""
Enhanced Replay Engine - Professional backtesting data replay with full playback controls.

Wraps HistoricalReplayStream with:
- Thread-safe playback controls (start/pause/resume/stop/step)
- Speed control (1x, 10x, 100x, instant)
- Seeking by date or percentage
- Per-symbol on_bar / on_tick callbacks
- Bar-by-bar mode for deterministic testing
- Auto (threaded) mode for real-time simulation
"""

import sys
import threading
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime

import numpy as np
import pandas as pd

# Ensure project root is importable
for p in ("/app", "/opt/quantdesk", "/opt/quantdesk/src"):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.trading.market_data import Bar, HistoricalReplayStream  # noqa: E402

logger = logging.getLogger(__name__)

# Type aliases
BarCallback = Callable[[str, Bar], None]
TickCallback = Callable[[str, dict], None]


class ReplayEngine:
    """
    Enhanced replay engine with professional playback controls.

    Supports two modes:
      - Bar-by-bar (manual): call step() to advance one bar at a time.
      - Auto (threaded): call start() to launch a background thread that
        replays bars at the configured speed.

    Thread-safe: all public control methods and properties can be called
    from any thread.
    """

    SPEED_INSTANT = 0  # replay as fast as possible

    def __init__(self, base_interval: float = 1.0):
        """
        Args:
            base_interval: Seconds per bar at 1x speed (default 1.0s).
        """
        self._lock = threading.RLock()

        # Data storage: symbol -> list of Bar objects (time-sorted)
        self._bars: Dict[str, List[Bar]] = {}
        self._tick_data: Dict[str, pd.DataFrame] = {}

        # Current indices per symbol
        self._indices: Dict[str, int] = {}

        # Unified timeline of (timestamp, symbol, bar_idx)
        self._timeline: List[Tuple[datetime, str, int]] = []
        self._timeline_pos: int = 0

        # Speed / timing
        self._speed: float = 1.0
        self._base_interval: float = base_interval

        # State flags
        self._running: bool = False
        self._paused: bool = False
        self._stopped: bool = False

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially

        # Callbacks
        self._on_bar_callbacks: List[BarCallback] = []
        self._on_tick_callbacks: List[TickCallback] = []

        # Step synchronisation (for bar-by-bar mode)
        self._step_event = threading.Event()
        self._step_count: int = 0

        # HistoricalReplayStream reference (optional wrapper)
        self._stream: Optional[HistoricalReplayStream] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_data(self, symbol: str, ohlcv_df: pd.DataFrame) -> None:
        """
        Load OHLCV bar data for *symbol*.

        *ohlcv_df* must have columns: open, high, low, close, volume
        and a DatetimeIndex (or a 'date'/'timestamp' column).
        """
        with self._lock:
            df = ohlcv_df.copy()

            # Normalise index
            if not isinstance(df.index, pd.DatetimeIndex):
                for col in ("date", "timestamp", "datetime", "time"):
                    if col in df.columns:
                        df = df.set_index(col)
                        break
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
            df = df.sort_index()

            # Normalise column names
            col_map = {}
            for c in df.columns:
                lc = c.lower().strip()
                if lc in ("open", "o"):
                    col_map[c] = "open"
                elif lc in ("high", "h"):
                    col_map[c] = "high"
                elif lc in ("low", "l"):
                    col_map[c] = "low"
                elif lc in ("close", "c", "adj_close", "adjusted_close"):
                    col_map[c] = "close"
                elif lc in ("volume", "vol", "v"):
                    col_map[c] = "volume"
            df = df.rename(columns=col_map)

            bars: List[Bar] = []
            for idx, row in df.iterrows():
                bar = Bar(
                    symbol=symbol,
                    timestamp=idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                )
                bars.append(bar)

            self._bars[symbol] = bars
            self._indices[symbol] = 0
            self._rebuild_timeline()
            logger.info("Loaded %d bars for %s", len(bars), symbol)

    def load_tick_data(self, symbol: str, tick_df: pd.DataFrame) -> None:
        """Load tick-level data for *symbol*. Stored as DataFrame for on_tick callbacks."""
        with self._lock:
            df = tick_df.copy()
            if not isinstance(df.index, pd.DatetimeIndex):
                for col in ("date", "timestamp", "datetime", "time"):
                    if col in df.columns:
                        df = df.set_index(col)
                        break
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            self._tick_data[symbol] = df
            logger.info("Loaded %d ticks for %s", len(df), symbol)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def on_bar(self, callback: BarCallback) -> None:
        """Register a callback(symbol, bar) invoked for each bar during replay."""
        self._on_bar_callbacks.append(callback)

    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback(symbol, tick_dict) invoked for each tick."""
        self._on_tick_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Speed control
    # ------------------------------------------------------------------

    def set_speed(self, multiplier: float) -> None:
        """
        Set replay speed.
          - 1   = real-time (1 second per base_interval bar)
          - 10  = 10x faster
          - 100 = 100x faster
          - 0   = instant (no delay)
        """
        with self._lock:
            if multiplier < 0:
                raise ValueError("Speed multiplier must be >= 0")
            self._speed = multiplier
            logger.info("Replay speed set to %sx", multiplier if multiplier > 0 else "instant")

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def start(self, background: bool = True) -> None:
        """
        Start replay.

        Args:
            background: If True (default), run in a background thread (auto mode).
                        If False, run synchronously (blocks until finished).
        """
        with self._lock:
            if self._running:
                logger.warning("Replay already running")
                return
            if not self._timeline:
                logger.warning("No data loaded – nothing to replay")
                return
            self._running = True
            self._paused = False
            self._stopped = False
            self._pause_event.set()

        if background:
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ReplayEngine")
            self._thread.start()
        else:
            self._run_loop()

    def pause(self) -> None:
        """Pause an in-progress replay."""
        with self._lock:
            if not self._running or self._paused:
                return
            self._paused = True
            self._pause_event.clear()
            logger.info("Replay paused at position %d", self._timeline_pos)

    def resume(self) -> None:
        """Resume a paused replay."""
        with self._lock:
            if not self._running or not self._paused:
                return
            self._paused = False
            self._pause_event.set()
            logger.info("Replay resumed")

    def stop(self) -> None:
        """Stop replay entirely."""
        with self._lock:
            self._stopped = True
            self._paused = False
            self._pause_event.set()  # unblock if paused
            logger.info("Replay stopped")

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
            self._thread = None

        with self._lock:
            self._running = False

    def step(self, n: int = 1) -> List[Tuple[str, Bar]]:
        """
        Advance *n* bars in bar-by-bar mode (no threading needed).

        Returns list of (symbol, Bar) tuples that were emitted.
        If already at the end, returns an empty list.
        """
        emitted: List[Tuple[str, Bar]] = []
        with self._lock:
            for _ in range(n):
                if self._timeline_pos >= len(self._timeline):
                    break
                ts, sym, bar_idx = self._timeline[self._timeline_pos]
                bar = self._bars[sym][bar_idx]
                self._indices[sym] = bar_idx + 1
                self._timeline_pos += 1
                emitted.append((sym, bar))
                # Fire callbacks
                for cb in self._on_bar_callbacks:
                    try:
                        cb(sym, bar)
                    except Exception:
                        logger.exception("on_bar callback error for %s", sym)
        return emitted

    # ------------------------------------------------------------------
    # Seeking
    # ------------------------------------------------------------------

    def seek_to(self, date: datetime) -> None:
        """Jump replay to the first bar at or after *date*."""
        with self._lock:
            target = pd.Timestamp(date)
            for i, (ts, sym, bar_idx) in enumerate(self._timeline):
                if pd.Timestamp(ts) >= target:
                    self._timeline_pos = i
                    # Update per-symbol indices to reflect position
                    self._rebuild_indices_to(i)
                    logger.info("Seek to %s → timeline position %d", target, i)
                    return
            logger.warning("seek_to: no bar found at or after %s", date)

    def seek_to_pct(self, pct: float) -> None:
        """Jump replay to *pct* percent (0–100) of the timeline."""
        with self._lock:
            if not self._timeline:
                return
            pct = max(0.0, min(100.0, pct))
            idx = int(len(self._timeline) * pct / 100.0)
            idx = min(idx, len(self._timeline) - 1)
            self._timeline_pos = idx
            self._rebuild_indices_to(idx)
            logger.info("Seek to %.1f%% → timeline position %d", pct, idx)

    # ------------------------------------------------------------------
    # Progress / state
    # ------------------------------------------------------------------

    def get_progress(self) -> Dict[str, Any]:
        """Return current replay progress."""
        with self._lock:
            total = len(self._timeline)
            if total == 0:
                return {"current_idx": 0, "total": 0, "pct": 0.0, "current_date": None}
            pos = min(self._timeline_pos, total - 1)
            ts = self._timeline[pos][0] if total > 0 else None
            return {
                "current_idx": pos,
                "total": total,
                "pct": round(pos / total * 100, 2) if total else 0.0,
                "current_date": ts,
            }

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running and not self._stopped

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._bars.keys())

    # ------------------------------------------------------------------
    # HistoricalReplayStream compatibility
    # ------------------------------------------------------------------

    def get_stream(self, symbol: str) -> HistoricalReplayStream:
        """
        Return a HistoricalReplayStream backed by the loaded data for *symbol*.
        Useful for code that expects the stream interface.
        """
        with self._lock:
            if symbol not in self._bars:
                raise KeyError(f"No data loaded for {symbol}")
            bars = self._bars[symbol]
            # Build a DataFrame compatible with HistoricalReplayStream.load_data
            rows = []
            index = []
            for b in bars:
                index.append(b.timestamp)
                rows.append({
                    "Open": b.open,
                    "High": b.high,
                    "Low": b.low,
                    "Close": b.close,
                    "Volume": b.volume,
                })
            df = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
            stream = HistoricalReplayStream()
            stream.load_data(symbol, df)
            return stream

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rebuild_timeline(self) -> None:
        """Merge all per-symbol bars into a single time-ordered timeline."""
        entries: List[Tuple[datetime, str, int]] = []
        for sym, bars in self._bars.items():
            for i, bar in enumerate(bars):
                entries.append((bar.timestamp, sym, i))
        entries.sort(key=lambda e: e[0])
        self._timeline = entries
        # Reset position if out of bounds
        if self._timeline_pos > len(self._timeline):
            self._timeline_pos = len(self._timeline)

    def _rebuild_indices_to(self, timeline_pos: int) -> None:
        """Rebuild per-symbol indices to reflect replay up to *timeline_pos*."""
        indices: Dict[str, int] = {sym: 0 for sym in self._bars}
        for i in range(timeline_pos):
            _, sym, bar_idx = self._timeline[i]
            indices[sym] = bar_idx + 1
        self._indices = indices

    def _run_loop(self) -> None:
        """Main replay loop (runs in thread or synchronously)."""
        logger.info("Replay loop started (timeline length=%d)", len(self._timeline))
        while True:
            with self._lock:
                if self._stopped:
                    break
                if self._timeline_pos >= len(self._timeline):
                    break

            # Wait if paused
            self._pause_event.wait()
            if self._stopped:
                break

            with self._lock:
                if self._timeline_pos >= len(self._timeline):
                    break
                ts, sym, bar_idx = self._timeline[self._timeline_pos]
                bar = self._bars[sym][bar_idx]
                self._indices[sym] = bar_idx + 1
                self._timeline_pos += 1

            # Fire callbacks outside the lock to avoid deadlocks
            for cb in self._on_bar_callbacks:
                try:
                    cb(sym, bar)
                except Exception:
                    logger.exception("on_bar callback error for %s", sym)

            # Fire tick callbacks if tick data present
            if sym in self._tick_data:
                tick_df = self._tick_data[sym]
                # Ticks within this bar's time window
                if bar_idx + 1 < len(self._bars[sym]):
                    next_ts = self._bars[sym][bar_idx + 1].timestamp
                else:
                    next_ts = bar.timestamp + pd.Timedelta(days=1)
                mask = (tick_df.index >= pd.Timestamp(bar.timestamp)) & (
                    tick_df.index < pd.Timestamp(next_ts)
                )
                for _, tick_row in tick_df.loc[mask].iterrows():
                    for cb in self._on_tick_callbacks:
                        try:
                            cb(sym, tick_row.to_dict())
                        except Exception:
                            logger.exception("on_tick callback error for %s", sym)

            # Speed delay
            if self._speed == self.SPEED_INSTANT:
                continue
            delay = self._base_interval / self._speed
            if delay > 0:
                time.sleep(delay)

        with self._lock:
            self._running = False
        logger.info("Replay loop finished")

    def __repr__(self) -> str:
        symbols = list(self._bars.keys())
        total = len(self._timeline)
        state = "running" if self._running else ("paused" if self._paused else "idle")
        return f"<ReplayEngine symbols={symbols} timeline={total} state={state} speed={self._speed}x>"
