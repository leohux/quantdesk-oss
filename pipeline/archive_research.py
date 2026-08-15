#!/usr/bin/env python3
"""
archive_research.py — Idempotent research archiver

Usage:
    python archive_research.py /path/to/research/archive/<date>/<research_dir>/

Parses the research directory, extracts metadata, and UPSERTs into
research_registry. Safe to run multiple times — same result every run.

Expects the standard research archive structure:
    hypothesis.md     — frozen hypothesis
    protocol.md       — frozen protocol
    decision.md       — final decision (with grade table)
    data_quality.md   — data quality report
    is_results/       — IS parameter search output
    oos_results/      — OOS validation output
    walk_forward/     — Walk-Forward results
    post_mortem/      — Post-mortem analysis (optional)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import psycopg2
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


# ─── Parsers ────────────────────────────────────────────────────

def parse_decision_md(path: Path) -> dict:
    """Extract grades and status from decision.md."""
    info = {}
    if not path.exists():
        return info

    text = path.read_text()

    # Extract research_id from title
    m = re.search(r'#\s+(Research-\d+)', text)
    if m:
        info['research_id'] = m.group(1)

    # Extract grades from the rating table
    grade_map = {
        'data quality': 'data_quality_grade',
        '回测可信度': 'data_quality_grade',
        'backtest reliability': 'backtest_reliability_grade',
        '回测可信度': 'backtest_reliability_grade',
        'is结果': 'is_grade',
        'is result': 'is_grade',
        'oos结果': 'oos_grade',
        'oos result': 'oos_grade',
        'walk-forward': 'walk_forward_grade',
        'walk forward': 'walk_forward_grade',
    }

    for line in text.split('\n'):
        line_lower = line.lower().strip()

        # Grade table rows: | Data Quality | A | ... |
        for key, field in grade_map.items():
            if key in line_lower and '|' in line:
                parts = [p.strip() for p in line.split('|') if p.strip()]
                if len(parts) >= 2:
                    grade = parts[-1] if parts[-1] not in ('评分', 'Grade', '评价') else parts[1] if len(parts) > 1 else ''
                    # Last column in table row
                    for p in reversed(parts):
                        if re.match(r'^[A-F][+-]?$', p):
                            info[field] = p
                            break

        # Status / final verdict
        if 'hypothesis rejected' in line_lower or 'rejected' in line_lower:
            info['status'] = 'Rejected'
            info['stage'] = 'Archived'
        elif 'accepted' in line_lower and ('进入实盘' in line_lower or 'live' in line_lower):
            info['status'] = 'Accepted'
            info['stage'] = 'Completed'
        elif 'paper trading' in line_lower:
            info['status'] = 'Accepted'
            info['stage'] = 'PaperTrading'

        # Lookahead audit
        if 'look-ahead' in line_lower or 'lookahead' in line_lower:
            if 'fix' in line_lower or 'repair' in line_lower:
                info['lookahead_audit_status'] = 'Audited-Issues-Fixed'
                info['lookahead_audit_notes'] = 'Look-ahead bias detected and corrected during research.'

        # Failure category
        if 'regime shift' in line_lower or '市场结构变化' in line_lower:
            info['failure_category'] = 'RegimeShift'
        elif 'overfit' in line_lower or '过拟合' in line_lower:
            info['failure_category'] = 'Overfit'
        elif 'lookahead' in line_lower or 'look-ahead' in line_lower:
            info['failure_category'] = 'Lookahead'
        elif 'capacity' in line_lower:
            info['failure_category'] = 'Capacity'
        elif 'cost' in line_lower or '滑点' in line_lower:
            info['failure_category'] = 'Cost'
        elif 'low sample' in line_lower or '样本' in line_lower:
            info['failure_category'] = 'LowSample'

        # Rejection reason — grab the first bold or clear statement
        if '最终结论' in line_lower or 'final conclusion' in line_lower:
            # Next non-empty line is likely the reason
            pass

    # Extract final decision text
    m = re.search(r'(?:最终结论|Final Conclusion|## 结论)\s*\n+([^\n#]+)', text, re.IGNORECASE)
    if m:
        info['final_decision'] = m.group(1).strip().strip('*').strip()

    # Extract rejection reason
    m = re.search(r'(?:失败原因|Rejection Reason)[:\s]*\n((?:\d+\..*\n?)+)', text)
    if m:
        info['rejection_reason'] = m.group(1).strip()
    elif 'fail' in text.lower() or 'reject' in text.lower():
        # Fallback: grab a line near "conclusion"
        for line in text.split('\n'):
            if any(kw in line.lower() for kw in ['核心洞察', '失败原因', 'failure', 'rejection reason']):
                info['rejection_reason'] = line.strip().strip('*').strip('-').strip()

    return info


def parse_hypothesis_md(path: Path) -> dict:
    """Extract hypothesis text."""
    info = {}
    if not path.exists():
        return info
    text = path.read_text()

    # The main hypothesis is usually in a blockquote or the first paragraph after H1
    lines = text.strip().split('\n')
    hypothesis_lines = []
    in_hypothesis = False
    for line in lines:
        if line.startswith('#'):
            continue
        if line.startswith('>') or in_hypothesis:
            in_hypothesis = True
            cleaned = line.lstrip('> ').strip()
            if cleaned:
                hypothesis_lines.append(cleaned)
            elif hypothesis_lines:
                break

    if hypothesis_lines:
        info['hypothesis'] = ' '.join(hypothesis_lines)
    else:
        # Fallback: first non-empty, non-heading line
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                info['hypothesis'] = stripped
                break

    return info


def parse_protocol_md(path: Path) -> dict:
    """Extract strategy type and other protocol metadata."""
    info = {}
    if not path.exists():
        return info
    text = path.read_text().lower()

    # Infer strategy_type
    if 'event' in text:
        info['strategy_type'] = 'event-driven'
    elif 'regime' in text:
        info['strategy_type'] = 'regime-aware'
    elif 'stat' in text or 'mean reversion' in text:
        info['strategy_type'] = 'stat-arb'
    elif 'momentum' in text or 'trend' in text:
        info['strategy_type'] = 'momentum'
    else:
        info['strategy_type'] = 'technical'

    return info


def parse_is_results(base_dir: Path) -> dict:
    """Extract IS metrics from is_results/ or is_parameter_search output."""
    info = {}
    is_dir = base_dir / 'is_results'
    meta_file = is_dir / 'is_search_meta.json'
    top10_file = is_dir / 'is_top10.csv'

    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        if 'top_params' in meta:
            tp = meta['top_params']
            info['is_sharpe'] = tp.get('sharpe')
        if 'top_sharpe' in meta:
            info['is_sharpe'] = meta['top_sharpe']

    if top10_file.exists():
        lines = top10_file.read_text().strip().split('\n')
        if len(lines) > 1:
            # Parse CSV header + first data row
            headers = lines[0].split(',')
            values = lines[1].split(',')
            row = dict(zip(headers, values))
            try:
                info['is_sharpe'] = float(row.get('sharpe', 0))
                info['is_trades'] = int(float(row.get('total_trades', 0)))
            except (ValueError, TypeError):
                pass

    # IS grade (from Sharpe)
    if info.get('is_sharpe'):
        s = float(info['is_sharpe'])
        if s >= 2.0:
            info['is_grade'] = 'A'
        elif s >= 1.5:
            info['is_grade'] = 'B+'
        elif s >= 1.0:
            info['is_grade'] = 'B'
        elif s >= 0.5:
            info['is_grade'] = 'C'
        else:
            info['is_grade'] = 'D'

    return info


def parse_oos_results(base_dir: Path) -> dict:
    """Extract OOS metrics from oos_results/."""
    info = {}
    oos_file = base_dir / 'oos_results' / 'oos_validation.json'

    if oos_file.exists():
        data = json.loads(oos_file.read_text())
        # Pick the better of parameter_a and parameter_b
        best = None
        for key in ['parameter_b', 'parameter_a']:
            if key in data:
                p = data[key]
                if p.get('sharpe', 0) != 0 and (best is None or p['sharpe'] > best.get('sharpe', 0)):
                    best = p
        if best:
            info['oos_sharpe'] = best.get('sharpe')
            info['oos_trades'] = best.get('trades')

    # OOS grade
    if info.get('oos_sharpe') is not None:
        s = float(info['oos_sharpe'])
        if s >= 1.5:
            info['oos_grade'] = 'A'
        elif s >= 1.0:
            info['oos_grade'] = 'B'
        elif s >= 0.5:
            info['oos_grade'] = 'C'
        elif s >= 0:
            info['oos_grade'] = 'D'
        else:
            info['oos_grade'] = 'F'

    return info


def parse_walk_forward(base_dir: Path) -> dict:
    """Extract Walk-Forward summary metrics."""
    info = {}
    wf_csv = base_dir / 'walk_forward' / 'walk_forward_results.csv'
    wf_json = base_dir / 'walk_forward' / 'walk_forward_summary.json'

    if wf_json.exists():
        try:
            raw = wf_json.read_text()
            data = json.loads(raw)
            info['walk_forward_windows'] = data.get('total_windows')
            info['walk_forward_positive_ratio'] = data.get('positive_oos_ratio')
            info['walk_forward_mean_sharpe'] = data.get('mean_oos_sharpe')
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to CSV parser
    elif wf_csv.exists():
        lines = wf_csv.read_text().strip().split('\n')
        if len(lines) > 1:
            headers = lines[0].split(',')
            sharpe_idx = headers.index('test_sharpe') if 'test_sharpe' in headers else None
            trades_idx = headers.index('test_trades') if 'test_trades' in headers else None

            sharpes = []
            total_trades = 0
            active = 0
            positive = 0

            for line in lines[1:]:
                vals = line.split(',')
                if sharpe_idx is not None:
                    try:
                        s = float(vals[sharpe_idx])
                        t = int(float(vals[trades_idx])) if trades_idx else 0
                        if t >= 10:
                            active += 1
                            sharpes.append(s)
                            total_trades += t
                            if s > 0:
                                positive += 1
                    except (ValueError, IndexError):
                        pass

            info['walk_forward_windows'] = active
            info['walk_forward_positive_ratio'] = round(positive / active, 2) if active > 0 else 0
            info['walk_forward_mean_sharpe'] = round(sum(sharpes) / len(sharpes), 3) if sharpes else 0
            info['walk_forward_total_trades'] = total_trades

    # WF grade
    ratio = info.get('walk_forward_positive_ratio')
    mean_s = info.get('walk_forward_mean_sharpe')
    if ratio is not None:
        if ratio >= 0.7 and (mean_s or 0) >= 0.5:
            info['walk_forward_grade'] = 'A'
        elif ratio >= 0.6 and (mean_s or 0) >= 0.3:
            info['walk_forward_grade'] = 'B'
        elif ratio >= 0.5:
            info['walk_forward_grade'] = 'C'
        else:
            info['walk_forward_grade'] = 'D'

    return info


def parse_post_mortem(base_dir: Path) -> dict:
    """Extract post-mortem regime notes."""
    info = {}
    pm_dir = base_dir / 'post_mortem'
    if not pm_dir.exists():
        return info

    # Check for quarterly signals CSV
    signals_csv = pm_dir / 'quarterly_signals.csv'
    if signals_csv.exists():
        lines = signals_csv.read_text().strip().split('\n')
        if len(lines) > 2:
            info['regime_notes'] = 'Signal count analysis available. See post_mortem/quarterly_signals.csv'

    return info


# ─── Detect research_id from directory name ─────────────────────

def detect_research_id(dir_path: Path) -> str:
    """Extract research ID from directory name like 'research_001_intraday_gap_reversal'."""
    name = dir_path.name
    m = re.search(r'research[_-](\d+)', name, re.IGNORECASE)
    if m:
        return f"Research-{m.group(1).zfill(3)}"
    return name


def detect_strategy_type_from_path(dir_path: Path) -> str:
    """Infer strategy type from directory name."""
    name = dir_path.name.lower()
    if 'gap' in name or 'reversal' in name:
        return 'technical'
    if 'event' in name or 'earnings' in name:
        return 'event-driven'
    if 'regime' in name:
        return 'regime-aware'
    return 'technical'


def detect_economic_hypothesis(dir_path: Path, hypothesis: str) -> str:
    """Try to infer economic_hypothesis from hypothesis text if not explicit."""
    h = hypothesis.lower()
    if 'momentum' in h and ('exhaust' in h or 'reversal' in h or 'revert' in h):
        return 'Intraday momentum exhaustion creates temporary mean reversion after abnormal upside moves.'
    if 'mean reversion' in h:
        return 'Statistical mean reversion following extreme intraday moves.'
    if 'earnings' in h or 'surprise' in h:
        return 'Earnings surprise under-reacts for 2-5 trading days.'
    if 'gap' in h and 'reversal' in h:
        return 'Intraday gap-up followed by pullback exhibits mean-reverting behavior in semiconductor stocks.'
    return None


# ─── UPSERT ─────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO research_registry (
    research_id, version, parent_research_id,
    hypothesis, economic_hypothesis, strategy_type,
    status, stage, rejection_reason, failure_category,
    data_quality_grade, backtest_reliability_grade,
    lookahead_audit_status, lookahead_audit_notes,
    is_sharpe, is_grade, is_trades,
    oos_sharpe, oos_grade, oos_trades,
    walk_forward_grade, walk_forward_windows,
    walk_forward_positive_ratio, walk_forward_mean_sharpe,
    walk_forward_total_trades,
    cross_asset_tested, cost_test_passed,
    regime_dependency_score, regime_notes,
    correlation_to_existing, capacity_estimate_usd,
    reuse_of_rejected, went_to_live, final_decision
) VALUES (
    %(research_id)s, %(version)s, %(parent_research_id)s,
    %(hypothesis)s, %(economic_hypothesis)s, %(strategy_type)s,
    %(status)s, %(stage)s, %(rejection_reason)s, %(failure_category)s,
    %(data_quality_grade)s, %(backtest_reliability_grade)s,
    %(lookahead_audit_status)s, %(lookahead_audit_notes)s,
    %(is_sharpe)s, %(is_grade)s, %(is_trades)s,
    %(oos_sharpe)s, %(oos_grade)s, %(oos_trades)s,
    %(walk_forward_grade)s, %(walk_forward_windows)s,
    %(walk_forward_positive_ratio)s, %(walk_forward_mean_sharpe)s,
    %(walk_forward_total_trades)s,
    %(cross_asset_tested)s, %(cost_test_passed)s,
    %(regime_dependency_score)s, %(regime_notes)s,
    %(correlation_to_existing)s, %(capacity_estimate_usd)s,
    %(reuse_of_rejected)s, %(went_to_live)s, %(final_decision)s
)
ON CONFLICT (research_id, version)
DO UPDATE SET
    hypothesis = EXCLUDED.hypothesis,
    economic_hypothesis = COALESCE(EXCLUDED.economic_hypothesis, research_registry.economic_hypothesis),
    strategy_type = COALESCE(EXCLUDED.strategy_type, research_registry.strategy_type),
    status = EXCLUDED.status,
    stage = EXCLUDED.stage,
    rejection_reason = COALESCE(EXCLUDED.rejection_reason, research_registry.rejection_reason),
    failure_category = COALESCE(EXCLUDED.failure_category, research_registry.failure_category),
    data_quality_grade = COALESCE(EXCLUDED.data_quality_grade, research_registry.data_quality_grade),
    backtest_reliability_grade = COALESCE(EXCLUDED.backtest_reliability_grade, research_registry.backtest_reliability_grade),
    lookahead_audit_status = CASE
        WHEN research_registry.lookahead_audit_status != 'Unaudited'
        THEN research_registry.lookahead_audit_status
        ELSE EXCLUDED.lookahead_audit_status
    END,
    lookahead_audit_notes = COALESCE(EXCLUDED.lookahead_audit_notes, research_registry.lookahead_audit_notes),
    is_sharpe = COALESCE(EXCLUDED.is_sharpe, research_registry.is_sharpe),
    is_grade = COALESCE(EXCLUDED.is_grade, research_registry.is_grade),
    is_trades = COALESCE(EXCLUDED.is_trades, research_registry.is_trades),
    oos_sharpe = COALESCE(EXCLUDED.oos_sharpe, research_registry.oos_sharpe),
    oos_grade = COALESCE(EXCLUDED.oos_grade, research_registry.oos_grade),
    oos_trades = COALESCE(EXCLUDED.oos_trades, research_registry.oos_trades),
    walk_forward_grade = COALESCE(EXCLUDED.walk_forward_grade, research_registry.walk_forward_grade),
    walk_forward_windows = COALESCE(EXCLUDED.walk_forward_windows, research_registry.walk_forward_windows),
    walk_forward_positive_ratio = COALESCE(EXCLUDED.walk_forward_positive_ratio, research_registry.walk_forward_positive_ratio),
    walk_forward_mean_sharpe = COALESCE(EXCLUDED.walk_forward_mean_sharpe, research_registry.walk_forward_mean_sharpe),
    walk_forward_total_trades = COALESCE(EXCLUDED.walk_forward_total_trades, research_registry.walk_forward_total_trades),
    cross_asset_tested = COALESCE(EXCLUDED.cross_asset_tested, research_registry.cross_asset_tested),
    cost_test_passed = COALESCE(EXCLUDED.cost_test_passed, research_registry.cost_test_passed),
    regime_notes = COALESCE(EXCLUDED.regime_notes, research_registry.regime_notes),
    final_decision = COALESCE(EXCLUDED.final_decision, research_registry.final_decision)
RETURNING id, research_id, version, status, stage;
"""


def main():
    parser = argparse.ArgumentParser(description='Archive a research project into research_registry')
    parser.add_argument('research_dir', type=str, help='Path to research archive directory')
    parser.add_argument('--version', type=int, default=1, help='Research version (default: 1)')
    parser.add_argument('--economic-hypothesis', type=str, default=None, help='Economic hypothesis text')
    parser.add_argument('--dry-run', action='store_true', help='Print extracted data without writing to DB')
    args = parser.parse_args()

    dir_path = Path(args.research_dir).resolve()
    if not dir_path.exists():
        print(f"ERROR: Directory not found: {dir_path}")
        sys.exit(1)

    print("=" * 60)
    print(f"  Archiving: {dir_path.name}")
    print("=" * 60)

    # 1. Detect research_id
    research_id = detect_research_id(dir_path)
    print(f"  research_id: {research_id}")

    # 2. Parse all files
    record = {
        'research_id': research_id,
        'version': args.version,
        'parent_research_id': None,
        'hypothesis': '',
        'economic_hypothesis': None,
        'strategy_type': None,
        'status': 'Running',
        'stage': 'Idea',
        'rejection_reason': None,
        'failure_category': None,
        'data_quality_grade': None,
        'backtest_reliability_grade': None,
        'lookahead_audit_status': 'Unaudited',  # MUST default to Unaudited
        'lookahead_audit_notes': None,
        'is_sharpe': None,
        'is_grade': None,
        'is_trades': None,
        'oos_sharpe': None,
        'oos_grade': None,
        'oos_trades': None,
        'walk_forward_grade': None,
        'walk_forward_windows': None,
        'walk_forward_positive_ratio': None,
        'walk_forward_mean_sharpe': None,
        'walk_forward_total_trades': None,
        'cross_asset_tested': False,
        'cost_test_passed': None,
        'regime_dependency_score': None,
        'regime_notes': None,
        'correlation_to_existing': None,
        'capacity_estimate_usd': None,
        'reuse_of_rejected': None,
        'went_to_live': False,
        'final_decision': None,
    }

    # 3. Layer in parsed data
    # Hypothesis
    h = parse_hypothesis_md(dir_path / 'hypothesis.md')
    record.update({k: v for k, v in h.items() if v})

    # Protocol
    p = parse_protocol_md(dir_path / 'protocol.md')
    record.update({k: v for k, v in p.items() if v})

    # Decision (grades, status, failure)
    d = parse_decision_md(dir_path / 'decision.md')
    record.update({k: v for k, v in d.items() if v})

    # IS
    is_data = parse_is_results(dir_path)
    record.update({k: v for k, v in is_data.items() if v})
    if record.get('is_sharpe'):
        record['stage'] = 'IS'  # At minimum reached IS

    # OOS
    oos_data = parse_oos_results(dir_path)
    record.update({k: v for k, v in oos_data.items() if v})
    if record.get('oos_sharpe') is not None:
        record['stage'] = 'OOS'

    # Walk-Forward
    wf_data = parse_walk_forward(dir_path)
    record.update({k: v for k, v in wf_data.items() if v})
    if record.get('walk_forward_windows'):
        record['stage'] = 'WalkForward'

    # Post-Mortem
    pm_data = parse_post_mortem(dir_path)
    record.update({k: v for k, v in pm_data.items() if v})

    # Economic hypothesis override
    if args.economic_hypothesis:
        record['economic_hypothesis'] = args.economic_hypothesis
    elif not record.get('economic_hypothesis'):
        auto_eh = detect_economic_hypothesis(dir_path, record.get('hypothesis', ''))
        if auto_eh:
            record['economic_hypothesis'] = auto_eh

    # Strategy type fallback
    if not record.get('strategy_type'):
        record['strategy_type'] = detect_strategy_type_from_path(dir_path)

    # If rejected, ensure stage is Archived
    if record.get('status') == 'Rejected':
        record['stage'] = 'Archived'

    # 4. Print summary
    print()
    print("  Extracted Record:")
    print("  " + "-" * 56)
    for k, v in sorted(record.items()):
        if v is not None:
            print(f"  {k:35s} = {v}")

    # 5. Report fields that need manual input
    manual_fields = []
    if not record.get('economic_hypothesis'):
        manual_fields.append('economic_hypothesis')
    if not record.get('regime_dependency_score'):
        manual_fields.append('regime_dependency_score')
    if not record.get('capacity_estimate_usd'):
        manual_fields.append('capacity_estimate_usd')
    if not record.get('correlation_to_existing'):
        manual_fields.append('correlation_to_existing')
    if record.get('cost_test_passed') is None:
        manual_fields.append('cost_test_passed')
    if record.get('cross_asset_tested') is False:
        manual_fields.append('cross_asset_tested')
    if record.get('lookahead_audit_status') == 'Unaudited':
        manual_fields.append('lookahead_audit_status (requires human confirmation)')

    if manual_fields:
        print()
        print("  ⚠️  Fields requiring manual input:")
        for f in manual_fields:
            print(f"    - {f}")

    if args.dry_run:
        print("\n  [DRY RUN] No database writes performed.")
        return

    # 6. UPSERT
    print()
    print("  Writing to research_registry...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(UPSERT_SQL, record)
        row = cur.fetchone()
        conn.commit()

        if row:
            print(f"  ✅ UPSERT succeeded: id={row[0]}, {row[1]} v{row[2]}, status={row[3]}, stage={row[4]}")
        else:
            print("  ⚠️  UPSERT returned no rows (unexpected)")

        # 7. Read back full record for verification
        print()
        print("  Full record in database:")
        print("  " + "-" * 56)
        cur.execute("SELECT * FROM research_registry WHERE research_id = %s AND version = %s", (research_id, args.version))
        cols = [desc[0] for desc in cur.description]
        full_row = cur.fetchone()
        if full_row:
            for col, val in zip(cols, full_row):
                if val is not None:
                    print(f"  {col:35s} = {val}")

        cur.close()
        conn.close()

    except psycopg2.Error as e:
        print(f"  ❌ Database error: {e}")
        sys.exit(1)

    print()
    print("  Done.")


if __name__ == '__main__':
    main()
