# -*- coding: utf-8 -*-
"""Orchestrate evidence-based research review (rules + stats + optional LLM)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .llm_review import run_llm_review
from .research_gates import evaluate_rules
from .stat_review import run_stat_review
from .store import save_review
from .version import ENGINE_NAME, ENGINE_VERSION

STRATEGIES_FILE = Path(os.environ.get("STRATEGIES_FILE", "/app/data/store/strategies.json"))


def _duplicate_check(cand: dict[str, Any]) -> dict[str, Any]:
    """Lightweight overlap: same type count in strategies.json."""
    if not STRATEGIES_FILE.exists():
        return {"max_type_overlap": 0.0}
    try:
        items = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
        stype = str(cand.get("type") or "custom")
        same = sum(1 for x in items if str(x.get("type")) == stype)
        total = max(len(items), 1)
        return {
            "type": stype,
            "same_type_count": same,
            "max_type_overlap": round(same / total, 3),
        }
    except Exception:
        return {"max_type_overlap": 0.0}


def compute_research_score(
    rule_score: float,
    stat_score: float | None,
    llm_score: float | None = None,
    *,
    flags: list[str] | None = None,
) -> float:
    if stat_score is None:
        score = float(rule_score)
    elif llm_score is None:
        score = 0.55 * rule_score + 0.45 * float(stat_score)
    else:
        score = 0.35 * rule_score + 0.40 * float(stat_score) + 0.25 * float(llm_score)
    if flags:
        score -= min(10, len(flags) * 2)
    return round(max(0.0, min(100.0, score)), 1)


def _status_label(
    gate_pass: bool,
    scores: dict[str, Any],
    llm: dict[str, Any],
    *,
    has_stat: bool = False,
) -> str:
    if not gate_pass:
        return "Reject"
    rec = (llm or {}).get("recommendation")
    if rec in ("Paper Candidate", "Watchlist", "Needs More Data", "Reject"):
        return rec
    # Rule-only baseline: eligible for deeper review, not paper yet
    if not has_stat:
        return "Review"
    overall = float(scores.get("overall") or 0)
    if overall >= 85:
        return "Paper Candidate"
    if overall >= 70:
        return "Review"
    return "Watchlist"


def run_review(
    cand: dict[str, Any],
    metrics: dict[str, Any],
    *,
    backtest: dict[str, Any] | None = None,
    strategy_id: str | None = None,
    run_stat: bool | None = None,
    run_llm: bool | None = None,
) -> dict[str, Any]:
    """Full review pipeline. Rules decide gate_pass; LLM only reports."""
    if run_stat is None:
        run_stat = os.environ.get("RESEARCH_REVIEW_STAT", "1").strip() not in {
            "0",
            "false",
            "no",
        }
    if run_llm is None:
        run_llm = os.environ.get("RESEARCH_REVIEW_LLM", "0").strip() in {
            "1",
            "true",
            "yes",
        }

    clean_metrics = {k: v for k, v in (metrics or {}).items() if k != "raw"}

    stat: dict[str, Any] = {}
    if run_stat and cand.get("code"):
        stat = run_stat_review(cand, backtest)

    duplicate = _duplicate_check(cand)
    rules = evaluate_rules(metrics, stat=stat or None, duplicate=duplicate)

    llm: dict[str, Any] = {}
    if run_llm and rules["research_gate_pass"]:
        llm_input = {
            "hypothesis": cand.get("name"),
            "type": cand.get("type"),
            "description": cand.get("description"),
            "symbols": cand.get("symbols"),
            "metrics": clean_metrics,
            "rule_checks": rules.get("checks"),
            "statistical": stat,
            "duplicate": duplicate,
        }
        llm = run_llm_review(llm_input)

    llm_score = llm.get("llm_score") if llm and not llm.get("skipped") else None
    stat_score = stat.get("stat_score") if stat else None
    rule_score = float(rules["rule_score"])
    overall = compute_research_score(
        rule_score,
        float(stat_score) if stat_score is not None else None,
        llm_score,
        flags=rules.get("flags"),
    )

    scores = {
        "overall": overall,
        "rule": rule_score,
        "stat": stat_score,
        "economic": llm_score,  # from LLM quality grade when present
        "novelty": None,  # Phase 2
        "capacity": None,  # Phase 2
        "llm": llm_score,
    }

    evidence = {
        "metrics": clean_metrics,
        "trades": clean_metrics.get("trades"),
        "is_oos": {
            "is_sharpe": stat.get("is_sharpe"),
            "oos_sharpe": stat.get("oos_sharpe"),
            "oos_is_ratio": stat.get("oos_is_ratio"),
            "walk_forward": stat.get("walk_forward"),
            "is_period": stat.get("is_period"),
            "oos_period": stat.get("oos_period"),
        }
        if stat
        else None,
        "rolling_sharpe_regime": stat.get("regime_score") if stat else None,
        "parameter_sensitivity": {
            "stability": stat.get("parameter_stability"),
            "worst_ratio": stat.get("param_worst_ratio"),
            "shocks": (stat.get("param_shocks") or [])[:6],
        }
        if stat
        else None,
        "duplicate": duplicate,
        "rule_checks": rules.get("checks"),
        "llm_report": {
            "research_quality": llm.get("research_quality"),
            "economic_rationale": llm.get("economic_rationale"),
            "possible_bias": llm.get("possible_bias"),
            "missing_validation": llm.get("missing_validation"),
            "risks": llm.get("risks"),
            "novelty_note": llm.get("novelty_note"),
        }
        if llm and not llm.get("skipped") and not llm.get("error")
        else None,
    }

    status = _status_label(rules["research_gate_pass"], scores, llm, has_stat=bool(stat))
    recommendation = (llm.get("recommendation") if llm else None) or status

    review = {
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": cand.get("name"),
        "type": cand.get("type"),
        "source": cand.get("source"),
        "symbols": cand.get("symbols"),
        "research_gate_pass": rules["research_gate_pass"],
        "status": status,
        "research_score": overall,
        "scores": scores,
        "reason_codes": rules.get("reason_codes") or [],
        "rule_score": rule_score,
        "stat_score": stat_score,
        "llm_score": llm_score,
        "rules": rules,
        "statistical": stat,
        "duplicate": duplicate,
        "llm": llm,
        "evidence": evidence,
        "recommendation": recommendation,
        "promote_eligible": rules["research_gate_pass"],
        "layers_run": {
            "rules": True,
            "stat": bool(stat),
            "llm": bool(llm) and not llm.get("skipped"),
        },
    }

    if strategy_id:
        save_review(strategy_id, review)
    return review
