"""Consistency Validation - Compare results across backtest, replay, and paper trading.

Ensures strategy behaviour is deterministic and reproducible across different
execution modes. Detects divergences in returns, trade counts, equity curves,
and position alignment with root-cause hints.
"""
from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

# Ensure project root is importable
for p in ("/app", "/opt/quantdesk", "/opt/quantdesk/src"):
    if p not in sys.path:
        sys.path.insert(0, p)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ConsistencyResult:
    """Outcome of a consistency validation check."""

    backtest_return_pct: float = 0.0
    replay_return_pct: float = 0.0
    paper_return_pct: float = 0.0

    return_diff_pct: dict[str, float] = field(default_factory=dict)
    # e.g. {"backtest_vs_replay": 0.12, "backtest_vs_paper": 0.05, "replay_vs_paper": 0.07}

    trade_count_diff: dict[str, int] = field(default_factory=dict)
    # e.g. {"backtest_vs_replay": 2, "backtest_vs_paper": 0, "replay_vs_paper": 2}

    position_diff: list[dict[str, Any]] = field(default_factory=list)
    # list of divergent positions

    is_consistent: bool = True
    tolerance_pct: float = 0.5
    details: str = ""

    # Additional metric comparisons
    sharpe_diff: dict[str, float] = field(default_factory=dict)
    max_drawdown_diff: dict[str, float] = field(default_factory=dict)
    equity_curve_correlation: dict[str, float] = field(default_factory=dict)
    trade_alignment: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable summary."""
        status = "CONSISTENT ✓" if self.is_consistent else "INCONSISTENT ✗"
        lines = [
            f"Consistency Check: {status} (tolerance={self.tolerance_pct}%)",
            f"  Backtest return: {self.backtest_return_pct:.2f}%",
            f"  Replay return:   {self.replay_return_pct:.2f}%",
            f"  Paper return:    {self.paper_return_pct:.2f}%",
        ]
        for pair, diff in self.return_diff_pct.items():
            flag = " !! " if abs(diff) > self.tolerance_pct else "    "
            lines.append(f"  {flag}{pair}: {diff:+.4f}%")
        for pair, diff in self.trade_count_diff.items():
            flag = " !! " if diff != 0 else "    "
            lines.append(f"  {flag}trade_count {pair}: {diff:+d}")
        if self.details:
            lines.append(f"  Details: {self.details}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# ConsistencyValidator
# ---------------------------------------------------------------------------

class ConsistencyValidator:
    """Compare backtest, replay, and paper trading results for consistency."""

    _PAIRS = [
        ("backtest", "replay"),
        ("backtest", "paper"),
        ("replay", "paper"),
    ]

    # ---- public API -------------------------------------------------------

    def validate(
        self,
        backtest_results: dict[str, Any],
        replay_results: dict[str, Any],
        paper_results: dict[str, Any],
        tolerance_pct: float = 0.5,
    ) -> ConsistencyResult:
        """Run full consistency validation across three result sets.

        Each *results dict* is expected to contain keys similar to what
        ``RealtimeMetrics.calculate()`` returns:
          - total_return_pct
          - total_trades
          - trades (list[dict])
          - equity_curve (list[dict] with 'equity' key)
          - max_drawdown_pct
          - sharpe
        """
        result = ConsistencyResult(tolerance_pct=tolerance_pct)

        # --- returns --------------------------------------------------------
        bt_ret = backtest_results.get("total_return_pct", 0.0) or 0.0
        rp_ret = replay_results.get("total_return_pct", 0.0) or 0.0
        pp_ret = paper_results.get("total_return_pct", 0.0) or 0.0

        result.backtest_return_pct = float(bt_ret)
        result.replay_return_pct = float(rp_ret)
        result.paper_return_pct = float(pp_ret)

        result.return_diff_pct = self._pairwise_diffs(bt_ret, rp_ret, pp_ret)

        # --- trade counts ---------------------------------------------------
        bt_tc = backtest_results.get("total_trades", 0) or 0
        rp_tc = replay_results.get("total_trades", 0) or 0
        pp_tc = paper_results.get("total_trades", 0) or 0
        result.trade_count_diff = {
            "backtest_vs_replay": int(bt_tc - rp_tc),
            "backtest_vs_paper": int(bt_tc - pp_tc),
            "replay_vs_paper": int(rp_tc - pp_tc),
        }

        # --- equity curve correlation ---------------------------------------
        bt_curve = self._extract_equity_series(backtest_results.get("equity_curve"))
        rp_curve = self._extract_equity_series(replay_results.get("equity_curve"))
        pp_curve = self._extract_equity_series(paper_results.get("equity_curve"))

        result.equity_curve_correlation = {
            "backtest_vs_replay": self.check_equity_curve_correlation(bt_curve, rp_curve),
            "backtest_vs_paper": self.check_equity_curve_correlation(bt_curve, pp_curve),
            "replay_vs_paper": self.check_equity_curve_correlation(rp_curve, pp_curve),
        }

        # --- max drawdown ---------------------------------------------------
        bt_dd = float(backtest_results.get("max_drawdown_pct", 0) or 0)
        rp_dd = float(replay_results.get("max_drawdown_pct", 0) or 0)
        pp_dd = float(paper_results.get("max_drawdown_pct", 0) or 0)
        result.max_drawdown_diff = self._pairwise_diffs(bt_dd, rp_dd, pp_dd)

        # --- Sharpe ratio ---------------------------------------------------
        bt_sh = backtest_results.get("sharpe")
        rp_sh = replay_results.get("sharpe")
        pp_sh = paper_results.get("sharpe")
        result.sharpe_diff = self._pairwise_diffs(
            bt_sh if bt_sh is not None else 0.0,
            rp_sh if rp_sh is not None else 0.0,
            pp_sh if pp_sh is not None else 0.0,
        )

        # --- trade alignment (pairwise) -------------------------------------
        bt_trades = backtest_results.get("trades", [])
        rp_trades = replay_results.get("trades", [])
        pp_trades = paper_results.get("trades", [])

        result.trade_alignment = {
            "backtest_vs_replay": self.check_trade_alignment(bt_trades, rp_trades),
            "backtest_vs_paper": self.check_trade_alignment(bt_trades, pp_trades),
            "replay_vs_paper": self.check_trade_alignment(rp_trades, pp_trades),
        }

        # --- position diffs -------------------------------------------------
        result.position_diff = self._compute_position_diffs(
            backtest_results, replay_results, paper_results
        )

        # --- verdict --------------------------------------------------------
        detail_parts: list[str] = []

        # Check return diffs against tolerance
        for pair, diff in result.return_diff_pct.items():
            if abs(diff) > tolerance_pct:
                result.is_consistent = False
                detail_parts.append(f"{pair} return diff {diff:+.4f}% > {tolerance_pct}%")

        # Check trade count diffs (any nonzero is inconsistent)
        for pair, diff in result.trade_count_diff.items():
            if diff != 0:
                result.is_consistent = False
                detail_parts.append(f"{pair} trade count diff {diff:+d}")

        # Check equity curve correlation (should be > 0.99)
        for pair, corr in result.equity_curve_correlation.items():
            if corr < 0.99 and not math.isnan(corr):
                result.is_consistent = False
                detail_parts.append(f"{pair} equity curve correlation {corr:.4f} < 0.99")

        # Check max drawdown diff
        for pair, diff in result.max_drawdown_diff.items():
            if abs(diff) > tolerance_pct * 2:  # wider tolerance for drawdown
                result.is_consistent = False
                detail_parts.append(f"{pair} max_dd diff {diff:+.4f}% > {tolerance_pct * 2}%")

        result.details = "; ".join(detail_parts) if detail_parts else "All checks passed"

        logger.info("Consistency check: is_consistent=%s", result.is_consistent)
        return result

    # ---- equity curve correlation -----------------------------------------

    @staticmethod
    def check_equity_curve_correlation(
        curve1: pd.Series | None, curve2: pd.Series | None
    ) -> float:
        """Pearson correlation between two equity curves.

        Returns ``float('nan')`` if either curve is empty or has zero
        variance.
        """
        if curve1 is None or curve2 is None or curve1.empty or curve2.empty:
            return float("nan")

        # Align to same length (use the shorter)
        min_len = min(len(curve1), len(curve2))
        if min_len < 2:
            return float("nan")

        c1 = curve1.iloc[:min_len].reset_index(drop=True)
        c2 = curve2.iloc[:min_len].reset_index(drop=True)

        std1, std2 = c1.std(), c2.std()
        if std1 == 0 or std2 == 0:
            return float("nan")

        return float(c1.corr(c2))

    # ---- trade alignment --------------------------------------------------

    @staticmethod
    def check_trade_alignment(
        trades1: list[dict[str, Any]],
        trades2: list[dict[str, Any]],
        tolerance_bars: int = 1,
    ) -> dict[str, Any]:
        """Compare two trade lists for alignment.

        Matches trades by symbol + side, then checks that entry/exit bars
        (or timestamps) are within *tolerance_bars* of each other.

        Returns a dict with:
          - matched: int
          - unmatched_1: list  (trades in trades1 not matched)
          - unmatched_2: list  (trades in trades2 not matched)
          - timing_diffs: list[dict]  (matched trades with bar-level timing diff)
        """
        if not trades1 and not trades2:
            return {"matched": 0, "unmatched_1": [], "unmatched_2": [], "timing_diffs": []}

        used2: set[int] = set()
        matched = 0
        timing_diffs: list[dict[str, Any]] = []
        unmatched_1: list[dict[str, Any]] = []

        for t1 in trades1:
            key1 = (t1.get("symbol", ""), t1.get("side", ""))
            best_idx = None
            best_diff = None

            for i2, t2 in enumerate(trades2):
                if i2 in used2:
                    continue
                key2 = (t2.get("symbol", ""), t2.get("side", ""))
                if key1 != key2:
                    continue

                # Compute bar-level timing difference
                diff = ConsistencyValidator._trade_timing_diff(t1, t2)
                if diff is not None and diff <= tolerance_bars:
                    if best_diff is None or diff < best_diff:
                        best_idx = i2
                        best_diff = diff

            if best_idx is not None:
                used2.add(best_idx)
                matched += 1
                if best_diff and best_diff > 0:
                    timing_diffs.append({
                        "trade": t1,
                        "bar_diff": best_diff,
                    })
            else:
                unmatched_1.append(t1)

        unmatched_2 = [t2 for i2, t2 in enumerate(trades2) if i2 not in used2]

        return {
            "matched": matched,
            "unmatched_1": unmatched_1,
            "unmatched_2": unmatched_2,
            "timing_diffs": timing_diffs,
        }

    # ---- private helpers --------------------------------------------------

    @staticmethod
    def _trade_timing_diff(t1: dict[str, Any], t2: dict[str, Any]) -> int | None:
        """Return absolute bar-index (or timestamp) difference between two trades.

        Returns None if we cannot compute a meaningful diff.
        """
        # Try bar-index style
        for key in ("entry_bar", "exit_bar", "bar_index"):
            v1, v2 = t1.get(key), t2.get(key)
            if v1 is not None and v2 is not None:
                try:
                    return abs(int(v1) - int(v2))
                except (TypeError, ValueError):
                    pass

        # Try timestamp style
        for key in ("entry_time", "exit_time", "timestamp"):
            v1, v2 = t1.get(key), t2.get(key)
            if v1 is not None and v2 is not None:
                try:
                    if isinstance(v1, str):
                        v1 = datetime.fromisoformat(v1)
                    if isinstance(v2, str):
                        v2 = datetime.fromisoformat(v2)
                    return abs((v1 - v2).days)
                except Exception:
                    pass

        return None

    @staticmethod
    def _extract_equity_series(curve: list[dict[str, Any]] | None) -> pd.Series:
        """Convert an equity curve (list of dicts) to a pandas Series."""
        if not curve:
            return pd.Series(dtype=float)
        values = []
        for pt in curve:
            eq = pt.get("equity") if isinstance(pt, dict) else pt
            if eq is not None:
                values.append(float(eq))
        return pd.Series(values)

    def _pairwise_diffs(
        self, bt: float, rp: float, pp: float
    ) -> dict[str, float]:
        return {
            "backtest_vs_replay": round(bt - rp, 6),
            "backtest_vs_paper": round(bt - pp, 6),
            "replay_vs_paper": round(rp - pp, 6),
        }

    @staticmethod
    def _compute_position_diffs(
        backtest_results: dict[str, Any],
        replay_results: dict[str, Any],
        paper_results: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Find positions that differ across the three result sets."""
        diffs: list[dict[str, Any]] = []

        bt_positions = {
            p.get("symbol", ""): p
            for p in backtest_results.get("positions", [])
        }
        rp_positions = {
            p.get("symbol", ""): p
            for p in replay_results.get("positions", [])
        }
        pp_positions = {
            p.get("symbol", ""): p
            for p in paper_results.get("positions", [])
        }

        all_symbols = set(bt_positions) | set(rp_positions) | set(pp_positions)

        for sym in sorted(all_symbols):
            bt_p = bt_positions.get(sym)
            rp_p = rp_positions.get(sym)
            pp_p = pp_positions.get(sym)

            # Check if present in all three
            present = [bt_p is not None, rp_p is not None, pp_p is not None]
            if not all(present):
                diffs.append({
                    "symbol": sym,
                    "type": "missing",
                    "in_backtest": bt_p is not None,
                    "in_replay": rp_p is not None,
                    "in_paper": pp_p is not None,
                })
                continue

            # Compare quantities and prices
            for field_name in ("qty", "quantity", "shares", "entry_price", "avg_price"):
                vals = [
                    bt_p.get(field_name),
                    rp_p.get(field_name),
                    pp_p.get(field_name),
                ]
                if vals[0] is not None and vals[1] is not None and vals[2] is not None:
                    try:
                        fv = [float(v) for v in vals]
                        if max(fv) - min(fv) > 1e-6:
                            diffs.append({
                                "symbol": sym,
                                "type": "value_mismatch",
                                "field": field_name,
                                "backtest": vals[0],
                                "replay": vals[1],
                                "paper": vals[2],
                            })
                    except (TypeError, ValueError):
                        pass

        return diffs


# ---------------------------------------------------------------------------
# BacktestReplayConsistency
# ---------------------------------------------------------------------------

class BacktestReplayConsistency:
    """Run the same strategy on the same data via both backtest and replay
    engines, then compare results and report divergences with root-cause
    hints.

    This is a higher-level orchestrator that wraps ``ConsistencyValidator``.
    """

    def __init__(self, validator: ConsistencyValidator | None = None) -> None:
        self._validator = validator or ConsistencyValidator()

    # ---- public API -------------------------------------------------------

    def run(
        self,
        strategy_code: str,
        market_data: pd.DataFrame,
        backtest_engine: Any,
        replay_engine: Any,
        initial_capital: float = 100_000.0,
        tolerance_pct: float = 0.5,
    ) -> ConsistencyResult:
        """Execute the strategy on both engines and compare.

        Parameters
        ----------
        strategy_code : str
            The strategy source code (Python).
        market_data : pd.DataFrame
            OHLCV data to feed both engines.
        backtest_engine : Any
            An object with ``run(strategy_code, data, initial_capital) -> dict``.
        replay_engine : Any
            An object with ``run(strategy_code, data, initial_capital) -> dict``.
        initial_capital : float
            Starting capital for both runs.
        tolerance_pct : float
            Acceptable difference threshold.

        Returns
        -------
        ConsistencyResult
        """
        logger.info("Running backtest-replay consistency check")

        # --- run backtest ---------------------------------------------------
        try:
            bt_results = backtest_engine.run(strategy_code, market_data, initial_capital)
        except Exception as exc:
            bt_results = {}
            logger.error("Backtest engine failed: %s", exc)

        # --- run replay -----------------------------------------------------
        try:
            rp_results = replay_engine.run(strategy_code, market_data, initial_capital)
        except Exception as exc:
            rp_results = {}
            logger.error("Replay engine failed: %s", exc)

        # --- compare (paper is empty dict — only bt vs replay) --------------
        result = self._validator.validate(
            bt_results, rp_results, {}, tolerance_pct=tolerance_pct
        )

        # Add root-cause hints
        if not result.is_consistent:
            result.details = self._add_root_cause_hints(result, bt_results, rp_results)

        return result

    def compare_results(
        self,
        backtest_results: dict[str, Any],
        replay_results: dict[str, Any],
        tolerance_pct: float = 0.5,
    ) -> ConsistencyResult:
        """Compare pre-computed backtest and replay result dicts."""
        result = self._validator.validate(
            backtest_results, replay_results, {}, tolerance_pct=tolerance_pct
        )
        if not result.is_consistent:
            result.details = self._add_root_cause_hints(
                result, backtest_results, replay_results
            )
        return result

    # ---- root-cause hints -------------------------------------------------

    @staticmethod
    def _add_root_cause_hints(
        result: ConsistencyResult,
        bt_results: dict[str, Any],
        rp_results: dict[str, Any],
    ) -> str:
        """Append root-cause hints based on the nature of divergences."""
        hints: list[str] = [result.details]

        # Return divergence but trades match → likely rounding / slippage model
        return_divergent = any(
            abs(d) > result.tolerance_pct for d in result.return_diff_pct.values()
        )
        trades_match = all(d == 0 for d in result.trade_count_diff.values())

        if return_divergent and trades_match:
            hints.append(
                "HINT: Same number of trades but returns differ → "
                "likely cause: different fill/slippage/fee models between engines, "
                "or floating-point rounding in position sizing."
            )

        # Trade count divergence → different signal/bar alignment
        if any(d != 0 for d in result.trade_count_diff.values()):
            hints.append(
                "HINT: Trade count differs → likely cause: bar timing "
                "misalignment (look-ahead bias in backtest vs confirmed-bar "
                "in replay), or different handling of market-on-open / close orders."
            )

        # Low equity curve correlation → fundamentally different trade sequence
        for pair, corr in result.equity_curve_correlation.items():
            if not math.isnan(corr) and corr < 0.95:
                hints.append(
                    f"HINT: {pair} equity curve correlation {corr:.4f} is low → "
                    "strategy logic may behave differently under the two engines. "
                    "Check for data-snooping, non-deterministic random seeds, or "
                    "different bar-close semantics."
                )

        # Max drawdown divergence
        for pair, diff in result.max_drawdown_diff.items():
            if abs(diff) > result.tolerance_pct * 2:
                hints.append(
                    f"HINT: {pair} max drawdown diverges by {diff:+.2f}% → "
                    "position sizing or stop-loss execution may differ between engines."
                )

        return "\n".join(hints)
