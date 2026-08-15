"""
Strategy position ledger ("sleeves")
====================================
The paper/live account is shared by several runner processes (phase6_runner,
intraday_runner). Asking the broker "do I hold MSFT?" cannot tell you *which*
strategy opened it, so an exit rule belonging to strategy A used to liquidate a
position that strategy B had just opened.

This ledger records, per strategy, the positions that strategy owns. Normally a
symbol has one owner (claim is exclusive). Shared lots are allowed when a
repair explicitly `claim(..., coexist=True)` — e.g. restoring a force-sold lot
alongside another strategy's live holding. Exits always use `qty_of(strategy)`,
never the full broker size.

Broker-side bracket legs (stop-loss / take-profit) close positions without any
runner knowing, so `reconcile()` must run at the start of every scan to sync
the ledger back to broker truth. Reconcile compares broker qty to the *sum* of
sleeve lots on that symbol.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

TABLE = "strategy_positions"

# Positions held at the broker that no sleeve claims (manual trades, imports).
# No strategy is allowed to trade these; they are reported and left alone.
UNASSIGNED = "__unassigned__"

# A claim younger than this is never cleared by reconcile(), so a resting limit
# order that has not filled yet does not free the symbol for another sleeve.
CLAIM_GRACE_SEC = 3600


class LedgerUnavailable(RuntimeError):
    """Raised when the ledger cannot be reached; callers must not trade blind."""


@dataclass
class SleevePosition:
    strategy_id: str
    symbol: str
    qty: float
    avg_price: float


@dataclass
class LedgerChange:
    """One adjustment reconcile() made, in a form callers can act on.

    `closed` and `reduced` are the interesting ones: shares left the account
    without a runner selling them, which in practice means a bracket leg fired.
    """

    kind: str  # closed | reduced | increased | pending | orphaned
    strategy_id: str
    symbol: str
    prev_qty: float
    new_qty: float
    avg_price: float = 0.0
    # When the ledger row was last touched. Settlement uses it as the lower
    # bound when hunting for the fills that closed this position, so an earlier
    # round-trip in the same symbol cannot contaminate the exit price.
    since: datetime | None = None

    @property
    def qty_gone(self) -> float:
        return max(0.0, self.prev_qty - self.new_qty)

    def __str__(self) -> str:
        if self.kind in ("closed", "pending", "orphaned"):
            return f"{self.strategy_id}/{self.symbol}"
        return f"{self.strategy_id}/{self.symbol} {self.prev_qty:g}->{self.new_qty:g}"


def _session():
    try:
        from core.db import SyncSessionLocal
    except Exception as exc:  # pragma: no cover - import guard
        raise LedgerUnavailable(f"core.db import failed: {exc}") from exc
    return SyncSessionLocal()


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


def ensure_schema() -> None:
    """Create the ledger table. Drop exclusive-owner index if present (shared lots)."""
    stmts = [
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            strategy_id VARCHAR(128)     NOT NULL,
            symbol      VARCHAR(20)      NOT NULL,
            qty         DOUBLE PRECISION NOT NULL DEFAULT 0,
            avg_price   DOUBLE PRECISION NOT NULL DEFAULT 0,
            source      VARCHAR(32)      NOT NULL DEFAULT 'runner',
            opened_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
            PRIMARY KEY (strategy_id, symbol)
        )
        """,
        f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_symbol ON {TABLE} (symbol)",
        # Shared lots (repair coexist) need multiple qty>0 rows per symbol.
        f"DROP INDEX IF EXISTS uq_{TABLE}_owner",
    ]
    try:
        s = _session()
    except LedgerUnavailable:
        raise
    try:
        for sql in stmts:
            s.execute(_text(sql))
        s.commit()
    except Exception as exc:
        s.rollback()
        raise LedgerUnavailable(f"schema init failed: {exc}") from exc
    finally:
        s.close()


class SleeveBook:
    """In-memory view of the ledger, kept in step with the database."""

    def __init__(self, rows: Iterable[SleevePosition] = ()) -> None:
        # (strategy_id, symbol) -> position; multiple strategies may share a symbol.
        self._by_key: dict[tuple[str, str], SleevePosition] = {}
        for r in rows:
            if r.qty > 0:
                self._by_key[(r.strategy_id, r.symbol)] = r

    # ── loading ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "SleeveBook":
        ensure_schema()
        s = _session()
        try:
            rows = s.execute(
                _text(
                    f"SELECT strategy_id, symbol, qty, avg_price FROM {TABLE} WHERE qty > 0"
                )
            ).fetchall()
        except Exception as exc:
            s.rollback()
            raise LedgerUnavailable(f"load failed: {exc}") from exc
        finally:
            s.close()
        return cls(
            SleevePosition(str(r[0]), str(r[1]), float(r[2]), float(r[3])) for r in rows
        )

    # ── queries ──────────────────────────────────────────────────────────

    def owners_of(self, symbol: str) -> list[SleevePosition]:
        return [p for (sid, sym), p in self._by_key.items() if sym == symbol]

    def owner_of(self, symbol: str) -> str | None:
        """Primary owner for exclusive-claim checks. None if unowned.

        If multiple strategies share the symbol, returns the first by strategy_id
        so exclusive `claim()` still blocks a third party from entering blindly.
        """
        owners = self.owners_of(symbol)
        if not owners:
            return None
        return sorted(owners, key=lambda p: p.strategy_id)[0].strategy_id

    def qty_of(self, strategy_id: str, symbol: str) -> float:
        pos = self._by_key.get((strategy_id, symbol))
        return pos.qty if pos else 0.0

    def symbols_of(self, strategy_id: str) -> set[str]:
        return {sym for (sid, sym), p in self._by_key.items() if sid == strategy_id}

    def holdings(self) -> dict[str, SleevePosition]:
        """Legacy map symbol -> one position (first owner). Prefer owners_of()."""
        out: dict[str, SleevePosition] = {}
        for p in self._by_key.values():
            if p.symbol not in out:
                out[p.symbol] = p
        return out

    def all_holdings(self) -> list[SleevePosition]:
        return list(self._by_key.values())

    def orphans(self) -> set[str]:
        return {sym for (sid, sym) in self._by_key if sid == UNASSIGNED}

    # ── mutations ────────────────────────────────────────────────────────

    def claim(
        self,
        strategy_id: str,
        symbol: str,
        qty: float,
        price: float,
        source: str = "runner",
        *,
        coexist: bool = False,
    ) -> bool:
        """Reserve `symbol` for `strategy_id`.

        Default: exclusive — False when another strategy already holds the symbol.
        `coexist=True`: allow a second lot (repair / restore). Same strategy always
        updates its own row.
        """
        others = [
            p for p in self.owners_of(symbol) if p.strategy_id != strategy_id
        ]
        if others and not coexist:
            return False

        s = _session()
        try:
            s.execute(
                _text(
                    f"""
                    INSERT INTO {TABLE}
                        (strategy_id, symbol, qty, avg_price, source, opened_at, updated_at)
                    VALUES (:sid, :sym, :qty, :px, :src, NOW(), NOW())
                    ON CONFLICT (strategy_id, symbol) DO UPDATE SET
                        qty        = EXCLUDED.qty,
                        avg_price  = EXCLUDED.avg_price,
                        source     = EXCLUDED.source,
                        updated_at = NOW()
                    """
                ),
                {
                    "sid": strategy_id,
                    "sym": symbol,
                    "qty": float(qty),
                    "px": float(price),
                    "src": source,
                },
            )
            s.commit()
        except Exception as exc:
            s.rollback()
            logger.warning("sleeve claim rejected %s/%s: %s", strategy_id, symbol, exc)
            return False
        finally:
            s.close()

        self._by_key[(strategy_id, symbol)] = SleevePosition(
            strategy_id, symbol, float(qty), float(price)
        )
        return True

    def release(self, strategy_id: str, symbol: str) -> None:
        """Drop a claim entirely (order rejected, or position fully closed)."""
        s = _session()
        try:
            s.execute(
                _text(f"DELETE FROM {TABLE} WHERE strategy_id = :sid AND symbol = :sym"),
                {"sid": strategy_id, "sym": symbol},
            )
            s.commit()
        except Exception as exc:
            s.rollback()
            logger.warning("sleeve release failed %s/%s: %s", strategy_id, symbol, exc)
        finally:
            s.close()

        self._by_key.pop((strategy_id, symbol), None)

    def reduce(self, strategy_id: str, symbol: str, qty: float) -> None:
        """Book a partial or full exit for this strategy's lot only."""
        pos = self._by_key.get((strategy_id, symbol))
        remaining = max(0.0, (pos.qty if pos else 0.0) - float(qty))
        if remaining <= 0:
            self.release(strategy_id, symbol)
            return

        s = _session()
        try:
            s.execute(
                _text(
                    f"UPDATE {TABLE} SET qty = :qty, updated_at = NOW() "
                    f"WHERE strategy_id = :sid AND symbol = :sym"
                ),
                {"qty": remaining, "sid": strategy_id, "sym": symbol},
            )
            s.commit()
        except Exception as exc:
            s.rollback()
            logger.warning("sleeve reduce failed %s/%s: %s", strategy_id, symbol, exc)
        finally:
            s.close()

        if pos:
            pos.qty = remaining

    def adopt_orphan(
        self,
        strategy_id: str,
        symbol: str,
        qty: float,
        price: float,
        *,
        source: str = "adopt",
    ) -> float:
        """Move an `__unassigned__` (or unowned) broker lot onto `strategy_id`.

        Returns qty claimed (0 if nothing to adopt). Uses coexist so a partial
        orphan next to another sleeve does not wipe the other owner.
        """
        symbol = str(symbol).upper()
        want = float(qty)
        if want <= 0:
            return 0.0
        orphan_qty = self.qty_of(UNASSIGNED, symbol)
        others = [
            p
            for p in self.owners_of(symbol)
            if p.strategy_id not in (strategy_id, UNASSIGNED)
        ]
        if others and orphan_qty <= 0:
            return 0.0
        take = min(want, orphan_qty) if orphan_qty > 0 else want
        if orphan_qty > 0:
            self.reduce(UNASSIGNED, symbol, take)
        ok = self.claim(
            strategy_id, symbol, take, float(price or 0), source=source, coexist=True
        )
        return take if ok else 0.0

    # ── reconciliation ───────────────────────────────────────────────────

    def reconcile(
        self,
        broker_qty: Mapping[str, float],
        protected: Iterable[str] = (),
        grace_sec: int = CLAIM_GRACE_SEC,
        symbols: Iterable[str] | None = None,
    ) -> list[LedgerChange]:
        """Sync the ledger to broker truth.

        Compares broker qty to the *sum* of sleeve lots per symbol so shared
        lots (coexist) are not mistaken for orphans.
        """
        protected = set(protected)
        scope = {str(s) for s in symbols} if symbols is not None else None
        changes: list[LedgerChange] = []

        s = _session()
        try:
            sql = (
                f"SELECT strategy_id, symbol, qty, avg_price, "
                f"EXTRACT(EPOCH FROM (NOW() - updated_at)), updated_at FROM {TABLE}"
            )
            if scope is not None:
                sql += " WHERE symbol = ANY(:scope)"
            rows = s.execute(
                _text(sql), {"scope": sorted(scope)} if scope is not None else {}
            ).fetchall()

            by_sym: dict[str, list[tuple]] = {}
            for row in rows:
                by_sym.setdefault(str(row[1]), []).append(row)

            touched: set[str] = set()

            for symbol, lots in by_sym.items():
                touched.add(symbol)
                bq = float(broker_qty.get(symbol, 0.0))
                total = sum(float(r[2]) for r in lots)
                # Youngest lot age for grace (pending entries).
                min_age = min(float(r[4] or 0) for r in lots)

                if bq <= 0:
                    if symbol in protected or min_age < grace_sec:
                        for sid, _sym, qty, avg_price, _age, updated_at in lots:
                            changes.append(
                                LedgerChange(
                                    "pending",
                                    str(sid),
                                    symbol,
                                    float(qty),
                                    float(qty),
                                    float(avg_price),
                                    updated_at,
                                )
                            )
                        continue
                    for sid, _sym, qty, avg_price, _age, updated_at in lots:
                        s.execute(
                            _text(
                                f"DELETE FROM {TABLE} WHERE strategy_id = :sid AND symbol = :sym"
                            ),
                            {"sid": str(sid), "sym": symbol},
                        )
                        self._by_key.pop((str(sid), symbol), None)
                        changes.append(
                            LedgerChange(
                                "closed",
                                str(sid),
                                symbol,
                                float(qty),
                                0.0,
                                float(avg_price),
                                updated_at,
                            )
                        )
                    continue

                if abs(bq - total) <= 1e-6:
                    # In sync (single or shared lots).
                    for sid, _sym, qty, avg_price, _age, _upd in lots:
                        self._by_key[(str(sid), symbol)] = SleevePosition(
                            str(sid), symbol, float(qty), float(avg_price)
                        )
                    continue

                if bq < total - 1e-9:
                    # Shares disappeared — shrink lots newest-first.
                    deficit = total - bq
                    ordered = sorted(
                        lots,
                        key=lambda r: r[5] or datetime.min.replace(tzinfo=None),
                        reverse=True,
                    )
                    for sid, _sym, qty, avg_price, _age, updated_at in ordered:
                        if deficit <= 1e-9:
                            self._by_key[(str(sid), symbol)] = SleevePosition(
                                str(sid), symbol, float(qty), float(avg_price)
                            )
                            continue
                        q = float(qty)
                        take = min(q, deficit)
                        new_q = q - take
                        deficit -= take
                        if new_q <= 1e-9:
                            s.execute(
                                _text(
                                    f"DELETE FROM {TABLE} "
                                    f"WHERE strategy_id = :sid AND symbol = :sym"
                                ),
                                {"sid": str(sid), "sym": symbol},
                            )
                            self._by_key.pop((str(sid), symbol), None)
                            changes.append(
                                LedgerChange(
                                    "closed",
                                    str(sid),
                                    symbol,
                                    q,
                                    0.0,
                                    float(avg_price),
                                    updated_at,
                                )
                            )
                        else:
                            s.execute(
                                _text(
                                    f"UPDATE {TABLE} SET qty = :qty, updated_at = NOW() "
                                    f"WHERE strategy_id = :sid AND symbol = :sym"
                                ),
                                {"qty": new_q, "sid": str(sid), "sym": symbol},
                            )
                            self._by_key[(str(sid), symbol)] = SleevePosition(
                                str(sid), symbol, new_q, float(avg_price)
                            )
                            changes.append(
                                LedgerChange(
                                    "reduced",
                                    str(sid),
                                    symbol,
                                    q,
                                    new_q,
                                    float(avg_price),
                                    updated_at,
                                )
                            )
                    continue

                # bq > total: unexplained increase — freeze excess as orphan lot
                # without wiping attributed sleeves.
                excess = bq - total
                prev_orphan = next(
                    (float(r[2]) for r in lots if str(r[0]) == UNASSIGNED), 0.0
                )
                s.execute(
                    _text(
                        f"""
                        INSERT INTO {TABLE}
                            (strategy_id, symbol, qty, avg_price, source, opened_at, updated_at)
                        VALUES (:sid, :sym, :qty, 0, 'orphan', NOW(), NOW())
                        ON CONFLICT (strategy_id, symbol) DO UPDATE SET
                            qty = {TABLE}.qty + EXCLUDED.qty,
                            updated_at = NOW()
                        """
                    ),
                    {"sid": UNASSIGNED, "sym": symbol, "qty": excess},
                )
                self._by_key[(UNASSIGNED, symbol)] = SleevePosition(
                    UNASSIGNED, symbol, prev_orphan + excess, 0.0
                )
                for sid, _sym, qty, avg_price, _age, _upd in lots:
                    if str(sid) != UNASSIGNED:
                        self._by_key[(str(sid), symbol)] = SleevePosition(
                            str(sid), symbol, float(qty), float(avg_price)
                        )
                changes.append(
                    LedgerChange(
                        "orphaned", UNASSIGNED, symbol, total, bq, 0.0, None
                    )
                )

            for symbol, bq in broker_qty.items():
                if scope is not None and symbol not in scope:
                    continue
                if bq > 0 and symbol not in touched:
                    s.execute(
                        _text(
                            f"INSERT INTO {TABLE} "
                            f"(strategy_id, symbol, qty, avg_price, source) "
                            f"VALUES (:sid, :sym, :qty, 0, 'orphan') "
                            f"ON CONFLICT (strategy_id, symbol) DO UPDATE "
                            f"SET qty = EXCLUDED.qty, updated_at = NOW()"
                        ),
                        {"sid": UNASSIGNED, "sym": symbol, "qty": float(bq)},
                    )
                    self._by_key[(UNASSIGNED, symbol)] = SleevePosition(
                        UNASSIGNED, symbol, float(bq), 0.0
                    )
                    changes.append(
                        LedgerChange("orphaned", UNASSIGNED, symbol, 0.0, float(bq))
                    )

            s.commit()
        except Exception as exc:
            s.rollback()
            raise LedgerUnavailable(f"reconcile failed: {exc}") from exc
        finally:
            s.close()

        return changes


def broker_qty_map(positions: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Broker position list → {symbol: qty}."""
    out: dict[str, float] = {}
    for p in positions:
        try:
            out[str(p["symbol"])] = float(p.get("qty") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def open_order_symbols(client: Any) -> set[str]:
    """Symbols with resting orders, used to protect unfilled entry claims.

    Raises rather than returning an empty set on failure. An empty set reads as
    "nothing is resting", which lets reconcile drop a claim whose entry order is
    still live at the broker and hand the symbol to a second strategy.
    """
    try:
        return {
            str(o.get("symbol"))
            for o in (client.orders(status="open", limit=200) or [])
            if o.get("symbol")
        }
    except Exception as exc:
        logger.warning("open order lookup failed: %s", exc)
        raise LedgerUnavailable(f"cannot list open orders: {exc}") from exc
