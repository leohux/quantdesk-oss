"""
Self-test for the strategy_positions ledger.
============================================
Runs against the live Postgres but only touches synthetic symbols (ZZ*), so it
is safe to run at any time. Replays the 2026-07-28/29 MSFT incident where the
intraday sleeve opened a position and a mean-reversion strategy closed it.

Usage:  python /app/scripts/sleeve_selftest.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from core.portfolio.sleeve import UNASSIGNED, SleeveBook, ensure_schema

INTRADAY = "strategy-046bfa"
MIMO = "mimo-mean-reversion-rsi-extreme-fac1bf"
ATRBREAK = "cursor-hybrid-classic-atrbreak-aapl-m15-061001-d4e35-44d4b4"

SYMS = ["ZZMSFT", "ZZAAPL", "ZZORPH", "ZZKEEP"]
# Everything reconcile() is allowed to see. ZZKEEP is deliberately excluded so
# the last case can prove an out-of-scope position survives untouched.
SCOPE = ["ZZMSFT", "ZZAAPL", "ZZORPH"]

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def cleanup() -> None:
    from sqlalchemy import text

    from core.db import SyncSessionLocal

    s = SyncSessionLocal()
    try:
        s.execute(
            text("DELETE FROM strategy_positions WHERE symbol = ANY(:syms)"),
            {"syms": SYMS},
        )
        s.commit()
    finally:
        s.close()


def main() -> None:
    ensure_schema()
    cleanup()

    print("\n1. MSFT incident replay ??intraday opens, mimo tries to exit")
    book = SleeveBook.load()
    check("intraday claims ZZMSFT", book.claim(INTRADAY, "ZZMSFT", 36, 455.0), True)
    check("owner is intraday", book.owner_of("ZZMSFT"), INTRADAY)
    # phase6 asks "is this my position?" before allowing an exit signal through
    check("mimo sees no position of its own", book.qty_of(MIMO, "ZZMSFT"), 0.0)
    check("intraday sees its 36 shares", book.qty_of(INTRADAY, "ZZMSFT"), 36.0)
    check("mimo cannot claim it either", book.claim(MIMO, "ZZMSFT", 36, 455.0), False)
    check("owner unchanged after failed claim", book.owner_of("ZZMSFT"), INTRADAY)

    print("\n2. cross-process claim race (separate SleeveBook instances)")
    other = SleeveBook.load()
    check("second process sees the claim", other.owner_of("ZZMSFT"), INTRADAY)
    check("second process claim rejected", other.claim(ATRBREAK, "ZZMSFT", 52, 464.0), False)

    print("\n3. owner exits its own position")
    book.reduce(INTRADAY, "ZZMSFT", 20)
    check("partial exit leaves 16", book.qty_of(INTRADAY, "ZZMSFT"), 16.0)
    book.reduce(INTRADAY, "ZZMSFT", 16)
    check("full exit clears owner", book.owner_of("ZZMSFT"), None)
    check("symbol now claimable by others", book.claim(ATRBREAK, "ZZMSFT", 52, 464.0), True)

    print("\n4. reconcile against broker truth")
    book.claim(ATRBREAK, "ZZAAPL", 78, 307.81)
    # Broker shows ZZMSFT closed by a bracket stop and ZZAAPL trimmed to 40.
    changes = book.reconcile({"ZZAAPL": 40.0}, protected=set(), grace_sec=0, symbols=SCOPE)
    check("bracket-closed position dropped", book.owner_of("ZZMSFT"), None)
    check("trimmed position resized", book.qty_of(ATRBREAK, "ZZAAPL"), 40.0)
    by_kind = {(c.kind, c.symbol): c for c in changes}
    check("closed reported", ("closed", "ZZMSFT") in by_kind, True)
    check("reduced reported", ("reduced", "ZZAAPL") in by_kind, True)
    # settle_broker_exits() needs these to find the matching broker fills
    check("closed carries lost qty", by_kind[("closed", "ZZMSFT")].qty_gone, 52.0)
    check("reduced carries lost qty", by_kind[("reduced", "ZZAAPL")].qty_gone, 38.0)

    print("\n5. fresh claim survives reconcile while its order rests unfilled")
    book.claim(MIMO, "ZZMSFT", 10, 100.0)
    book.reconcile({"ZZAAPL": 40.0}, protected={"ZZMSFT"}, grace_sec=0, symbols=SCOPE)
    check("protected claim kept", book.owner_of("ZZMSFT"), MIMO)
    book.reconcile({"ZZAAPL": 40.0}, protected=set(), grace_sec=3600, symbols=SCOPE)
    check("in-grace claim kept", book.owner_of("ZZMSFT"), MIMO)
    book.reconcile({"ZZAAPL": 40.0}, protected=set(), grace_sec=0, symbols=SCOPE)
    check("stale unfilled claim released", book.owner_of("ZZMSFT"), None)

    print("\n6. unknown broker position becomes a frozen orphan")
    book.reconcile({"ZZAAPL": 40.0, "ZZORPH": 5.0}, protected=set(), grace_sec=0, symbols=SCOPE)
    check("orphan recorded", book.owner_of("ZZORPH"), UNASSIGNED)
    check("no strategy owns it", book.qty_of(ATRBREAK, "ZZORPH"), 0.0)
    check("strategies cannot claim it", book.claim(ATRBREAK, "ZZORPH", 5, 1.0), False)

    print("\n6b. unexplained share increase freezes excess as orphan (keeps attributed lot)")
    # ATRBREAK still holds ZZAAPL x40. If the broker suddenly shows 55, do not
    # gift the extra 15 to ATRBREAK — park 15 as orphan beside the attributed 40.
    changes = book.reconcile(
        {"ZZAAPL": 55.0, "ZZORPH": 5.0}, protected=set(), grace_sec=0, symbols=SCOPE
    )
    check("attributed lot kept", book.qty_of(ATRBREAK, "ZZAAPL"), 40.0)
    check("excess is orphan", book.qty_of(UNASSIGNED, "ZZAAPL"), 15.0)
    check("orphaned change reported", any(c.kind == "orphaned" and c.symbol == "ZZAAPL" for c in changes), True)

    print("\n7. a scoped reconcile leaves positions outside the scope alone")
    # An unscoped reconcile with a partial broker map would read every ledger
    # row, see no broker position, and delete the lot. This is that regression.
    book.claim(INTRADAY, "ZZKEEP", 99, 12.34)
    book.reconcile({"ZZAAPL": 40.0}, protected=set(), grace_sec=0, symbols=SCOPE)
    check("out-of-scope owner intact", SleeveBook.load().owner_of("ZZKEEP"), INTRADAY)
    check("out-of-scope qty intact", SleeveBook.load().qty_of(INTRADAY, "ZZKEEP"), 99.0)

    cleanup()
    print(f"\n{'ALL PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
