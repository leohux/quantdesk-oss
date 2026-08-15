# -*- coding: utf-8 -*-
"""LLM hypothesis dump for leftover alpha search (no DB required).

Uses SiliconFlow via agents.common.call_llm. Does not pick a winner.

Usage:
  python -m research.alpha_v2.run_llm_proposals
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json as _json
import os
import re
import ssl
import urllib.request

OUT = ROOT / "data" / "research" / "alpha_v2_llm_proposals.json"

SYSTEM = """你是 QuantDesk 研究体系中的 Research Proposal Agent。
每个候选必须包含清晰经济机制、可证伪预测、可从 SEC companyfacts 或行情/新闻构建的特征。
不要提出均线、突破、earnings_yield 本身、或已死的 CS combo 加权。
只输出一个 JSON 对象：{"candidates":[{"title":"","economic_logic":"","testable_prediction":"","required_features":[],"estimated_theoretical_edge_bps":null,"risk_notes":""}]}
"""


def call_llm(user_content: str) -> str:
    if os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("QUANTDESK_AGENT_API_KEY"):
        base = os.environ.get("QUANTDESK_AGENT_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
        model = os.environ.get("QUANTDESK_AGENT_MODEL", "deepseek-ai/DeepSeek-V4-Pro")
        key = (
            os.environ.get("SILICONFLOW_API_KEY", "").strip()
            or os.environ.get("QUANTDESK_AGENT_API_KEY", "").strip()
        )
        extra = {"enable_thinking": False, "max_tokens": 3500}
    else:
        base = os.environ.get("MIMO_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1").rstrip("/")
        model = os.environ.get("MIMO_MODEL", "mimo-v2.5-pro")
        key = os.environ.get("MIMO_API_KEY", "").strip()
        extra = {"max_completion_tokens": 3500}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.55,
        **extra,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=_json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=180, context=ssl.create_default_context()) as resp:
        body = _json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    return content if content else (msg.get("reasoning_content") or "").strip()


def parse_json_response(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON in model response: " + raw[:400])
    return _json.loads(match.group(0))

DEAD = [
    {
        "research_id": "TrackA-combo",
        "status": "Rejected",
        "economic_hypothesis": (
            "vol_adj_rev_5 + near_high + earnings_yield + neg_leverage CS combo. "
            "OOS Sharpe 1.21 but residual Sharpe ~0 after SPY+200MA and EY sleeve, R2=0.85."
        ),
        "failure_category": "NoIndependentAlpha",
    },
    {
        "research_id": "Wash-round1",
        "status": "Rejected",
        "economic_hypothesis": (
            "MA trend / panda composite / skip5 momentum. Survivorship+PIT+cost+attribution "
            "show market timing / beta, not alpha."
        ),
        "failure_category": "BetaMasqueradingAsAlpha",
    },
    {
        "research_id": "PEAD-t20",
        "status": "Archived",
        "economic_hypothesis": (
            "Post-earnings announcement drift T+20. PASS_AS_EVENT only "
            "(early window share 0.59). Not continuous CS alpha. LIVE LOCKED."
        ),
        "failure_category": "EventNotContinuous",
    },
    {
        "research_id": "046bfa-surge",
        "status": "Rejected",
        "economic_hypothesis": "Intraday surge continuation / ATR breakout. research_archive_gate_fail.",
        "failure_category": "OverfitIntraday",
    },
]

DIRECTIONS = [
    (
        "residual_quality_after_ey",
        "US S&P PIT cross-section T+5 RankIC. Earnings yield already explains the "
        "value sleeve. Propose 6 hypotheses for residual QUALITY / ACCRUALS / INVESTMENT "
        "alpha that must survive CS residualization vs earnings_yield and GICS sector. "
        "Do not propose momentum, MA, breakout, or EY itself. Features must be buildable "
        "from SEC companyfacts (ni, ocf, assets, gp, debt, revenue, eps) + daily close.",
    ),
    (
        "industry_neutral_idio",
        "US S&P PIT T+5 RankIC. Propose 6 hypotheses for industry-neutral idiosyncratic "
        "return (stock minus sector and market) that is NOT short-term reversal already "
        "tested (vol_adj_rev_5, resid_mom_20/60 failed Gate12-A after EY residual). "
        "No MA/breakout. Prefer delayed overreaction, lottery, crowding, or liquidity.",
    ),
    (
        "event_book_not_cs",
        "Not a continuous CS book. Propose 6 EVENT hypotheses (earnings revision, "
        "8-K, news surprise, volume shock) as discrete event modules. PEAD T+20 already "
        "PASS_AS_EVENT. Need something with a different mechanism or a PEAD filter that "
        "is not just 'buy every beat'. Must be testable with NASDAQ earnings dates "
        "and/or news_trader signals.jsonl.",
    ),
]


def _one(direction_id: str, prompt: str, n: int = 6) -> dict:
    payload = {
        "direction": prompt,
        "n_candidates": n,
        "existing_registry_summary": DEAD,
        "constraint": (
            "LIVE is LOCKED. Do not retune archived names. Each candidate must name "
            "who is mispricing and why it is not instantly arbitraged. "
            "estimated_theoretical_edge_bps after 10bps round-trip, or null."
        ),
    }
    raw = call_llm(json.dumps(payload, ensure_ascii=False, indent=2))
    parsed = parse_json_response(raw)
    return {
        "direction_id": direction_id,
        "prompt": prompt,
        "candidates": parsed.get("candidates") or [],
        "raw_preview": raw[:400],
    }


def main() -> None:
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "live": "LOCKED",
        "directions": [],
    }
    for did, prompt in DIRECTIONS:
        print(f"=== LLM {did} ===", flush=True)
        try:
            block = _one(did, prompt)
        except Exception as exc:
            print(f"  FAIL {did}: {exc}", flush=True)
            block = {"direction_id": did, "error": str(exc), "candidates": []}
        n = len(block.get("candidates") or [])
        print(f"  got {n} candidates", flush=True)
        for i, c in enumerate(block.get("candidates") or [], 1):
            print(f"  [{i}] {c.get('title')}", flush=True)
        report["directions"].append(block)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
