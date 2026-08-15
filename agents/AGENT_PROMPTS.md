# QuantDesk Research Pipeline — Agent 提示词与落地说明

配合 `prompts.py`（提示词的唯一代码来源）+ `common.py` / `post_mortem_agent.py` /
`research_proposal_agent.py` 使用。本文档是给人看的参考，实际运行时脚本从
`prompts.py` 里 import，不要在这里改了却忘记同步代码。

## 落地顺序（不要打乱）

| 顺序 | Agent | 状态 | 触发条件 |
|---|---|---|---|
| 1 | Post-Mortem Agent | ✅ 已有代码 | Registry 里任意一条记录跑完 OOS/WF 后即可用 |
| 2 | Research Proposal Agent | ✅ 已有代码，但角色降级 | 现在就能用，只负责"生成候选+查重"，不负责"该不该做" |
| 3 | Feature Discovery Agent | 🕐 仅提示词模板 | Research-002 假设确定后，需要时再封装成脚本 |
| 4 | Strategy Generator | 🕐 仅提示词模板，非 JSON 输出 | 直接连同 Proposal 一起发给 Hermes/Claude Code 执行 |
| 5 | Pattern Recognition Agent | 🚫 先不要启用 | Registry 归档记录 ≥ 5 条之后再启用，样本不够会产出噪声 |

原因：Post-Mortem 不需要历史样本积累就能产出价值，且它产出的分析本身就是在给
Registry 攒高质量数据；Research Proposal 现在只做"结构化生成+查重"，"该选哪个
方向"这个判断权重仍然在人，因为现在数据量不足以让 AI 自己判断。

## 关键设计原则

1. **数学计算不交给 LLM 做。** Walk-Forward 的方差、相关性等统计量都在
   `post_mortem_agent.py` 里用 numpy 算好，LLM 只负责在数字基础上做归因和写
   总结，不自己编数字。这是为了避免"看起来很有道理但其实是幻觉"的分析报告。
2. **人工审核字段永远不会被 Agent 自动覆盖。** `lookahead_audit_status` 只能人工
   改；Post-Mortem Agent 的结论写入独立的 `post_mortem_reports` 表留痕，不直接
   UPDATE `research_registry`，避免自动化流程悄悄污染已确认的研究结论。
3. **failure_category 枚举、estimated_theoretical_edge_bps 等字段如果模型给不出
   可靠答案，输出 null 或 "Other" 并解释原因，而不是编造一个看似合理的值。**

## Post-Mortem Agent

**输入**：`research_registry` 单条记录的关键字段 + `wf_windows` 表的逐窗口
trades/sharpe + Python 预先算好的方差、trade_count-sharpe 相关系数等统计量。

**输出**：failure_category、root_cause_summary、red_flags、
recommended_next_direction 等结构化 JSON，写入 `post_mortem_reports` 表。

系统提示词见 `prompts.py` 中的 `POST_MORTEM_SYSTEM_PROMPT`（内容摘要）：
- 不自己算数字，只解释已给出的数字
- 不用"市场环境变化"这种模糊表述，要找具体机制
- failure_category 必须从传入的枚举里选，不合适就用 Other 并说明
- 不能碰 lookahead_audit_status
- 只输出 JSON，不加任何额外文字

## Research Proposal Agent

**输入**：研究方向描述 + 整个 Registry 的假设/状态摘要（用于查重）。

**输出**：5-8 个候选假设，每个必须包含经济逻辑、可证伪预测、所需特征、
理论 edge 粗估（bp）、风险提示。生成后代码里再用字面相似度（`difflib`）做一次
粗筛，标记出需要人工复核是否与历史 Rejected 研究本质重复的候选。

系统提示词见 `prompts.py` 中的 `RESEARCH_PROPOSAL_SYSTEM_PROMPT`（内容摘要）：
- 每个候选必须解释"谁的行为造成了这个定价错误、为什么没被立刻套利消除"
- 必须给出具体可证伪预测
- 和已 Rejected 研究机制类似的候选依然可以提，但要注明关键差异
- edge 估计给不出就填 null，不编数字

## Feature Discovery Agent（模板，暂未接代码）

给定一个已确定的假设，产出可直接翻译成代码的特征定义清单 + 至少一个
regime 相关的过滤变量，并标注每个特征的 look-ahead 风险点。提示词见
`prompts.py` 中的 `FEATURE_DISCOVERY_SYSTEM_PROMPT`。

## Strategy Generator（模板，非 JSON 输出，直接喂给 Hermes/Claude Code）

给定已批准的 Proposal + Feature 清单，生成新研究目录骨架的任务描述
（`config.yaml` / `feature.py` / `backtest.py` / `oos.py` / `wf.py`），
结构必须和 research-001 保持一致，且 `wf.py` 必须把逐窗口结果写入
`wf_windows` 表，这是 Post-Mortem Agent 能计算方差/相关性的前提。模板见
`prompts.py` 中的 `STRATEGY_GENERATOR_PROMPT_TEMPLATE`。

## Pattern Recognition Agent（模板，Registry ≥ 5 条记录后再启用）

跨多条已归档记录寻找失败/成功模式的共性。规则里明确要求：只基于输入数据
本身归纳，不能引入训练数据里的"常识"充当发现；每个"模式"至少要有 2 条以上
记录支持，否则只能叫"个案观察"。提示词见 `prompts.py` 中的
`PATTERN_RECOGNITION_SYSTEM_PROMPT`。

## 环境变量（运行前设置）

```
SILICONFLOW_API_KEY=...                 # 硅基流动 API Key
QUANTDESK_AGENT_BASE_URL=https://api.siliconflow.cn/v1
QUANTDESK_AGENT_MODEL=deepseek-ai/DeepSeek-V4-Pro     # 可换成 DeepSeek-V4-Flash 等
QUANTDESK_DB_HOST=localhost
QUANTDESK_DB_PORT=5432
QUANTDESK_DB_NAME=quantdesk
QUANTDESK_DB_USER=...
QUANTDESK_DB_PASSWORD=...
```

## 首次运行建议顺序

```bash
pip install -r requirements.txt

# 1. 先用 Research-001 跑一次 Post-Mortem，验证字段对得上、结论和你手动分析的
#    "trade_count 与 sharpe 负相关" 是否一致
python post_mortem_agent.py Research-001 --dry-run

# 2. 确认无误后正式写库
python post_mortem_agent.py Research-001

# 3. Research-002 方向确定后，先用 Proposal Agent 生成候选辅助确定具体假设
python research_proposal_agent.py "Earnings Drift" --n 6
```
