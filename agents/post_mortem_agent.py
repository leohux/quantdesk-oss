# -*- coding: utf-8 -*-
"""
Post-Mortem Agent — QuantDesk Research Pipeline（最高优先级 Agent）

用途：读取 research_registry 中某条已完成 OOS/Walk-Forward 的研究记录，
      结合 Python 计算的客观统计量（相关性、方差等数学计算不交给 LLM 做，
      避免幻觉数字），调用硅基流动 DeepSeek 生成结构化 Post-Mortem 报告。

用法：
    python post_mortem_agent.py Research-001
    python post_mortem_agent.py Research-001 --dry-run   # 只打印，不写库
"""
import sys
import json

import numpy as np

from common import (
    call_llm, parse_json_response,
    fetch_research_row, fetch_wf_windows, ensure_wf_windows_table,
    get_conn,
)
from prompts import POST_MORTEM_SYSTEM_PROMPT

# ⚠️ 必须和 REGISTRY_CONVENTIONS.md 里的枚举保持完全一致。
# 改这里的同时必须同步检查 REGISTRY_CONVENTIONS.md，反之亦然。
# 可用 python check_enum_consistency.py 验证。
KNOWN_FAILURE_CATEGORIES = [
    "RegimeShift", "Overfit", "LowSample", "Cost", "Lookahead",
    "Execution", "Capacity", "DataIssue", "NoEdgeFound", "Other",
]


def compute_wf_stats(windows: list) -> dict:
    """在代码里算数字，LLM 只负责解释，不负责计算。"""
    if not windows:
        return {"warning": "无 wf_windows 数据，无法计算方差/相关性"}

    trades = np.array([w["trades"] for w in windows], dtype=float)
    sharpes = np.array([w["sharpe"] for w in windows], dtype=float)

    stats = {
        "n_windows": len(windows),
        "mean_sharpe": round(float(np.mean(sharpes)), 3),
        "std_sharpe": round(float(np.std(sharpes, ddof=1)), 3) if len(sharpes) > 1 else None,
        "positive_ratio": round(float(np.mean(sharpes > 0)), 3),
        "min_sharpe": round(float(np.min(sharpes)), 3),
        "max_sharpe": round(float(np.max(sharpes)), 3),
    }

    if len(windows) >= 3 and np.std(trades) > 0 and np.std(sharpes) > 0:
        corr = float(np.corrcoef(trades, sharpes)[0, 1])
        stats["trade_count_sharpe_correlation"] = round(corr, 3)
        # corr 明显为负 -> 交易数越多反而越亏，是典型的"小样本噪声撑起表现"信号
        stats["small_sample_noise_flag"] = bool(corr < -0.5)
    else:
        stats["trade_count_sharpe_correlation"] = None
        stats["small_sample_noise_flag"] = False

    return stats


def build_agent_input(research_id: str) -> dict:
    row = fetch_research_row(research_id)
    windows = fetch_wf_windows(research_id)
    wf_stats = compute_wf_stats(windows)

    return {
        "research_id": row.get("research_id"),
        "hypothesis": row.get("hypothesis"),
        "economic_hypothesis": row.get("economic_hypothesis"),
        "strategy_type": row.get("strategy_type"),
        "known_failure_categories": KNOWN_FAILURE_CATEGORIES,
        "data_quality_grade": row.get("data_quality_grade"),
        "backtest_reliability_grade": row.get("backtest_reliability_grade"),
        "lookahead_audit_status": row.get("lookahead_audit_status"),
        "is_sharpe": float(row["is_sharpe"]) if row.get("is_sharpe") is not None else None,
        "is_grade": row.get("is_grade"),
        "oos_sharpe": float(row["oos_sharpe"]) if row.get("oos_sharpe") is not None else None,
        "oos_grade": row.get("oos_grade"),
        "walk_forward_grade": row.get("walk_forward_grade"),
        "cost_test_passed": row.get("cost_test_passed"),
        "cross_asset_tested": row.get("cross_asset_tested"),
        "wf_window_detail": windows,
        "wf_computed_stats": wf_stats,
    }


def run_post_mortem(research_id: str, dry_run: bool = False) -> dict:
    ensure_wf_windows_table()
    agent_input = build_agent_input(research_id)

    response_text = call_llm(
        system_prompt=POST_MORTEM_SYSTEM_PROMPT,
        user_content=json.dumps(agent_input, ensure_ascii=False, indent=2, default=str),
    )
    report = parse_json_response(response_text)

    print(f"\n=== Post-Mortem: {research_id} ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not dry_run:
        _write_report(research_id, report, agent_input["wf_computed_stats"])

    return report


def _write_report(research_id: str, report: dict, wf_stats: dict):
    """把 Agent 结论写入独立的 post_mortem_reports 表（留痕，每次运行都是新记录，
    不覆盖历史），刻意不直接 UPDATE research_registry 里的 failure_category /
    regime_notes 等字段——那些字段目前的填表规范是人工确认后才改，
    Agent 只负责生成建议，不自动覆盖。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS post_mortem_reports (
                    id SERIAL PRIMARY KEY,
                    research_id VARCHAR(20) NOT NULL,
                    generated_at TIMESTAMP DEFAULT NOW(),
                    failure_category VARCHAR(50),
                    root_cause_summary TEXT,
                    confidence VARCHAR(10),
                    recommended_next_direction TEXT,
                    red_flags JSONB,
                    wf_computed_stats JSONB,
                    raw_response JSONB
                );
            """)
            cur.execute("""
                INSERT INTO post_mortem_reports
                    (research_id, failure_category, root_cause_summary, confidence,
                     recommended_next_direction, red_flags, wf_computed_stats, raw_response)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                research_id,
                report.get("failure_category"),
                report.get("root_cause_summary"),
                report.get("confidence"),
                report.get("recommended_next_direction"),
                json.dumps(report.get("red_flags", []), ensure_ascii=False),
                json.dumps(wf_stats, ensure_ascii=False, default=str),
                json.dumps(report, ensure_ascii=False),
            ))
        conn.commit()
    print(f"\n已写入 post_mortem_reports 表 (research_id={research_id})")
    print("注意：failure_category / regime_notes 等字段是否同步回 research_registry "
          "需人工确认后再 UPDATE，脚本不会自动覆盖已有的人工审核字段。")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python post_mortem_agent.py <research_id> [--dry-run]")
        sys.exit(1)
    rid = sys.argv[1]
    dry = "--dry-run" in sys.argv
    run_post_mortem(rid, dry_run=dry)
