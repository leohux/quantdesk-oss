# -*- coding: utf-8 -*-
"""
Research Proposal Agent — 生成可证伪假设候选清单 + Registry 查重

用法：
    python research_proposal_agent.py "Earnings Drift"
    python research_proposal_agent.py "Earnings Drift" --n 8
"""
import sys
import json
import difflib

from common import call_llm, parse_json_response, fetch_all_hypotheses_summary
from prompts import RESEARCH_PROPOSAL_SYSTEM_PROMPT


def flag_similar_existing(candidate_title: str, existing: list, threshold: float = 0.45) -> list:
    """粗略文本相似度查重，防止 Agent 生成的假设和已有记录重复。
    这是字面相似度，不是语义相似度 —— 最终判断仍需人工看
    economic_hypothesis 是否本质相同，这里只是先筛出需要人工复核的候选。
    """
    hits = []
    for item in existing:
        for field in ("hypothesis", "economic_hypothesis"):
            text = item.get(field) or ""
            if not text:
                continue
            ratio = difflib.SequenceMatcher(None, candidate_title.lower(), text.lower()).ratio()
            if ratio >= threshold:
                hits.append({
                    "research_id": item["research_id"],
                    "status": item["status"],
                    "matched_field": field,
                    "similarity": round(ratio, 2),
                })
    return hits


def propose_research(direction: str, n: int = 6) -> list:
    existing = fetch_all_hypotheses_summary()

    agent_input = {
        "direction": direction,
        "n_candidates": n,
        "existing_registry_summary": existing,
        "constraint": (
            "每个假设必须给出明确经济逻辑，不能只是技术形态描述；"
            "必须给出可证伪的具体预测；必须避免与 Rejected 记录本质重复"
        ),
    }

    response_text = call_llm(
        system_prompt=RESEARCH_PROPOSAL_SYSTEM_PROMPT,
        user_content=json.dumps(agent_input, ensure_ascii=False, indent=2),
        max_tokens=3000,
        temperature=0.6,  # Proposal 生成希望更发散一些，Post-Mortem 相反要更保守
    )
    result = parse_json_response(response_text)
    candidates = result.get("candidates", [])

    for c in candidates:
        c["similarity_check"] = flag_similar_existing(c.get("title", ""), existing)

    _print_candidates(direction, candidates)
    return candidates


def _print_candidates(direction: str, candidates: list):
    print(f"\n=== Research Proposals: {direction} ===\n")
    for i, c in enumerate(candidates, 1):
        print(f"[{i}] {c.get('title')}")
        print(f"    经济逻辑: {c.get('economic_logic')}")
        print(f"    可证伪预测: {c.get('testable_prediction')}")
        print(f"    所需特征: {', '.join(c.get('required_features', []))}")
        print(f"    预估理论 edge: {c.get('estimated_theoretical_edge_bps')} bp")
        print(f"    风险提示: {c.get('risk_notes')}")
        if c["similarity_check"]:
            print(f"    ⚠️  与已有记录字面相似，需人工复核: {c['similarity_check']}")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('用法: python research_proposal_agent.py "方向描述" [--n 6]')
        sys.exit(1)
    direction_arg = sys.argv[1]
    n_arg = 6
    if "--n" in sys.argv:
        n_arg = int(sys.argv[sys.argv.index("--n") + 1])
    propose_research(direction_arg, n=n_arg)
