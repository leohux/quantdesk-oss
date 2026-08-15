# -*- coding: utf-8 -*-
"""
QuantDesk Research Agents - 公共工具模块
提供：硅基流动（SiliconFlow）OpenAI 兼容 LLM 调用、Postgres 连接、JSON 安全解析

环境变量：
    SILICONFLOW_API_KEY    必需（也可用 QUANTDESK_AGENT_API_KEY）
    QUANTDESK_AGENT_BASE_URL  默认 https://api.siliconflow.cn/v1
    QUANTDESK_AGENT_MODEL     默认 deepseek-ai/DeepSeek-V4-Pro
    QUANTDESK_DB_HOST      默认 localhost
    QUANTDESK_DB_PORT      默认 5432
    QUANTDESK_DB_NAME      默认 quantdesk
    QUANTDESK_DB_USER      必需
    QUANTDESK_DB_PASSWORD  必需
"""
import json
import os
import re
import ssl
import urllib.error
import urllib.request

import psycopg2
import psycopg2.extras

# ---- SiliconFlow / DeepSeek (OpenAI-compatible) ----
DEFAULT_BASE_URL = os.environ.get(
    "QUANTDESK_AGENT_BASE_URL", "https://api.siliconflow.cn/v1"
).rstrip("/")
DEFAULT_MODEL = os.environ.get(
    "QUANTDESK_AGENT_MODEL", "deepseek-ai/DeepSeek-V4-Pro"
)


def _agent_api_key() -> str:
    key = (
        os.environ.get("SILICONFLOW_API_KEY", "").strip()
        or os.environ.get("QUANTDESK_AGENT_API_KEY", "").strip()
    )
    if not key:
        raise RuntimeError(
            "SILICONFLOW_API_KEY missing — research agents require SiliconFlow. "
            "Set SILICONFLOW_API_KEY (or QUANTDESK_AGENT_API_KEY) in .env"
        )
    return key


def call_llm(
    system_prompt: str,
    user_content: str,
    model: str = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
) -> str:
    """调用硅基流动 Chat Completions，返回原始文本响应。

    temperature 默认给低值（0.3），因为这里是分析/归纳类任务，
    不需要高多样性；Feature/Proposal 类如果想要更发散的候选，
    调用时可以传高一点的 temperature。
    """
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Keep structured agent JSON calls snappy; reasoning models otherwise
        # spend most tokens in reasoning_content under SiliconFlow defaults.
        "enable_thinking": False,
    }
    req = urllib.request.Request(
        DEFAULT_BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + _agent_api_key())
    try:
        with urllib.request.urlopen(
            req, timeout=180, context=ssl.create_default_context()
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"SiliconFlow HTTP {e.code}: {detail}") from e

    msg = body["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    # thinking-mode 模型可能把正文放在 reasoning_content
    reasoning = (msg.get("reasoning_content") or "").strip()
    return content if content else reasoning


# 兼容旧调用名
call_claude = call_llm


def parse_json_response(raw: str) -> dict:
    """从模型返回文本中稳健提取 JSON（容忍 ```json 包裹、前后多余文字）。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"未能在响应中找到 JSON 结构:\n{raw[:500]}")
    return json.loads(match.group(0))


# ---- Database ----
def get_conn():
    return psycopg2.connect(
        host=os.environ.get("QUANTDESK_DB_HOST", "localhost"),
        port=os.environ.get("QUANTDESK_DB_PORT", 5432),
        dbname=os.environ.get("QUANTDESK_DB_NAME", "quantdesk"),
        user=os.environ.get("QUANTDESK_DB_USER"),
        password=os.environ.get("QUANTDESK_DB_PASSWORD"),
    )


def fetch_research_row(research_id: str) -> dict:
    """按 research_id 取 research_registry 单行。
    注意：实际列名以 Hermes 建表结果为准，若有差异（例如字段改名），
    这里的 SELECT * 不受影响，但下游 agent 里按 key 取值的地方需要同步调整。
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM research_registry WHERE research_id = %s",
                (research_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"未找到 research_id={research_id}")
            return dict(row)


def fetch_all_hypotheses_summary() -> list:
    """给 Research Proposal Agent 查重用的精简历史清单。"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT research_id, hypothesis, economic_hypothesis, strategy_type,
                       status, failure_category, final_decision
                FROM research_registry
                ORDER BY research_id
            """)
            return [dict(r) for r in cur.fetchall()]


def ensure_wf_windows_table():
    """若 Hermes 尚未建独立的逐窗口 Walk-Forward 明细表，这里兜底创建（幂等）。
    如果 Hermes 已经用别的表名/结构存了这份数据，把这个函数和下面的
    fetch_wf_windows 改成指向那张表即可，不需要改上层 agent 逻辑。
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS wf_windows (
        id SERIAL PRIMARY KEY,
        research_id VARCHAR(20) NOT NULL,
        window_label VARCHAR(20),
        period VARCHAR(20),
        trades INTEGER,
        sharpe NUMERIC(8,3),
        UNIQUE(research_id, window_label)
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def fetch_wf_windows(research_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT window_label, period, trades, sharpe FROM wf_windows "
                "WHERE research_id = %s ORDER BY period",
                (research_id,),
            )
            return [dict(r) for r in cur.fetchall()]
