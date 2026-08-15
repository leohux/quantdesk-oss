"""
Phase 6.2: Paper Trading Runner (v4)
=====================================
Loads strategies from strategies.json + strategy_code/,
generates signals, runs risk checks, submits Alpaca paper orders,
and logs to PostgreSQL.

v4 fixes (shared paper account):
  - Upsert enabled strategies into Postgres before order logging (FK)
  - One actionable order per symbol per run (no multi-strategy pile-on)
  - Track local buying_power / positions after each fill
  - Cap BUY qty by remaining buying_power
  - On SELL: cancel resting bracket legs first, then sell available qty

Usage:
  python phase6_runner.py                        # daily run (all enabled strategies)
  python phase6_runner.py --symbol AAPL          # single symbol
  python phase6_runner.py --dry-run              # signal only, no orders
  python phase6_runner.py --mode force-buy --symbol AAPL   # acceptance: force 1 BUY
  python phase6_runner.py --mode force-sell --symbol AAPL  # acceptance: force 1 SELL
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, '/app')

from core.portfolio.sleeve import (
    LedgerUnavailable,
    SleeveBook,
    broker_qty_map,
    ensure_schema as ensure_sleeve_schema,
    open_order_symbols,
)
from core.portfolio.account_guard import check_account_buy
from core.portfolio.combined_position_guard import check_combined_buy
from core.portfolio.fills import await_fill, reprice_protection
from core.portfolio.settle import format_settlements, settle_broker_exits
from core.config.calendar import hold_trading_days
from core.trade_log import (
    ensure_strategy,
    mark_stale_journal,
    record_entry,
    record_exit,
)
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from data.loader import load_ohlcv
from strategies.engine import run_signal_fn
from execution.alpaca_client import AlpacaPaperClient
from execution.broker_factory import get_trading_client

# ── Constants ─────────────────────────────────────────────
STORE_DIR = Path('/app/data/store')
STRATEGIES_JSON = STORE_DIR / 'strategies.json'
STRATEGY_CODE_DIR = STORE_DIR / 'strategy_code'
RUNNER_VERSION = 'v10'

# Risk parameters
MAX_POSITION_PCT = 20.0      # single stock max % of equity (used when no weight)
RISK_PER_TRADE_PCT = 2.0     # risk per trade % of equity
ATR_STOP_MULTIPLIER = 2.0    # stop = entry - ATR * 2
DAILY_STOP_MIN_PCT = 0.03    # floor |SL| 3%
DAILY_STOP_MAX_PCT = 0.10    # cap |SL| 10%
DAILY_TP_RR = 2.5            # take-profit = |SL| * RR (bracket needs TP)
DAILY_LOSS_HALT_PCT = 2.0    # halt if daily loss >= 2%
MAX_DAILY_ORDERS_PER_SYMBOL = 1  # shared account: one action per symbol / day-run

# Portfolio layer (ERC): each strategy carries params.portfolio_weight (0..1).
# Per-name budget = weight * gross, hard-capped by SINGLE_NAME_CAP_PCT for safety.
PORTFOLIO_GROSS = float(os.environ.get("PORTFOLIO_GROSS", "1.5"))  # target gross (1.5x leverage)
SINGLE_NAME_CAP_PCT = 10.0   # hard ceiling per position regardless of weight
MAX_GROSS_EXPOSURE_PCT = float(os.environ.get("MAX_GROSS_EXPOSURE_PCT", "150"))  # allow 1.5x book

SYMBOLS_DEFAULT = ['AAPL', 'MSFT', 'NVDA', 'QQQ']


# ── Helpers ───────────────────────────────────────────────

def load_strategies(enabled_only: bool = True) -> list[dict]:
    """Load strategies from strategies.json."""
    if not STRATEGIES_JSON.exists():
        print(f"[WARN] {STRATEGIES_JSON} not found")
        return []
    with open(STRATEGIES_JSON) as f:
        data = json.load(f)
    strategies = data if isinstance(data, list) else list(data.values())
    if enabled_only:
        strategies = [s for s in strategies if s.get('enabled', False)]
    return strategies


def load_strategy_code(strategy_id: str) -> str | None:
    """Load strategy code from strategy_code/ directory."""
    for candidate in [
        STRATEGY_CODE_DIR / f'{strategy_id}.py',
        STRATEGY_CODE_DIR / strategy_id,
    ]:
        if candidate.exists():
            return candidate.read_text()
    return None



def _bracket_pcts(entry_price: float, atr: float | None) -> tuple[float, float]:
    """Return (stop_loss_pct negative, take_profit_pct positive) for daily brackets."""
    if entry_price <= 0:
        return -0.08, 0.20
    if atr is not None and atr == atr and atr > 0:
        raw = (float(atr) * ATR_STOP_MULTIPLIER) / entry_price
    else:
        raw = 0.08
    sl_mag = min(DAILY_STOP_MAX_PCT, max(DAILY_STOP_MIN_PCT, raw))
    return -sl_mag, sl_mag * DAILY_TP_RR


def is_mimo_meanrev(strat_name: str, params: dict | None = None) -> bool:
    """MiMo RSI-extreme / mean-reversion sleeve ??gets price-zone limit entry."""
    n = (strat_name or "").lower()
    if "mimo" in n and ("mean" in n or "reversion" in n or "rsi" in n):
        return True
    p = params or {}
    return p.get("entry_level") is not None and p.get("exit_level") is not None and "rsi_period" in p


def mimo_price_zones(
    df: pd.DataFrame,
    *,
    last_price: float,
    atr: float,
    params: dict | None = None,
) -> dict[str, Any]:
    """Build buy/sell price zones for MiMo dip entries.

    Buy zone  : near N-day support, padded by ATR (limit near zone mid-low).
    Sell zone : toward recent mean / resistance (TP at zone mid).
    Stop      : below support by 0.5 ATR.
    """
    p = params or {}
    lookback = int(p.get("zone_lookback", 10))
    lookback = max(5, min(lookback, 40))
    atr = float(atr) if atr and atr == atr and atr > 0 else float(last_price) * 0.02

    low = df["low"].astype(float).iloc[-lookback:] if "low" in df.columns else df["close"].astype(float).iloc[-lookback:]
    high = df["high"].astype(float).iloc[-lookback:] if "high" in df.columns else df["close"].astype(float).iloc[-lookback:]
    close_s = df["close"].astype(float).iloc[-lookback:]
    support = float(low.min())
    resistance = float(high.max())
    mean_px = float(close_s.mean())

    buy_lo = round(support, 2)
    buy_hi = round(min(float(last_price), support + 0.50 * atr), 2)
    if buy_hi <= buy_lo:
        buy_hi = round(buy_lo + max(0.15 * atr, 0.01), 2)
    # Prefer lower third of zone (true dip limit, not chase)
    limit_px = round(buy_lo + 0.35 * (buy_hi - buy_lo), 2)

    sell_lo = round(max(float(last_price) * 1.015, mean_px, limit_px * 1.02), 2)
    sell_hi = round(max(sell_lo + 0.35 * atr, min(resistance, float(last_price) + 2.0 * atr)), 2)
    if sell_hi <= sell_lo:
        sell_hi = round(sell_lo + max(0.5 * atr, 0.05), 2)
    tp_px = round((sell_lo + sell_hi) / 2.0, 2)

    sl_px = round(support - 0.50 * atr, 2)
    # Keep valid geometry: SL < limit < TP
    if sl_px >= limit_px:
        sl_px = round(limit_px * (1.0 - 0.03), 2)
    if tp_px <= limit_px:
        tp_px = round(limit_px * 1.04, 2)
        sell_lo = limit_px
        sell_hi = tp_px

    return {
        "buy_zone": (buy_lo, buy_hi),
        "sell_zone": (sell_lo, sell_hi),
        "limit_price": limit_px,
        "stop_loss": sl_px,
        "take_profit": tp_px,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "atr": round(atr, 4),
    }

def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def log_to_db(table: str, data: dict) -> None:
    """Insert a row into a PostgreSQL table."""
    try:
        from core.db import SyncSessionLocal
        from sqlalchemy import text
        s = SyncSessionLocal()
        cols = ', '.join(data.keys())
        placeholders = ', '.join(f':{k}' for k in data.keys())
        s.execute(text(f'INSERT INTO {table} ({cols}) VALUES ({placeholders})'), data)
        s.commit()
        s.close()
    except Exception as e:
        print(f"  [DB WARN] Failed to log to {table}: {e}")


def ensure_strategy_in_db(strat: dict) -> None:
    """Upsert strategy row so orders.strategy_id FK succeeds."""
    ensure_strategy(
        str(strat.get('id') or ''),
        name=strat.get('name'),
        stype=str(strat.get('type') or 'custom'),
        enabled=bool(strat.get('enabled', True)),
        params=strat.get('params'),
        metrics=strat.get('metrics'),
    )


def ensure_runner_history_table() -> None:
    """Create runner_history table if it doesn't exist."""
    try:
        from core.db import SyncSessionLocal
        from sqlalchemy import text
        s = SyncSessionLocal()
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS runner_history (
                id              SERIAL PRIMARY KEY,
                run_time        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                mode            VARCHAR(20) NOT NULL DEFAULT 'daily',
                strategies_loaded INTEGER DEFAULT 0,
                symbols_scanned  INTEGER DEFAULT 0,
                signal_buy      INTEGER DEFAULT 0,
                signal_sell     INTEGER DEFAULT 0,
                signal_hold     INTEGER DEFAULT 0,
                risk_rejected   INTEGER DEFAULT 0,
                orders_submitted INTEGER DEFAULT 0,
                orders_filled   INTEGER DEFAULT 0,
                orders_failed   INTEGER DEFAULT 0,
                halt_triggered  BOOLEAN DEFAULT FALSE,
                runtime_seconds FLOAT DEFAULT 0,
                errors          TEXT,
                version         VARCHAR(10) DEFAULT 'v3'
            )
        """))
        s.commit()
        s.close()
    except Exception as e:
        print(f"  [DB WARN] Failed to create runner_history: {e}")


def log_runner_history(data: dict) -> None:
    """Log a run to runner_history table."""
    log_to_db('runner_history', data)


# ── Main Runner ───────────────────────────────────────────

class PaperTradingRunner:
    """Daily paper trading pipeline."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.now = datetime.now(timezone.utc)
        # Paper today; live reserved in broker_factory (refuses until implemented).
        self.client = get_trading_client()
        self.account = self.client.account()
        self.equity = self.account['equity']
        self.cash = self.account['cash']
        self.buying_power = float(self.account.get('buying_power') or self.cash or 0)
        self.existing_positions = {p['symbol']: p for p in self.client.positions()}
        self.orders_submitted = []
        self.orders_filled = []
        self.orders_failed = []
        self.orders_rejected = []
        self.signals_generated = []  # all signals (BUY/SELL/HOLD)
        self.errors = []
        # Shared-account guards: one actionable order per symbol this run
        self._acted_symbols: set[str] = set()
        self._pending_buy_notional = 0.0
        # Sleeve ledger: which strategy owns which position
        self.sleeve: SleeveBook | None = None
        self.sleeve_error: str | None = None
        self.sleeve_changes: list[Any] = []
        self.settlements: list[dict[str, Any]] = []
        self.halt_triggered = False

    def _load_sleeves(self) -> None:
        """Attach the ownership ledger, sync it to broker truth, settle exits."""
        try:
            ensure_sleeve_schema()
            book = SleeveBook.load()
            self.sleeve_changes = book.reconcile(
                broker_qty_map(self.existing_positions.values()),
                protected=open_order_symbols(self.client),
            )
            self.sleeve = book
            self.settlements = settle_broker_exits(self.client, self.sleeve_changes)
        except LedgerUnavailable as e:
            self.sleeve = None
            self.sleeve_error = str(e)
            self.errors.append(f"Sleeve ledger unavailable: {e}")

    def _refresh_broker_state(self, report_lines: list[str] | None = None) -> None:
        """Reload positions, buying power and sleeve right before order submission."""
        try:
            account = self.client.account()
            self.equity = float(account["equity"])
            self.buying_power = float(account["buying_power"])
            self.existing_positions = {
                p["symbol"]: p for p in self.client.positions()
            }
        except Exception as exc:
            msg = f"broker refresh failed: {exc}"
            self.errors.append(msg)
            if report_lines is not None:
                report_lines.append(f"  [WARN] {msg}")
            return
        try:
            book = SleeveBook.load()
            changes = book.reconcile(
                broker_qty_map(self.existing_positions.values()),
                protected=open_order_symbols(self.client),
            )
            self.sleeve = book
            if changes and report_lines is not None:
                for change in changes:
                    report_lines.append(f"  [REFRESH {change.kind}] {change}")
            settled = settle_broker_exits(self.client, changes)
            if settled and report_lines is not None:
                for line in format_settlements(settled):
                    report_lines.append(f"  [REFRESH settled] {line}")
        except LedgerUnavailable as exc:
            self.sleeve = None
            self.sleeve_error = str(exc)
            if report_lines is not None:
                report_lines.append(f"  [WARN] sleeve refresh failed: {exc}")

    def run(self, symbols: list[str] | None = None, mode: str = 'daily') -> str:
        """Execute the full daily pipeline."""
        t0 = time.time()
        strategies = load_strategies(enabled_only=True)
        if not symbols:
            strat_syms: list[str] = []
            for strat in strategies:
                for s in (strat.get("params") or {}).get("symbols") or []:
                    u = str(s).upper()
                    if u not in strat_syms:
                        strat_syms.append(u)
            symbols = strat_syms or SYMBOLS_DEFAULT

        report_lines = []
        report_lines.append(f"{'='*60}")
        report_lines.append(f"PAPER TRADING DAILY — {self.today}")
        report_lines.append(f"{'='*60}")
        report_lines.append(f"  Equity: ${self.equity:,.2f} | Cash: ${self.cash:,.2f} | BP: ${self.buying_power:,.2f}")
        report_lines.append(f"  Positions: {len(self.existing_positions)}")
        report_lines.append(f"  Enabled strategies: {len(strategies)}")
        report_lines.append(f"  Symbols: {', '.join(symbols)}")
        report_lines.append(f"  Mode: {mode} | Dry-run: {self.dry_run}")
        report_lines.append(f"  Runner: {RUNNER_VERSION}")
        report_lines.append("")

        # Cancel post-close zombie MARKET orders that lock buying power / shares
        try:
            zombies = self.client.cancel_stale_market_orders()
            if zombies:
                report_lines.append(
                    f"  Canceled {len(zombies)} stale MARKET order(s): "
                    + ", ".join(f"{z['side']} {z['symbol']}x{z['qty']}" for z in zombies[:8])
                )
                # Refresh account snapshot after cancels
                self.account = self.client.account()
                self.equity = self.account['equity']
                self.cash = self.account['cash']
                self.buying_power = float(self.account.get('buying_power') or self.cash or 0)
                self.existing_positions = {p['symbol']: p for p in self.client.positions()}
                report_lines.append(
                    f"  After cancel — Equity: ${self.equity:,.2f} | BP: ${self.buying_power:,.2f} | "
                    f"Positions: {len(self.existing_positions)}"
                )
        except Exception as e:
            report_lines.append(f"  [WARN] stale order cleanup failed: {e}")

        self._load_sleeves()
        if self.sleeve is None:
            report_lines.append(
                f"  [SLEEVE] ledger unavailable ({self.sleeve_error}) — "
                f"trading disabled, broker brackets remain the only protection"
            )
            self.dry_run = True
        else:
            owners = self.sleeve.holdings()
            report_lines.append(f"  Sleeve ledger: {len(owners)} owned position(s)")
            for symbol, pos in sorted(owners.items()):
                report_lines.append(f"      {symbol:6s} {pos.qty:>7.0f}  {pos.strategy_id}")
            for change in self.sleeve_changes:
                report_lines.append(f"      [{change.kind}] {change}")
            for line in format_settlements(self.settlements):
                report_lines.append(f"      [settled] {line}")

        market_open = True
        try:
            market_open = self.client.is_market_open()
        except Exception as e:
            report_lines.append(f"  [WARN] clock check failed: {e}")
        report_lines.append(f"  Market open: {market_open}")
        if not market_open and not self.dry_run:
            report_lines.append(
                "[SKIP LIVE] US cash market closed — signals only "
                "(cron should run ~15:50 ET, not after close)"
            )
            self.dry_run = True

        if not strategies:
            report_lines.append("[WARN] No enabled strategies found in strategies.json")
            report = '\n'.join(report_lines)
            self._print_and_log_history(report, len(strategies), len(symbols), mode, time.time() - t0)
            return report

        # Ensure Postgres strategy rows exist (orders.strategy_id FK)
        for strat in strategies:
            ensure_strategy_in_db(strat)
        # Also ensure common special IDs used by other runners
        ensure_strategy_in_db({
            'id': 'strategy-046bfa',
            'name': '美股科技股突破延续',
            'type': 'intraday_surge',
            'enabled': True,
            'params': {},
        })
        ensure_strategy_in_db({
            'id': 'acceptance-test',
            'name': 'Acceptance Test',
            'type': 'test',
            'enabled': False,
        })
        report_lines.append(f"  Synced {len(strategies)} enabled strategies (+intraday/test) to Postgres")

        if not self.dry_run:
            try:
                scripts_dir = str(Path(__file__).resolve().parent)
                if scripts_dir not in sys.path:
                    sys.path.insert(0, scripts_dir)
                from ops_atrbreak_orphans import maybe_flatten_from_runner

                maybe_flatten_from_runner(min_interval_sec=0)
                report_lines.append("  OPS flatten pass (ATRBreak / unassigned orphans)")
            except Exception as exc:
                report_lines.append(f"  [WARN] ops flatten: {exc}")
            try:
                self._maybe_time_exits(strategies, report_lines)
            except Exception as exc:
                report_lines.append(f"  [WARN] time-exit pass failed: {exc}")

        # Load market data for all symbols (parallel I/O)
        market_data = {}
        start = (datetime.now() - timedelta(days=3*365)).strftime('%Y-%m-%d')

        def _load_one(sym: str):
            df = load_ohlcv(sym, start=start)
            df.columns = [c.lower() for c in df.columns]
            if 'date' in df.columns:
                df = df.set_index('date')
            return sym, df

        with ThreadPoolExecutor(max_workers=min(32, max(4, len(symbols)))) as pool:
            futs = {pool.submit(_load_one, sym): sym for sym in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    s, df = fut.result()
                    market_data[s] = df
                    print(f"  Loaded {s}: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
                except Exception as e:
                    err_msg = f"Failed to load {sym}: {e}"
                    print(f"  [ERROR] {err_msg}")
                    self.errors.append(err_msg)

        # Check daily loss halt
        halt_triggered = self.halt_triggered = self._check_daily_loss_halt()
        if halt_triggered:
            report_lines.append("[HALT] Daily loss limit reached — no new orders today")

        # Process each strategy × symbol
        # SELL pass first (free capital / unlock brackets), then BUY
        report_lines.append(f"\n--- Signals ---")
        pending: list[dict[str, Any]] = []
        for strat in strategies:
            strat_id = strat['id']
            strat_name = strat.get('name', strat_id)
            strat_params = strat.get('params', {})
            strat_symbols = [str(s).upper() for s in (strat_params.get('symbols') or symbols)]

            code = load_strategy_code(strat_id)
            if not code:
                from strategies.engine import TEMPLATES
                code = TEMPLATES.get(strat.get('type', ''))
                if not code:
                    report_lines.append(f"  {strat_name}: [SKIP] no code found")
                    continue

            for sym in strat_symbols:
                if sym not in market_data:
                    continue
                df = market_data[sym]
                if len(df) < 60:
                    report_lines.append(f"  {sym}/{strat_name}: [SKIP] insufficient data ({len(df)} bars)")
                    continue

                try:
                    action = self._build_signal(
                        strat_id, strat_name, sym, df, code, strat_params, halt_triggered, report_lines
                    )
                    if action:
                        pending.append(action)
                except Exception as e:
                    err_msg = f"{sym}/{strat_name}: {e}"
                    report_lines.append(f"  {err_msg} [ERROR]")
                    self.errors.append(err_msg)

        # Resolve conflicts: one action per symbol; SELL beats BUY
        resolved = self._resolve_actions(pending, report_lines)
        # Data load can take minutes; refresh broker + sleeve truth before
        # any order so intraday/news fills during the scan are visible.
        if resolved:
            self._refresh_broker_state(report_lines)
        for action in resolved:
            try:
                self._execute_action(action, report_lines)
            except Exception as e:
                err_msg = f"execute {action.get('symbol')}/{action.get('strategy_name')}: {e}"
                report_lines.append(f"  {err_msg} [ERROR]")
                self.errors.append(err_msg)

        # ── Daily Summary Statistics ──
        elapsed = time.time() - t0
        sig_buy = sum(1 for s in self.signals_generated if s['side'] == 'buy')
        sig_sell = sum(1 for s in self.signals_generated if s['side'] == 'sell')
        sig_hold = sum(1 for s in self.signals_generated if s['side'] == 'hold')

        report_lines.append(f"\n{'='*60}")
        report_lines.append(f"DAILY SUMMARY")
        report_lines.append(f"{'='*60}")
        report_lines.append(f"  Strategies loaded  : {len(strategies)}")
        report_lines.append(f"  Symbols scanned    : {len(market_data)}")
        report_lines.append(f"  Signals:")
        report_lines.append(f"    BUY              : {sig_buy}")
        report_lines.append(f"    SELL             : {sig_sell}")
        report_lines.append(f"    HOLD             : {sig_hold}")
        report_lines.append(f"  Actions executed   : {len(resolved)}")
        report_lines.append(f"  Risk rejected      : {len(self.orders_rejected)}")
        report_lines.append(f"  Orders submitted   : {len(self.orders_submitted)}")
        report_lines.append(f"  Orders filled      : {len(self.orders_filled)}")
        report_lines.append(f"  Orders failed      : {len(self.orders_failed)}")
        report_lines.append(f"  Halt triggered     : {'YES' if halt_triggered else 'No'}")
        report_lines.append(f"  Errors             : {len(self.errors)}")
        report_lines.append(f"  Runtime            : {elapsed:.1f}s")
        report_lines.append(f"{'='*60}")

        # trade_journal is written from _execute_action on real submissions only.
        # Retire rows left open by exits the runners never saw (bracket fills).
        if self.sleeve is not None and not self.dry_run:
            # Do not stale journal rows for symbols still at the broker — sleeve
            # may have briefly orphaned them to __unassigned__ (news-trader re-adopts).
            broker_held = {
                str(sym).upper()
                for sym, p in (self.existing_positions or {}).items()
                if float((p or {}).get("qty") or 0) > 0
            }
            stale = mark_stale_journal(broker_held)
            if stale:
                report_lines.append(f"  Retired {stale} stale trade_journal row(s)")
            # Wind-down complete: sleeve empty + allow_new_entries=false → disable.
            try:
                self._maybe_finish_wind_downs(strategies, report_lines)
            except Exception as exc:
                report_lines.append(f"  [WARN] wind-down finalize failed: {exc}")

        report = '\n'.join(report_lines)
        self._print_and_log_history(report, len(strategies), len(market_data), mode, elapsed)
        return report

    def _maybe_finish_wind_downs(self, strategies: list[dict], report_lines: list[str]) -> None:
        """When a strategy is exits-only and its sleeve is flat, set enabled=false."""
        from datetime import datetime, timezone

        from config.store import update_strategy

        if self.sleeve is None:
            return
        for strat in strategies:
            params = strat.get('params') or {}
            if bool(params.get('allow_new_entries', True)):
                continue
            sid = strat['id']
            held = self.sleeve.symbols_of(sid)
            if held:
                report_lines.append(
                    f"  WIND_DOWN {sid}: exits-only, still holding {sorted(held)}"
                )
                continue
            update_strategy(
                sid,
                {
                    'enabled': False,
                    'params': {
                        **params,
                        'allow_new_entries': False,
                        'wind_down_completed_at': datetime.now(timezone.utc).isoformat(),
                        'lifecycle': 'RESEARCH_ARCHIVE_GATE_FAIL',
                    },
                },
            )
            report_lines.append(
                f"  WIND_DOWN_COMPLETE {sid}: sleeve empty → enabled=false"
            )

    def _journal_opened_at(self, strategy_id: str, symbol: str) -> datetime | None:
        try:
            from sqlalchemy import text
            from core.db import SyncSessionLocal

            s = SyncSessionLocal()
            try:
                row = s.execute(
                    text(
                        """
                        SELECT MIN(opened_at) FROM trade_journal
                        WHERE strategy_id = :sid AND symbol = :sym
                          AND side = 'buy' AND status = 'open'
                        """
                    ),
                    {"sid": strategy_id, "sym": symbol},
                ).scalar()
            finally:
                s.close()
        except Exception as exc:
            print(f"  [WARN] journal_opened_at {strategy_id}/{symbol}: {exc}", flush=True)
            return None
        if row is None:
            return None
        if getattr(row, "tzinfo", None) is None:
            return row.replace(tzinfo=timezone.utc)
        return row

    def _maybe_time_exits(self, strategies: list[dict], report_lines: list[str]) -> None:
        """Sell sleeve lots that reached params.max_hold_days (NYSE sessions, ET)."""
        if self.sleeve is None or self.dry_run:
            return
        now = datetime.now(ET)
        for strat in strategies:
            params = strat.get("params") or {}
            try:
                max_hold = int(params.get("max_hold_days") or 0)
            except (TypeError, ValueError):
                max_hold = 0
            if max_hold <= 0:
                continue
            sid = strat["id"]
            name = strat.get("name") or sid
            exempt = {
                str(x).upper()
                for x in (params.get("time_exit_exempt") or [])
            }
            for sym in sorted(self.sleeve.symbols_of(sid)):
                if sym in exempt:
                    continue
                opened = self._journal_opened_at(sid, sym)
                if opened is None:
                    continue
                age = hold_trading_days(opened, now)
                if age < max_hold:
                    continue
                owned = int(float(self.sleeve.qty_of(sid, sym)))
                pos = self.existing_positions.get(sym)
                if not pos or owned <= 0:
                    continue
                sell_qty = min(owned, int(float(pos.get("qty") or 0)))
                if sell_qty <= 0:
                    continue
                reason = f"time_exit {age} trading_d >= max_hold_days={max_hold}"
                report_lines.append(
                    f"  TIME_EXIT {name} {sym} qty={sell_qty} {reason}"
                )
                try:
                    result = self.client.unlock_and_sell(sym, sell_qty)
                except Exception as exc:
                    report_lines.append(f"         -> TIME_EXIT FAILED {sym}: {exc}")
                    self.errors.append(f"time_exit {sym}: {exc}")
                    continue
                fill = await_fill(self.client, {**result, "qty": sell_qty})
                report_lines.append(f"         -> {fill.describe()}")
                if not fill.filled:
                    continue
                qty = int(fill.filled_qty)
                px = fill.price_or(float(pos.get("current_price") or 0))
                self.sleeve.reduce(sid, sym, qty)
                record_exit(
                    sid,
                    name,
                    sym,
                    qty,
                    px,
                    order_id=result.get("id"),
                    status=fill.status,
                    reason=reason,
                    filled_qty=float(qty),
                )
                remaining = self.sleeve.qty_of(sid, sym)
                if remaining > 0:
                    self.existing_positions[sym]["qty"] = float(remaining)
                else:
                    self.existing_positions.pop(sym, None)
                self.buying_power += qty * px
        try:
            self.existing_positions = {
                p["symbol"]: p for p in self.client.positions()
            }
        except Exception:
            pass

    def run_force_order(self, symbol: str, side: str) -> str:
        """Force a test order for acceptance testing.

        Submits a real Alpaca paper order of 1 share,
        logs to orders + transactions + trade_journal + runner_history.
        """
        t0 = time.time()
        report_lines = []
        report_lines.append(f"{'='*60}")
        report_lines.append(f"FORCE ORDER — Acceptance Test")
        report_lines.append(f"{'='*60}")
        report_lines.append(f"  Symbol: {symbol} | Side: {side}")
        report_lines.append(f"  Equity: ${self.equity:,.2f}")

        qty = 1
        order_status = 'submitted'
        try:
            # Get current price
            df = load_ohlcv(symbol, start=(datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
            df.columns = [c.lower() for c in df.columns]
            current_price = float(df['close'].iloc[-1])
            report_lines.append(f"  Price: ${current_price:.2f}")

            if self.dry_run:
                report_lines.append(f"  [DRY-RUN] Would submit {side} {qty} {symbol} @ ${current_price:.2f}")
                report = '\n'.join(report_lines)
                print(report)
                return report

            ensure_strategy_in_db({
                'id': 'acceptance-test',
                'name': 'Acceptance Test',
                'type': 'test',
                'enabled': False,
            })

            # Submit real order.
            # combined_position_cap 校验有意不接入：这是 acceptance 测试专用路径，
            # 不应被自动风控拦住；勿复用此路径做真实策略下单。
            if side.lower() == 'sell':
                result = self.client.unlock_and_sell(symbol, qty)
            else:
                result = self.client.market_order(symbol, qty, side)
            order_id = result.get('id', str(uuid.uuid4()))
            order_status = result.get('status', 'submitted')

            report_lines.append(f"  Order ID: {order_id}")
            report_lines.append(f"  Status: {order_status}")
            report_lines.append(f"  Filled qty: {result.get('filled_qty', 0)}")

            # Log to orders
            log_to_db('orders', {
                'id': order_id,
                'symbol': symbol,
                'qty': float(qty),
                'filled_qty': float(result.get('filled_qty', 0) or 0),
                'side': side,
                'type': 'market',
                'status': order_status,
                'strategy_id': 'acceptance-test',
                'submitted_at': self.now.isoformat(),
                'created_at': self.now.isoformat(),
            })

            # Log to transactions
            log_to_db('transactions', {
                'id': str(uuid.uuid4())[:12],
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'qty': float(qty),
                'price': current_price,
                'fee': current_price * qty * 0.0001,
                'strategy_id': 'acceptance-test',
                'created_at': self.now.isoformat(),
            })

            # Log to trade_journal
            log_to_db('trade_journal', {
                'trade_id': str(uuid.uuid4())[:12],
                'strategy_id': 'acceptance-test',
                'strategy_name': 'Acceptance Test',
                'symbol': symbol,
                'side': side,
                'signal_reason': f'forced {side} for acceptance test',
                'entry_price': current_price if side == 'buy' else None,
                'exit_price': current_price if side == 'sell' else None,
                'qty': float(qty),
                'risk_pct': 0,
                'position_value_pct': round(qty * current_price / self.equity * 100, 2),
                'status': 'open' if side == 'buy' else 'closed',
                'opened_at': self.now.isoformat(),
                'created_at': self.now.isoformat(),
            })

            report_lines.append(f"\n  [PASS] Order submitted and logged to all 3 tables")
            report_lines.append(f"    orders:         order_id={order_id}")
            report_lines.append(f"    transactions:   logged")
            report_lines.append(f"    trade_journal:  logged")

        except Exception as e:
            report_lines.append(f"\n  [FAIL] Order failed: {e}")
            self.errors.append(str(e))

        elapsed = time.time() - t0
        report_lines.append(f"\n  Runtime: {elapsed:.1f}s")
        report_lines.append(f"{'='*60}")

        # Log to runner_history
        log_runner_history({
            'mode': f'force-{side}',
            'strategies_loaded': 0,
            'symbols_scanned': 1,
            'signal_buy': 1 if side == 'buy' else 0,
            'signal_sell': 1 if side == 'sell' else 0,
            'signal_hold': 0,
            'risk_rejected': 0,
            'orders_submitted': 1,
            'orders_filled': 1 if order_status == 'filled' else 0,
            'orders_failed': 0,
            'halt_triggered': False,
            'runtime_seconds': elapsed,
            'errors': '; '.join(self.errors) if self.errors else None,
            'version': RUNNER_VERSION,
        })

        report = '\n'.join(report_lines)
        print(report)
        return report

    def _build_signal(
        self,
        strat_id: str,
        strat_name: str,
        symbol: str,
        df: pd.DataFrame,
        code: str,
        params: dict,
        halt: bool,
        report_lines: list,
    ) -> dict[str, Any] | None:
        """Generate signal for one strategy x symbol; return actionable dict or None."""
        close = df['close'].astype(float)
        # Keep only complete bars ??developing session often has NaN close at 15:50
        valid = close.notna() & (close > 0)
        if int(valid.sum()) < 60:
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} HOLD  price=n/a [SKIP insufficient bars]"
            )
            return None
        df_ok = df.loc[valid].copy()
        close_ok = close.loc[valid]

        # T-1 only when last bar is *today*; otherwise use full complete history
        last_ts = df_ok.index[-1]
        last_date = last_ts.date() if hasattr(last_ts, "date") else last_ts
        today = datetime.now(ET).date()
        if last_date == today and len(close_ok) >= 2:
            close_until = close_ok.iloc[:-1]
            df_sig = df_ok.iloc[:-1]
        else:
            close_until = close_ok
            df_sig = df_ok
        if len(close_until) < 60:
            return None

        run_params = dict(params or {})
        for src_key, dst_key in (
            ("high", "_high"),
            ("low", "_low"),
            ("open", "_open"),
            ("volume", "_volume"),
        ):
            if src_key in df_sig.columns:
                run_params[dst_key] = df_sig[src_key].astype(float)

        entries, exits = run_signal_fn(close_until, code, run_params)
        has_entry = bool(entries.iloc[-1]) if len(entries) > 0 else False
        has_exit = bool(exits.iloc[-1]) if len(exits) > 0 else False

        if 'high' in df_sig.columns and 'low' in df_sig.columns:
            atr = compute_atr(df_sig['high'], df_sig['low'], df_sig['close']).iloc[-1]
        else:
            atr = float(close_ok.iloc[-1]) * 0.02
        if pd.isna(atr) or atr <= 0:
            atr = float(close_ok.iloc[-1]) * 0.02

        # Prefer live quote for sizing; fall back to last complete close
        current_price = float(close_ok.iloc[-1])
        try:
            from data.loader import get_latest_price
            live = float(get_latest_price(symbol))
            if live == live and live > 0:
                current_price = live
        except Exception:
            pass
        # Ownership, not mere presence in the account: a strategy may only exit
        # what its own sleeve opened, and may only enter a symbol nobody holds.
        owner = self.sleeve.owner_of(symbol) if self.sleeve else None
        has_position = owner == strat_id
        held_by_other = owner is not None and owner != strat_id

        if pd.isna(current_price) or current_price <= 0:
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} HOLD  price=n/a [SKIP bad price]"
            )
            return None

        if has_entry and held_by_other:
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} HOLD  [SKIP] owned by {owner}"
            )
            return None
        if has_exit and held_by_other:
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} HOLD  [SKIP] exit ignored, "
                f"position belongs to {owner}"
            )
            return None

        if has_entry and not has_position:
            signal = 'BUY'
        elif has_exit and has_position:
            signal = 'SELL'
        else:
            signal = 'HOLD'

        sig_record = {
            'strategy_id': strat_id,
            'strategy_name': strat_name,
            'symbol': symbol,
            'side': signal.lower(),
            'price': current_price,
            'atr': float(atr) if not pd.isna(atr) else None,
            'reason': f"entry={has_entry}, exit={has_exit}, pos={has_position}",
        }
        self.signals_generated.append(sig_record)

        if signal == 'HOLD':
            report_lines.append(f"  {symbol:6s} {strat_name:20s} HOLD  price=${current_price:.2f}")
            return None

        if halt and signal == 'BUY':
            report_lines.append(f"  {symbol:6s} {strat_name:20s} BUY [HALT BLOCKED]")
            self.orders_rejected.append({**sig_record, 'reject_reason': 'daily_halt'})
            return None

        # Archive wind-down: block new opens, still allow SELL / bracket exits.
        if signal == 'BUY' and not bool((params or {}).get('allow_new_entries', True)):
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} BUY [WIND_DOWN — allow_new_entries=false]"
            )
            self.orders_rejected.append({**sig_record, 'reject_reason': 'wind_down'})
            return None

        # Position sizing (final BP clamp happens at execute time)
        if signal == 'BUY':
            atr_qty = int(self.equity * (RISK_PER_TRADE_PCT / 100) / (float(atr) * ATR_STOP_MULTIPLIER)) if atr and not pd.isna(atr) and atr > 0 else 0
            # ERC portfolio weight → per-name capital budget (fallback: flat cap)
            weight = (params or {}).get('portfolio_weight')
            if weight is not None:
                try:
                    budget_pct = min(float(weight) * 100.0 * PORTFOLIO_GROSS, SINGLE_NAME_CAP_PCT)
                except (TypeError, ValueError):
                    budget_pct = MAX_POSITION_PCT
            else:
                budget_pct = MAX_POSITION_PCT
            cap_qty = int(self.equity * (budget_pct / 100) / current_price)
            qty = min(atr_qty, cap_qty) if atr_qty > 0 else cap_qty
            if qty <= 0:
                report_lines.append(f"  {symbol:6s} {strat_name:20s} BUY [SKIP] qty=0")
                return None
        else:
            # Sell only this sleeve's share, never the whole broker position
            pos = self.existing_positions.get(symbol)
            broker_qty = int(float(pos['qty'])) if pos else 0
            sleeve_qty = int(self.sleeve.qty_of(strat_id, symbol)) if self.sleeve else 0
            qty = min(sleeve_qty, broker_qty)
            if qty <= 0:
                report_lines.append(f"  {symbol:6s} {strat_name:20s} SELL [SKIP] no position")
                return None

        position_pct = (qty * current_price / self.equity * 100) if self.equity > 0 else 0
        sig_record['qty'] = qty
        sig_record['position_pct'] = position_pct

        zones = None
        if signal == 'BUY' and is_mimo_meanrev(strat_name, params):
            try:
                zones = mimo_price_zones(
                    df_sig,
                    last_price=current_price,
                    atr=float(atr),
                    params=params,
                )
                # Re-size off limit price (capital at intended fill, not chase print)
                lim = float(zones['limit_price'])
                if lim > 0:
                    weight = (params or {}).get('portfolio_weight')
                    if weight is not None:
                        try:
                            budget_pct = min(float(weight) * 100.0 * PORTFOLIO_GROSS, SINGLE_NAME_CAP_PCT)
                        except (TypeError, ValueError):
                            budget_pct = MAX_POSITION_PCT
                    else:
                        budget_pct = MAX_POSITION_PCT
                    atr_qty2 = int(self.equity * (RISK_PER_TRADE_PCT / 100) / max(float(atr) * ATR_STOP_MULTIPLIER, 1e-6))
                    cap_qty2 = int(self.equity * (budget_pct / 100) / lim)
                    qty = min(atr_qty2, cap_qty2) if atr_qty2 > 0 else cap_qty2
                    if qty <= 0:
                        report_lines.append(f"  {symbol:6s} {strat_name:20s} BUY [SKIP] zone qty=0")
                        return None
                    sig_record['qty'] = qty
                    position_pct = (qty * lim / self.equity * 100) if self.equity > 0 else 0
                    sig_record['position_pct'] = position_pct
                sig_record['zones'] = zones
                bz, sz = zones['buy_zone'], zones['sell_zone']
                report_lines.append(
                    f"  {symbol:6s} {strat_name:20s} BUY qty={qty} "
                    f"px=${current_price:.2f} pos={position_pct:.1f}% "
                    f"| buy[{bz[0]:.2f}-{bz[1]:.2f}] limit=${zones['limit_price']:.2f} "
                    f"sell[{sz[0]:.2f}-{sz[1]:.2f}] SL=${zones['stop_loss']:.2f} TP=${zones['take_profit']:.2f}"
                )
            except Exception as e:
                report_lines.append(
                    f"  {symbol:6s} {strat_name:20s} BUY qty={qty} "
                    f"price=${current_price:.2f} pos={position_pct:.1f}% [zone-fail {e}]"
                )
                zones = None
        else:
            report_lines.append(
                f"  {symbol:6s} {strat_name:20s} {signal} qty={qty} "
                f"price=${current_price:.2f} pos={position_pct:.1f}%"
            )

        out = {
            **sig_record,
            'signal': signal,
            'qty': qty,
            'price': current_price,
        }
        if zones:
            out['zones'] = zones
            out['order_style'] = 'mimo_limit_bracket'
        return out

    def _resolve_actions(self, pending: list[dict[str, Any]], report_lines: list) -> list[dict[str, Any]]:
        """Keep one action per symbol; SELL wins over BUY; first strategy wins ties."""
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for a in pending:
            by_symbol.setdefault(a['symbol'], []).append(a)

        resolved: list[dict[str, Any]] = []
        for symbol, actions in by_symbol.items():
            sells = [a for a in actions if a['signal'] == 'SELL']
            buys = [a for a in actions if a['signal'] == 'BUY']
            chosen = None
            if sells:
                chosen = sells[0]
                for extra in sells[1:] + buys:
                    extra['skipped_conflict'] = True
                    for sig in self.signals_generated:
                        if (
                            sig['strategy_id'] == extra['strategy_id']
                            and sig['symbol'] == symbol
                            and sig['side'] == extra['side']
                        ):
                            sig['skipped_conflict'] = True
                    report_lines.append(
                        f"  {symbol:6s} {extra['strategy_name']:20s} {extra['signal']} "
                        f"[SKIP] conflict — kept {chosen['strategy_name']} {chosen['signal']}"
                    )
                    self.orders_rejected.append({**extra, 'reject_reason': 'symbol_conflict'})
            elif buys:
                chosen = buys[0]
                for extra in buys[1:]:
                    extra['skipped_conflict'] = True
                    for sig in self.signals_generated:
                        if (
                            sig['strategy_id'] == extra['strategy_id']
                            and sig['symbol'] == symbol
                            and sig['side'] == 'buy'
                        ):
                            sig['skipped_conflict'] = True
                    report_lines.append(
                        f"  {symbol:6s} {extra['strategy_name']:20s} BUY "
                        f"[SKIP] multi-strategy same ticker — kept {chosen['strategy_name']}"
                    )
                    self.orders_rejected.append({**extra, 'reject_reason': 'multi_strategy_same_ticker'})
            if chosen:
                resolved.append(chosen)

        # Execute sells before buys so BP frees up
        resolved.sort(key=lambda a: 0 if a['signal'] == 'SELL' else 1)
        if resolved:
            report_lines.append(f"\n--- Execute ({len(resolved)} after conflict resolve) ---")
        return resolved

    def _execute_action(self, action: dict[str, Any], report_lines: list) -> None:
        """Submit one resolved action with shared-account guards."""
        symbol = action['symbol']
        signal = action['signal']
        strat_id = action['strategy_id']
        strat_name = action['strategy_name']
        current_price = float(action['price'])
        qty = int(action['qty'])

        if symbol in self._acted_symbols:
            report_lines.append(f"  {symbol:6s} {strat_name:20s} {signal} [REJECT] already acted this run")
            self.orders_rejected.append({**action, 'reject_reason': 'already_acted'})
            return

        if self.dry_run:
            zones = action.get('zones')
            if zones:
                bz, sz = zones['buy_zone'], zones['sell_zone']
                report_lines.append(
                    f"         -> [DRY-RUN] MiMo LIMIT BRACKET qty={qty} "
                    f"buy[{bz[0]:.2f}-{bz[1]:.2f}] limit=${zones['limit_price']:.2f} "
                    f"sell[{sz[0]:.2f}-{sz[1]:.2f}] SL=${zones['stop_loss']:.2f} "
                    f"TP=${zones['take_profit']:.2f}"
                )
            else:
                report_lines.append(f"         -> [DRY-RUN] order not submitted")
            self._acted_symbols.add(symbol)
            return

        if signal == 'BUY':
            # Refresh BP from local tracker; clamp qty
            max_by_bp = int(self.buying_power / current_price) if current_price > 0 else 0
            if max_by_bp <= 0:
                report_lines.append(
                    f"         -> BUY [SKIP] buying power insufficient "
                    f"(BP=${self.buying_power:,.2f})"
                )
                self.orders_rejected.append({**action, 'reject_reason': 'buying_power'})
                return
            if qty > max_by_bp:
                report_lines.append(
                    f"         -> BUY qty clamped {qty} -> {max_by_bp} (BP=${self.buying_power:,.2f})"
                )
                qty = max_by_bp
            if symbol in self.existing_positions:
                report_lines.append(f"         -> BUY [SKIP] already holding {symbol}")
                self.orders_rejected.append({**action, 'reject_reason': 'already_holding'})
                return

            gross = sum(
                abs(float(p.get('qty') or 0)) * float(p.get('current_price') or 0)
                for p in self.existing_positions.values()
            )
            if self.equity > 0 and (gross + qty * current_price) / self.equity * 100 > MAX_GROSS_EXPOSURE_PCT:
                report_lines.append(
                    f"         -> BUY [SKIP] gross exposure "
                    f"{gross / self.equity * 100:.0f}% + this order exceeds "
                    f"{MAX_GROSS_EXPOSURE_PCT:.0f}%"
                )
                self.orders_rejected.append({**action, 'reject_reason': 'gross_exposure'})
                return
            acct_cap = check_account_buy(self.client, float(qty) * float(current_price))
            if not acct_cap.allowed:
                report_lines.append(
                    f"         -> BUY [SKIP] account cap ({acct_cap.reason})"
                )
                self.orders_rejected.append(
                    {**action, 'reject_reason': 'account_exposure'}
                )
                return

            # Claim before ordering so the intraday process cannot race us onto
            # the same symbol; released again if the broker rejects the order.
            if self.sleeve is None or not self.sleeve.claim(
                strat_id, symbol, qty, current_price
            ):
                owner = self.sleeve.owner_of(symbol) if self.sleeve else 'ledger-down'
                report_lines.append(f"         -> BUY [SKIP] sleeve claim failed ({owner})")
                self.orders_rejected.append({**action, 'reject_reason': 'sleeve_claim'})
                return

            # Cross-process cap: broker position MV + open buys + this order.
            cap = check_combined_buy(
                self.client,
                symbol,
                float(qty) * float(current_price),
                ref_price=float(current_price),
            )
            if not cap.allowed:
                self.sleeve.release(strat_id, symbol)
                report_lines.append(
                    f"         -> BUY [SKIP] combined position cap "
                    f"({cap.reason})"
                )
                self.orders_rejected.append(
                    {**action, 'reject_reason': 'combined_position_cap'}
                )
                return

            market_bracket_pcts: tuple[float, float] | None = None
            signal_px = current_price
            try:
                zones = action.get('zones')
                if zones and action.get('order_style') == 'mimo_limit_bracket':
                    # Replace any resting buy/bracket on this name before re-arming dip limit
                    canceled = self.client.cancel_open_orders(symbol)
                    if canceled:
                        report_lines.append(
                            f"         -> canceled {canceled} open order(s) before MiMo limit"
                        )
                    result = self.client.limit_bracket_order(
                        symbol,
                        qty,
                        limit_price=float(zones['limit_price']),
                        stop_loss=float(zones['stop_loss']),
                        take_profit=float(zones['take_profit']),
                        side='buy',
                    )
                    bz, sz = zones['buy_zone'], zones['sell_zone']
                    report_lines.append(
                        f"         -> MIMO LIMIT BRACKET "
                        f"buy[{bz[0]:.2f}-{bz[1]:.2f}] @{result.get('limit_price')} "
                        f"sell[{sz[0]:.2f}-{sz[1]:.2f}] "
                        f"SL={result.get('stop_loss')} TP={result.get('take_profit')}"
                    )
                    fill_ref = float(zones['limit_price'])
                else:
                    atr_v = action.get('atr')
                    sl_pct, tp_pct = _bracket_pcts(current_price, atr_v)
                    market_bracket_pcts = (sl_pct, tp_pct)
                    result = self.client.bracket_order(
                        symbol,
                        qty,
                        entry_price=current_price,
                        stop_loss_pct=sl_pct,
                        take_profit_pct=tp_pct,
                        side='buy',
                    )
                    report_lines.append(
                        f"         -> BRACKET SL={result.get('stop_loss')} "
                        f"TP={result.get('take_profit')} ({sl_pct:.1%}/{tp_pct:.1%})"
                    )
                    fill_ref = current_price
            except Exception as e:
                self.sleeve.release(strat_id, symbol)
                report_lines.append(f"         -> ORDER FAILED: {e}")
                self.orders_failed.append({**action, 'error': str(e)})
                self.errors.append(f"Order failed {symbol}: {e}")
                return

            # Ask the broker what the order actually did before writing it down.
            fill = await_fill(self.client, result)
            report_lines.append(f"         -> {fill.describe()}")
            if fill.dead:
                self.sleeve.release(strat_id, symbol)
                report_lines.append("         -> claim released, nothing filled")
                self.orders_failed.append({**action, 'error': f'no fill ({fill.status})'})
                return
            if fill.pending:
                # A resting limit is the intended behaviour for MiMo dips. Keep
                # the claim so nobody else takes the symbol while it waits, and
                # journal it now: reconcile cannot open the row later because
                # the claim already carries the full quantity, so a fill would
                # otherwise never appear in the journal at all. A limit fills at
                # its price or better, so fill_ref is a sound entry mark. If it
                # never fills, the claim ages out and mark_stale_journal retires
                # the row.
                self.orders_submitted.append(result)
                self._acted_symbols.add(symbol)
                record_entry(
                    strategy_id=strat_id,
                    strategy_name=strat_name,
                    symbol=symbol,
                    qty=float(qty),
                    price=fill_ref,
                    order_id=result.get('id', str(uuid.uuid4())),
                    status=fill.status,
                    reason=action.get('reason', ''),
                    order_type='limit',
                    filled_qty=0.0,
                    atr=action.get('atr'),
                    risk_pct=RISK_PER_TRADE_PCT,
                    position_value_pct=action.get('position_pct'),
                )
                report_lines.append("         -> journalled as pending, claim held")
                return

            qty = int(fill.filled_qty)
            fill_ref = fill.price_or(fill_ref)
            result['filled_qty'] = fill.filled_qty
            result['status'] = fill.status
            if market_bracket_pcts is not None:
                note = reprice_protection(
                    self.client,
                    symbol,
                    qty,
                    fill_ref,
                    market_bracket_pcts[0],
                    market_bracket_pcts[1],
                    signal_price=signal_px,
                )
                if note:
                    report_lines.append(f"         -> {note}")
            notional = qty * fill_ref
            self.buying_power = max(0.0, self.buying_power - notional)
            self.sleeve.claim(strat_id, symbol, qty, fill_ref)
            self.existing_positions[symbol] = {
                'symbol': symbol,
                'qty': float(qty),
                'qty_available': float(qty),
                'current_price': current_price,
            }
        else:
            try:
                result = self.client.unlock_and_sell(symbol, qty)
                canceled = result.get('canceled_orders', 0)
                if canceled:
                    report_lines.append(f"         -> canceled {canceled} open order(s) on {symbol}")
                qty = int(float(result.get('qty') or qty))
            except Exception as e:
                report_lines.append(f"         -> ORDER FAILED: {e}")
                self.orders_failed.append({**action, 'error': str(e)})
                self.errors.append(f"Order failed {symbol}: {e}")
                return

            fill = await_fill(self.client, {**result, 'qty': qty})
            report_lines.append(f"         -> {fill.describe()}")
            if fill.dead:
                report_lines.append("         -> nothing sold, ledger untouched")
                self.orders_failed.append({**action, 'error': f'no fill ({fill.status})'})
                return
            # Reduce by what left the account, not by what was asked for; a
            # partial sell that books the full quantity loses track of shares
            # still held.
            if fill.filled:
                qty = int(fill.filled_qty)
            exit_price = fill.price_or(current_price)
            result['filled_qty'] = fill.filled_qty
            result['status'] = fill.status

            if self.sleeve is not None:
                self.sleeve.reduce(strat_id, symbol, qty)
            remaining = self.sleeve.qty_of(strat_id, symbol) if self.sleeve else 0
            if remaining > 0:
                self.existing_positions[symbol]['qty'] = float(remaining)
            else:
                self.existing_positions.pop(symbol, None)
            # Free BP approximately (cash returns after sell)
            self.buying_power += qty * exit_price

        self._acted_symbols.add(symbol)
        self.orders_submitted.append(result)
        order_status = result.get('status', 'submitted')
        if order_status == 'filled':
            self.orders_filled.append(result)

        order_id = result.get('id', str(uuid.uuid4()))
        common = {
            'strategy_id': strat_id,
            'strategy_name': strat_name,
            'symbol': symbol,
            'qty': float(qty),
            # The confirmed fill, falling back to the signal price only when the
            # broker did not report one.
            'price': fill_ref if signal == 'BUY' else exit_price,
            'order_id': order_id,
            'status': order_status,
            'reason': action.get('reason', ''),
            'filled_qty': float(result.get('filled_qty', 0) or 0),
            'order_type': 'limit' if action.get('order_style') == 'mimo_limit_bracket' else 'market',
        }
        if signal == 'BUY':
            record_entry(
                **common,
                atr=action.get('atr'),
                risk_pct=RISK_PER_TRADE_PCT,
                position_value_pct=action.get('position_pct'),
            )
        else:
            closed = record_exit(**common)
            report_lines.append(f"         -> journal closed {closed} row(s)")
        report_lines.append(f"         -> ORDER {order_status.upper()}: {order_id}")

    def _check_daily_loss_halt(self) -> bool:
        """Halt the day once the account is down more than the threshold.

        This used to net the day's buy and sell notional out of the `orders`
        table and call the difference a loss. Buying is not a loss, so a day
        that deployed cash looked catastrophic — and it never fired anyway,
        because it filtered on status='filled' while the table only ever gets
        'pending_new' rows with a NULL filled_avg_price.

        The broker's own equity mark is the honest number: `last_equity` is the
        previous session's close, so this is real P&L including open positions.
        """
        try:
            account = self.client.account()
            equity = float(account["equity"])
            last_equity = float(account.get("last_equity") or 0)
        except Exception as exc:
            # A risk control that cannot evaluate must not wave trading through.
            self.errors.append(f"Daily loss check failed, halting: {exc}")
            print(f"  [HALT] cannot read account to evaluate daily loss: {exc}")
            return True

        if last_equity <= 0:
            return False
        change_pct = (equity - last_equity) / last_equity * 100
        if change_pct <= -abs(DAILY_LOSS_HALT_PCT):
            print(
                f"  [HALT] Daily P&L {change_pct:.2f}% <= -{abs(DAILY_LOSS_HALT_PCT)}% "
                f"(equity ${equity:,.0f} vs ${last_equity:,.0f})"
            )
            return True
        return False

    def _print_and_log_history(self, report: str, n_strategies: int, n_symbols: int, mode: str, elapsed: float) -> None:
        """Print report and log to runner_history."""
        print(report)

        sig_buy = sum(1 for s in self.signals_generated if s['side'] == 'buy')
        sig_sell = sum(1 for s in self.signals_generated if s['side'] == 'sell')
        sig_hold = sum(1 for s in self.signals_generated if s['side'] == 'hold')

        log_runner_history({
            'mode': mode,
            'strategies_loaded': n_strategies,
            'symbols_scanned': n_symbols,
            'signal_buy': sig_buy,
            'signal_sell': sig_sell,
            'signal_hold': sig_hold,
            'risk_rejected': len(self.orders_rejected),
            'orders_submitted': len(self.orders_submitted),
            'orders_filled': len(self.orders_filled),
            'orders_failed': len(self.orders_failed),
            'halt_triggered': bool(self.halt_triggered),
            'runtime_seconds': round(elapsed, 2),
            'errors': '; '.join(self.errors) if self.errors else None,
            'version': RUNNER_VERSION,
        })


def main():
    parser = argparse.ArgumentParser(description='Phase 6.2 Paper Trading Runner')
    parser.add_argument('--symbol', default=None, help='Single symbol to process')
    parser.add_argument('--dry-run', action='store_true', help='Generate signals only, no orders')
    parser.add_argument('--mode', choices=['daily', 'force-buy', 'force-sell'], default='daily',
                        help='Run mode: daily (default), force-buy/force-sell (acceptance test)')
    args = parser.parse_args()

    # Ensure runner_history table exists
    ensure_runner_history_table()

    symbol = args.symbol or 'AAPL'

    if args.mode in ('force-buy', 'force-sell'):
        side = 'buy' if args.mode == 'force-buy' else 'sell'
        runner = PaperTradingRunner(dry_run=args.dry_run)
        runner.run_force_order(symbol, side)
    else:
        symbols = [symbol] if args.symbol else None
        runner = PaperTradingRunner(dry_run=args.dry_run)
        runner.run(symbols, mode='daily')


if __name__ == '__main__':
    main()
