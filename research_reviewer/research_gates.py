# -*- coding: utf-8 -*-
"""Layer 1: Rule Engine — 100% programmatic, no LLM tokens."""
from __future__ import annotations

import os
from typing import Any

# Stable reject reason codes for feedback loop / Research Yield
REASON_CODES = {
    "sample_size": "LOW_TRADES",
    "sharpe": "LOW_SHARPE",
    "max_drawdown": "HIGH_DRAWDOWN",
    "oos_is": "OVERFIT",
    "walk_forward": "WALK_FORWARD_FAIL",
    "parameter_stability": "PARAM_UNSTABLE",
    "duplicate_research": "HIGH_CORRELATION",
    "survivorship_bias": "SURVIVORSHIP_BIAS",
    "point_in_time_universe": "PIT_UNIVERSE_FAIL",
    "reality_gate": "REALITY_GATE_FAIL",
    "factor_attribution": "NO_INDEPENDENT_ALPHA",
}


def _cfg() -> dict[str, float]:
    return {
        "min_trades": float(os.environ.get("RESEARCH_GATE_MIN_TRADES", "30")),
        "min_sharpe": float(os.environ.get("RESEARCH_GATE_MIN_SHARPE", "1.0")),
        "max_dd": float(os.environ.get("RESEARCH_GATE_MAX_DD", "0.30")),
        "min_oos_is": float(os.environ.get("RESEARCH_GATE_MIN_OOS_IS", "0.60")),
        "max_correlation": float(os.environ.get("RESEARCH_GATE_MAX_CORRELATION", "0.85")),
    }


def _norm_dd(mdd: float) -> float:
    v = abs(float(mdd))
    return v / 100.0 if v > 1.5 else v


def evaluate_rules(
    metrics: dict[str, Any],
    *,
    stat: dict[str, Any] | None = None,
    duplicate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured rule results. research_gate_pass = all mandatory gates PASS."""
    cfg = _cfg()
    checks: list[dict[str, Any]] = []

    trades = float(metrics.get("trades") or 0)
    sharpe = float(metrics.get("sharpe") or 0)
    mdd = _norm_dd(float(metrics.get("max_drawdown") or metrics.get("max_drawdown_pct") or 0))

    def add(
        gate: str,
        status: str,
        reason: str,
        *,
        mandatory: bool = True,
        code: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "gate": gate,
            "status": status,
            "reason": reason,
            "mandatory": mandatory,
        }
        if status in ("FAIL", "FLAG"):
            item["reason_code"] = code or REASON_CODES.get(gate, gate.upper())
        checks.append(item)

    if trades < cfg["min_trades"]:
        add("sample_size", "FAIL", f"Trades={trades:.0f} < {cfg['min_trades']:.0f}")
    else:
        add("sample_size", "PASS", f"Trades={trades:.0f}")

    if sharpe < cfg["min_sharpe"]:
        add("sharpe", "FAIL", f"Sharpe={sharpe:.3f} < {cfg['min_sharpe']:.2f}")
    else:
        add("sharpe", "PASS", f"Sharpe={sharpe:.3f}")

    if mdd > cfg["max_dd"]:
        add("max_drawdown", "FAIL", f"MaxDD={mdd:.1%} > {cfg['max_dd']:.0%}")
    else:
        add("max_drawdown", "PASS", f"MaxDD={mdd:.1%}")

    if stat:
        oos_is = stat.get("oos_is_ratio")
        if oos_is is not None:
            if float(oos_is) < cfg["min_oos_is"]:
                add(
                    "oos_is",
                    "FAIL",
                    f"OOS/IS={float(oos_is):.2f} < {cfg['min_oos_is']:.2f} (overfit risk)",
                )
            else:
                add("oos_is", "PASS", f"OOS/IS={float(oos_is):.2f}")

        wf = stat.get("walk_forward")
        if wf == "FAIL":
            add("walk_forward", "FAIL", "Walk-forward OOS underperforms IS")
        elif wf == "PASS":
            add("walk_forward", "PASS", "Walk-forward stable")

        stability = stat.get("parameter_stability")
        if stability == "BAD":
            add(
                "parameter_stability",
                "FAIL",
                "Sharpe collapses under ±10% param perturbation",
            )
        elif stability in ("GOOD", "OK"):
            add("parameter_stability", "PASS", f"Parameter stability={stability}")

    if duplicate:
        corr = duplicate.get("max_type_overlap")
        if corr is not None and float(corr) >= cfg["max_correlation"]:
            add(
                "duplicate_research",
                "FLAG",
                f"Type overlap={float(corr):.0%} (similar strategies exist)",
                mandatory=False,
            )
        else:
            add("duplicate_research", "PASS", "Low overlap with existing research")

    # Hard Gate 8 — Survivorship Bias Gate (optional payload from CS research)
    # Expected metrics keys when provided:
    #   survivorship_gate_pass: bool
    #   oos_sharpe_debiased, oos_ann_debiased, oos_maxdd_debiased
    if "survivorship_gate_pass" in metrics or metrics.get("hard_gate8"):
        hg = metrics.get("hard_gate8") or {}
        passed_sb = bool(metrics.get("survivorship_gate_pass", hg.get("pass", False)))
        detail = (
            f"debias_S={float(metrics.get('oos_sharpe_debiased', hg.get('sharpe', 0))):.2f} "
            f"ann={float(metrics.get('oos_ann_debiased', hg.get('ann', 0))):.1%} "
            f"DD={float(metrics.get('oos_maxdd_debiased', hg.get('maxdd', 0))):.1%}"
        )
        if not passed_sb:
            add(
                "survivorship_bias",
                "FAIL",
                f"Hard Gate 8 FAIL ({detail}); need hist-universe+WF+cost/slippage",
            )
        else:
            add("survivorship_bias", "PASS", f"Hard Gate 8 PASS ({detail})")

    # Hard Gate 9 — Point-in-Time Universe Gate
    if "pit_gate_pass" in metrics or metrics.get("hard_gate9"):
        hg9 = metrics.get("hard_gate9") or {}
        passed_pit = bool(metrics.get("pit_gate_pass", hg9.get("pass", False)))
        detail = (
            f"PIT_S={float(metrics.get('oos_sharpe_pit', hg9.get('sharpe', 0))):.2f} "
            f"ann={float(metrics.get('oos_ann_pit', hg9.get('ann', 0))):.1%} "
            f"DD={float(metrics.get('oos_maxdd_pit', hg9.get('maxdd', 0))):.1%} "
            f"year_hit={float(metrics.get('stable_year_hit', hg9.get('stable_year_hit', 0))):.0%}"
        )
        if not passed_pit:
            add(
                "point_in_time_universe",
                "FAIL",
                f"Hard Gate 9 FAIL ({detail})",
            )
        else:
            add("point_in_time_universe", "PASS", f"Hard Gate 9 PASS ({detail})")

    # Hard Gate 10 — Reality Gate
    if "reality_gate_pass" in metrics or metrics.get("hard_gate10"):
        hg10 = metrics.get("hard_gate10") or {}
        passed_r = bool(metrics.get("reality_gate_pass", hg10.get("pass", False)))
        detail = (
            f"S={float(metrics.get('oos_sharpe_reality', hg10.get('sharpe', 0))):.2f} "
            f"DD={float(metrics.get('oos_maxdd_reality', hg10.get('maxdd', 0))):.1%}"
        )
        if not passed_r:
            add("reality_gate", "FAIL", f"Hard Gate 10 FAIL ({detail})")
        else:
            add("reality_gate", "PASS", f"Hard Gate 10 PASS ({detail})")

    mandatory_fails = [c for c in checks if c["mandatory"] and c["status"] == "FAIL"]
    flags = [c for c in checks if c["status"] == "FLAG"]
    reason_codes = sorted(
        {
            c["reason_code"]
            for c in checks
            if c.get("reason_code") and c["status"] in ("FAIL", "FLAG")
        }
    )
    passed = len(mandatory_fails) == 0

    return {
        "research_gate_pass": passed,
        "checks": checks,
        "fails": [c["reason"] for c in mandatory_fails],
        "flags": [c["reason"] for c in flags],
        "reason_codes": reason_codes,
        "rule_score": _rule_score(checks),
    }


def _rule_score(checks: list[dict[str, Any]]) -> float:
    if not checks:
        return 0.0
    weights = {"PASS": 1.0, "FLAG": 0.5, "FAIL": 0.0}
    return round(sum(weights.get(c["status"], 0) for c in checks) / len(checks) * 100, 1)
