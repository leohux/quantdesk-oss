# -*- coding: utf-8 -*-
"""Hard Gate 12 — RankIC / IC discovery gate."""
from __future__ import annotations

from typing import Any


def evaluate_gate12(
    ic_summary: dict[str, Any],
    rankic_summary: dict[str, Any],
    rolling_hit: float,
    *,
    min_ic: float = 0.02,
    min_rankic: float = 0.03,
    min_roll_hit: float = 0.70,
) -> dict[str, Any]:
    checks = [
        {
            "gate": "ic_mean",
            "pass": (ic_summary.get("mean") or 0) > min_ic,
            "value": ic_summary.get("mean"),
            "threshold": min_ic,
        },
        {
            "gate": "rankic_mean",
            "pass": (rankic_summary.get("mean") or 0) > min_rankic,
            "value": rankic_summary.get("mean"),
            "threshold": min_rankic,
        },
        {
            "gate": "rolling_rankic_positive_share",
            "pass": (rolling_hit or 0) >= min_roll_hit,
            "value": rolling_hit,
            "threshold": min_roll_hit,
        },
    ]
    return {
        "pass": all(c["pass"] for c in checks),
        "checks": checks,
        "note": (
            "Gate12 only measures predictive rank power. "
            "Gate13 cost/reality and Gate14 attribution still required before Live."
        ),
    }
