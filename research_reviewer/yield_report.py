# -*- coding: utf-8 -*-
"""Research Yield funnel + reject reason distribution."""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import STORE
from .version import ENGINE_VERSION

STRATEGIES_FILE = Path(os.environ.get("STRATEGIES_FILE", "/app/data/store/strategies.json"))
ALPHA_RUNS = Path(
    os.environ.get("ALPHA_MINER_STORE", "/app/data/store/alpha_miner")
) / "runs.jsonl"
YIELD_OUT = STORE / "yield_summary.json"


def _load_strategies() -> list[dict]:
    if not STRATEGIES_FILE.exists():
        return []
    return json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))


def _load_reviews() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not STORE.exists():
        return out
    for path in STORE.glob("*.json"):
        if path.name in {"yield_summary.json", "index.json"}:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("strategy_id") or path.stem
            out[sid] = data
        except Exception:
            continue
    return out


def _count_runs() -> dict[str, int]:
    """Optional: count alpha-miner run outcomes from runs.jsonl."""
    counts = Counter()
    if not ALPHA_RUNS.exists():
        return dict(counts)
    try:
        with ALPHA_RUNS.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                st = rec.get("status") or "unknown"
                counts[st] += 1
                if rec.get("reject_stage") == "research_gates":
                    counts["research_gate_reject"] += 1
    except Exception:
        pass
    return dict(counts)


def build_yield_report() -> dict[str, Any]:
    strategies = _load_strategies()
    reviews = _load_reviews()

    total = len(strategies)
    mined = [
        s
        for s in strategies
        if str(s.get("id", "")).startswith(("alpha-", "mimo-"))
    ]
    manual = [s for s in strategies if s not in mined]

    # Hard-gate survivors ≈ promoted into strategies.json as mined
    hard_pass = len(mined)

    reviewed = list(reviews.values())
    gate_pass = [r for r in reviewed if r.get("research_gate_pass")]
    gate_fail = [r for r in reviewed if r.get("research_gate_pass") is False]
    with_stat = [r for r in reviewed if (r.get("layers_run") or {}).get("stat")]
    stat_pass = [
        r
        for r in with_stat
        if r.get("research_gate_pass")
        and float((r.get("scores") or {}).get("stat") or r.get("stat_score") or 0) >= 70
    ]
    paper = [
        r
        for r in reviewed
        if r.get("status") == "Paper Candidate"
        or r.get("recommendation") == "Paper Candidate"
    ]
    enabled = [s for s in strategies if s.get("enabled")]

    reason_counter: Counter[str] = Counter()
    for r in gate_fail:
        codes = r.get("reason_codes") or []
        if not codes:
            # fallback from checks
            for c in (r.get("rules") or {}).get("checks") or []:
                if c.get("status") == "FAIL" and c.get("reason_code"):
                    codes.append(c["reason_code"])
        for code in codes or ["UNKNOWN"]:
            reason_counter[code] += 1

    # Score distribution for reviewed
    score_bins = {"0-50": 0, "50-70": 0, "70-85": 0, "85-100": 0}
    for r in reviewed:
        sc = float(r.get("research_score") or (r.get("scores") or {}).get("overall") or 0)
        if sc < 50:
            score_bins["0-50"] += 1
        elif sc < 70:
            score_bins["50-70"] += 1
        elif sc < 85:
            score_bins["70-85"] += 1
        else:
            score_bins["85-100"] += 1

    funnel = [
        {"stage": "Alpha generated (mined in registry)", "count": hard_pass},
        {"stage": "Hard Gates pass (promoted to strategies.json)", "count": hard_pass},
        {"stage": "Research reviewed (engine baseline)", "count": len(reviewed)},
        {"stage": "Research Gates pass", "count": len(gate_pass)},
        {"stage": "Stat reviewed", "count": len(with_stat)},
        {"stage": "Stat quality >= 70", "count": len(stat_pass)},
        {"stage": "Paper Candidate", "count": len(paper)},
        {"stage": "Enabled / Live-ish", "count": len(enabled)},
    ]

    reject_total = sum(reason_counter.values()) or 1
    reject_reasons = [
        {
            "code": code,
            "count": count,
            "pct": round(100.0 * count / reject_total, 1),
        }
        for code, count in reason_counter.most_common()
    ]

    report = {
        "engine_version": ENGINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory": {
            "total_strategies": total,
            "mined": hard_pass,
            "manual": len(manual),
            "reviewed": len(reviewed),
            "research_gate_pass": len(gate_pass),
            "research_gate_fail": len(gate_fail),
        },
        "funnel": funnel,
        "reject_reasons": reject_reasons,
        "score_bins": score_bins,
        "alpha_miner_runs": _count_runs(),
        "yield_rates": {
            "research_pass_rate": round(len(gate_pass) / max(len(reviewed), 1) * 100, 2),
            "paper_rate_of_reviewed": round(len(paper) / max(len(reviewed), 1) * 100, 2),
            "paper_rate_of_mined": round(len(paper) / max(hard_pass, 1) * 100, 4),
        },
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    print(f"=== Research Yield (engine v{report['engine_version']}) ===")
    print(f"generated_at: {report['generated_at']}")
    print()
    print("Funnel:")
    for row in report["funnel"]:
        print(f"  {row['stage']:<55} {row['count']:>8}")
    print()
    inv = report["inventory"]
    print(
        f"Inventory: mined={inv['mined']} reviewed={inv['reviewed']} "
        f"pass={inv['research_gate_pass']} fail={inv['research_gate_fail']}"
    )
    yr = report["yield_rates"]
    print(
        f"Yield: research_pass={yr['research_pass_rate']}% "
        f"paper/reviewed={yr['paper_rate_of_reviewed']}% "
        f"paper/mined={yr['paper_rate_of_mined']}%"
    )
    print()
    print("Reject reasons:")
    for row in report["reject_reasons"][:15]:
        print(f"  {row['code']:<24} {row['count']:>6}  ({row['pct']}%)")
    print()
    print("Score bins:", report["score_bins"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Research Yield report")
    p.add_argument("--save", action="store_true", help="Write yield_summary.json")
    args = p.parse_args(argv)

    report = build_yield_report()
    print_report(report)
    if args.save:
        STORE.mkdir(parents=True, exist_ok=True)
        YIELD_OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {YIELD_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
