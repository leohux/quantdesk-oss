# -*- coding: utf-8 -*-
"""Gate12-A: single-factor persistence gate."""
from __future__ import annotations

from typing import Any


def evaluate_gate12a(
    rankic_mean: float,
    rolling_hit: float,
    *,
    min_rankic: float = 0.02,
    min_roll_hit: float = 0.60,
) -> dict[str, Any]:
    checks = [
        {
            "gate": "factor_rankic_mean",
            "pass": (rankic_mean or 0) > min_rankic,
            "value": rankic_mean,
            "threshold": min_rankic,
        },
        {
            "gate": "rolling_rankic_positive_share",
            "pass": (rolling_hit or 0) >= min_roll_hit,
            "value": rolling_hit,
            "threshold": min_roll_hit,
        },
    ]
    return {"pass": all(c["pass"] for c in checks), "checks": checks}
