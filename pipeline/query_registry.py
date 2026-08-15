#!/usr/bin/env python3
"""
query_registry.py — Research Registry query tool

CLI Usage:
    python query_registry.py --status Rejected
    python query_registry.py --status Rejected --regime-sensitive
    python query_registry.py --strategy-type event-driven --min-oos-grade B
    python query_registry.py --research-id Research-001
    python query_registry.py --list-failures
    python query_registry.py --summary

Python API:
    from query_registry import find_research, get_research, list_rejected, list_live, list_event_driven
"""
import argparse
import json
import os
import sys
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


# ─── Database ───────────────────────────────────────────────────
DB_CONFIG = {
    'host': os.environ.get('PG_HOST', 'quantdesk-postgres'),
    'port': os.environ.get('PG_PORT', '5432'),
    'dbname': os.environ.get('PG_DB', 'quantdesk'),
    'user': os.environ.get('PG_USER', 'quantdesk'),
    'password': os.environ.get('PG_PASS', ''),
}


def _get_conn():
    return psycopg2.connect(**DB_CONFIG)


def _query(sql, params=None):
    """Execute query and return list of dicts."""
    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params or {})
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ─── Grade ordering ─────────────────────────────────────────────
GRADE_ORDER = {'A+': 7, 'A': 6, 'A-': 5, 'B+': 4, 'B': 3, 'B-': 2, 'C+': 1, 'C': 0, 'C-': -1, 'D': -2, 'F': -3}


def grade_above(actual: str, minimum: str) -> bool:
    return GRADE_ORDER.get(actual, -99) >= GRADE_ORDER.get(minimum, -99)


# ═══════════════════════════════════════════════════════════════
# Python API — importable functions
# ═══════════════════════════════════════════════════════════════

def find_research(
    status: Optional[str] = None,
    stage: Optional[str] = None,
    strategy_type: Optional[str] = None,
    failure_category: Optional[str] = None,
    min_is_sharpe: Optional[float] = None,
    min_oos_sharpe: Optional[float] = None,
    min_oos_grade: Optional[str] = None,
    regime_sensitive: bool = False,
    cross_asset_tested: Optional[bool] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Search research registry with flexible filters.

    Returns list of dicts. Each dict = one research_registry row.
    """
    conditions = []
    params = {}

    if status:
        conditions.append("status = %(status)s")
        params['status'] = status
    if stage:
        conditions.append("stage = %(stage)s")
        params['stage'] = stage
    if strategy_type:
        conditions.append("strategy_type = %(strategy_type)s")
        params['strategy_type'] = strategy_type
    if failure_category:
        conditions.append("failure_category = %(failure_category)s")
        params['failure_category'] = failure_category
    if min_is_sharpe is not None:
        conditions.append("is_sharpe >= %(min_is_sharpe)s")
        params['min_is_sharpe'] = min_is_sharpe
    if min_oos_sharpe is not None:
        conditions.append("oos_sharpe >= %(min_oos_sharpe)s")
        params['min_oos_sharpe'] = min_oos_sharpe
    if regime_sensitive:
        conditions.append("(regime_dependency_score IS NOT NULL OR regime_notes IS NOT NULL)")
    if cross_asset_tested is not None:
        conditions.append("cross_asset_tested = %(cross_asset_tested)s")
        params['cross_asset_tested'] = cross_asset_tested

    where = " AND ".join(conditions) if conditions else "TRUE"
    sql = f"""
        SELECT * FROM research_registry
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT %(limit)s
    """
    params['limit'] = limit
    rows = _query(sql, params)

    # Post-filter for grade-based queries
    if min_oos_grade and min_oos_grade in GRADE_ORDER:
        rows = [r for r in rows if grade_above(r.get('oos_grade', 'F'), min_oos_grade)]

    return rows


def get_research(research_id: str, version: int = 1) -> Optional[dict]:
    """Get a single research record by ID and version."""
    rows = _query(
        "SELECT * FROM research_registry WHERE research_id = %(rid)s AND version = %(v)s",
        {'rid': research_id, 'v': version}
    )
    return rows[0] if rows else None


def list_rejected(limit: int = 50) -> list[dict]:
    """List all rejected research."""
    return find_research(status='Rejected', limit=limit)


def list_live(limit: int = 50) -> list[dict]:
    """List all live/paper-trading research."""
    return find_research(status='Live', limit=limit)


def list_event_driven(limit: int = 50) -> list[dict]:
    """List all event-driven research."""
    return find_research(strategy_type='event-driven', limit=limit)


def failure_summary() -> list[dict]:
    """Aggregate count by failure_category."""
    return _query("""
        SELECT failure_category, COUNT(*) as count,
               array_agg(research_id ORDER BY research_id) as research_ids
        FROM research_registry
        WHERE failure_category IS NOT NULL
        GROUP BY failure_category
        ORDER BY count DESC
    """)


def overall_summary() -> dict:
    """Return overall registry statistics."""
    stats = {}
    stats['total'] = _query("SELECT COUNT(*) as c FROM research_registry")[0]['c']
    stats['by_status'] = {r['status']: r['c'] for r in _query(
        "SELECT status, COUNT(*) as c FROM research_registry GROUP BY status ORDER BY c DESC"
    )}
    stats['by_stage'] = {r['stage']: r['c'] for r in _query(
        "SELECT stage, COUNT(*) as c FROM research_registry GROUP BY stage ORDER BY c DESC"
    )}
    stats['by_failure'] = {r['failure_category']: r['c'] for r in _query(
        "SELECT failure_category, COUNT(*) as c FROM research_registry WHERE failure_category IS NOT NULL GROUP BY failure_category ORDER BY c DESC"
    )}
    return stats


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def _print_table(rows, fields=None):
    """Print rows as a formatted table."""
    if not rows:
        print("  (no results)")
        return

    if fields is None:
        fields = ['research_id', 'version', 'status', 'stage', 'strategy_type',
                  'is_sharpe', 'oos_sharpe', 'walk_forward_grade', 'failure_category']

    # Header
    header = " | ".join(f"{f:<22}" for f in fields)
    print(f"  {header}")
    print("  " + "-" * len(header))

    for row in rows:
        vals = []
        for f in fields:
            v = row.get(f, '')
            if v is None:
                v = ''
            elif isinstance(v, float):
                v = f"{v:.3f}"
            vals.append(f"{str(v):<22}")
        print("  " + " | ".join(vals))


def _print_record(row):
    """Print a single record in detail format."""
    print()
    for k, v in sorted(row.items()):
        if v is not None and k != 'id':
            print(f"  {k:35s} = {v}")


def main():
    parser = argparse.ArgumentParser(description='Query Research Registry')
    parser.add_argument('--status', type=str, help='Filter by status (Running/Rejected/Accepted/Live)')
    parser.add_argument('--stage', type=str, help='Filter by stage')
    parser.add_argument('--strategy-type', type=str, help='Filter by strategy type')
    parser.add_argument('--failure-category', type=str, help='Filter by failure category')
    parser.add_argument('--min-is-sharpe', type=float, help='Minimum IS Sharpe')
    parser.add_argument('--min-oos-sharpe', type=float, help='Minimum OOS Sharpe')
    parser.add_argument('--min-oos-grade', type=str, help='Minimum OOS grade (A/B/C/D/F)')
    parser.add_argument('--regime-sensitive', action='store_true', help='Only regime-sensitive research')
    parser.add_argument('--cross-asset', action='store_true', help='Only cross-asset tested')
    parser.add_argument('--research-id', type=str, help='Get single research by ID')
    parser.add_argument('--list-failures', action='store_true', help='Show failure category summary')
    parser.add_argument('--summary', action='store_true', help='Show overall summary')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--limit', type=int, default=50, help='Max results')
    args = parser.parse_args()

    if args.summary:
        s = overall_summary()
        print("\n=== Research Registry Summary ===")
        print(f"\nTotal: {s['total']}")
        print("\nBy Status:")
        for k, v in s['by_status'].items():
            print(f"  {k}: {v}")
        print("\nBy Stage:")
        for k, v in s['by_stage'].items():
            print(f"  {k}: {v}")
        if s['by_failure']:
            print("\nBy Failure Category:")
            for k, v in s['by_failure'].items():
                print(f"  {k}: {v}")
        return

    if args.list_failures:
        rows = failure_summary()
        print("\n=== Failure Category Summary ===")
        for r in rows:
            print(f"\n  {r['failure_category']} ({r['count']} researches):")
            for rid in r['research_ids']:
                print(f"    - {rid}")
        return

    if args.research_id:
        row = get_research(args.research_id)
        if row:
            if args.json:
                print(json.dumps(row, indent=2, default=str))
            else:
                _print_record(row)
        else:
            print(f"  Not found: {args.research_id}")
        return

    # General search
    rows = find_research(
        status=args.status,
        stage=args.stage,
        strategy_type=args.strategy_type,
        failure_category=args.failure_category,
        min_is_sharpe=args.min_is_sharpe,
        min_oos_sharpe=args.min_oos_sharpe,
        min_oos_grade=args.min_oos_grade,
        regime_sensitive=args.regime_sensitive,
        cross_asset_tested=args.cross_asset if args.cross_asset else None,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(f"\n  Found {len(rows)} result(s):\n")
        _print_table(rows)


if __name__ == '__main__':
    main()
