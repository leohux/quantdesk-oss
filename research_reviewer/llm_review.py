# -*- coding: utf-8 -*-
"""Layer 3: LLM Research Review — explains evidence, never final PASS/REJECT."""
from __future__ import annotations

import json
import os
from typing import Any

from alpha_miner.llm import chat, parse_json

SYSTEM = """You are an evidence-based quantitative research reviewer for US equities.
You receive deterministic backtest metrics, rule checks, and statistical tests.
You do NOT decide PASS/REJECT. Rules already decided eligibility.
Your job: assess research quality, economic rationale, biases, missing validation.
Return ONLY valid JSON with keys:
research_quality (A|B|C|D),
confidence (0-100 integer),
economic_rationale (string, 2-4 sentences),
possible_bias (array of strings),
missing_validation (array of strings),
recommendation (one of: Reject, Watchlist, Paper Candidate, Needs More Data),
risks (array of strings),
novelty_note (string, brief)
Be skeptical. Cite only the evidence provided."""


def run_llm_review(evidence: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("RESEARCH_LLM_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return {"skipped": True, "reason": "RESEARCH_LLM_DISABLED"}

    user = (
        "Review this strategy research evidence and return JSON only:\n\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)[:12000]
    )
    try:
        raw = chat(SYSTEM, user, temperature=0.2, max_tokens=1800)
        data = parse_json(raw)
        if not isinstance(data, dict):
            raise ValueError("LLM did not return object")
        grade = str(data.get("research_quality") or "C").upper()[:1]
        if grade not in "ABCD":
            grade = "C"
        data["research_quality"] = grade
        data["llm_score"] = {"A": 95, "B": 82, "C": 65, "D": 40}.get(grade, 65)
        return data
    except Exception as exc:
        return {
            "error": str(exc)[:400],
            "research_quality": "C",
            "llm_score": 50,
            "recommendation": "Needs More Data",
            "economic_rationale": "LLM review unavailable; rely on rule/stat layers.",
        }
