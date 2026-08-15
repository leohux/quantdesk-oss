# Research Registry — 填表规范 (REGISTRY_CONVENTIONS.md)

**Single Source of Truth 原则**：所有 Dashboard、Agent、报告、策略筛选必须读取 `research_registry` 表，不重新解析 Markdown。

---

## ① Primary Endpoint（锁死，不可协商）

所有验证必须以 **T+5** 为唯一 Primary Endpoint。Secondary 仅做 robustness 参考，不允许反选。

| 类型 | 窗口 | 用途 |
|------|------|------|
| **Primary** | **T+5** | **唯一判定窗口** |
| Secondary | T+1 | 超短线 robustness |
| Secondary | T+10 | 持有延展 robustness |
| Secondary | T+20 | 中期衰减 robustness |

**禁止行为**：
- ❌ "T+10 好一点，所以算成功"（Window Selection Bias）
- ❌ 用 Secondary 窗口的结果覆盖 Primary 判定
- ❌ 允许不同 research 使用不同 Primary Endpoint

**数据库字段**：
- `primary_endpoint` VARCHAR(5) DEFAULT 'T+5'
- `secondary_endpoints` JSONB DEFAULT '["T+1","T+10","T+20"]'

---

## ② Research Rating（A–F）+ Next Action

### 评级标准

| Rating | 条件 | next_action | 含义 |
|--------|------|-------------|------|
| **A** | IC>0.01, RankIC>0.015, p<0.05, 成本后Sharpe>1.0, OOS/IS>0.6, D级WF<50% | `paper_trading` | ✅ 进入模拟盘 |
| **B** | IC>0.005, Sharpe>0.5, OOS/IS>0.4, 有边际但未达A | `reoptimize` | ✅ 重新优化参数/特征 |
| **C** | 工程问题阻断（覆盖率不足/VIF超标/数据缺失） | `fix_engineering` | ⚠️ 修工程问题后重跑 |
| **D** | IS有信号但OOS衰减严重，仅保留研究价值 | `archive_research_value` | ❌ 停止开发，归档 |
| **F** | IS无显著边际，或存在Lookahead/致命缺陷 | `archive` | ❌ 归档 |

### 数据库字段
- `research_rating` VARCHAR(2) CHECK IN ('A','B','C','D','F')
- `rating_next_action` VARCHAR(50) CHECK IN ('paper_trading','reoptimize','fix_engineering','archive_research_value','archive')

### 用途
```sql
-- 统计命中率
SELECT research_rating, COUNT(*) FROM research_registry
WHERE research_rating IS NOT NULL GROUP BY research_rating;

-- 找出可优化的研究
SELECT research_id, hypothesis FROM research_registry
WHERE rating_next_action = 'reoptimize';
```

---

## ③ Coverage Gate — coverage_reports 表

Coverage Gate 不仅输出 `coverage_warning=true`，还必须写入 `coverage_reports` 表：

| 字段 | 类型 | 说明 |
|------|------|------|
| research_id | VARCHAR(20) | 关联 research_registry |
| symbol | VARCHAR(10) | 股票代码 |
| quarter | VARCHAR(10) | 如 '2023Q1' |
| coverage_pct | NUMERIC(5,2) | 覆盖率百分比 |
| total_days | INTEGER | 该季度总交易日 |
| missing_days | INTEGER | 缺失天数 |
| warning | BOOLEAN | coverage_pct < 90% 时为 true |

**示例查询**：
```sql
-- 查看某研究的覆盖率详情
SELECT symbol, quarter, coverage_pct, warning
FROM coverage_reports
WHERE research_id = 'Research-002'
ORDER BY symbol, quarter;

-- 找出所有覆盖率警告
SELECT research_id, symbol, quarter, coverage_pct
FROM coverage_reports WHERE warning = true;
```

---

## ④ Post-Mortem 模板（强制结构化）

每个完成的研究必须生成结构化 Post-Mortem，字段如下：

| 字段 | 说明 | 必填 |
|------|------|------|
| hypothesis | 原始假设 | ✅ |
| what_worked | 什么有效 | ✅ |
| what_failed | 什么失败 | ✅ |
| biggest_risk | 最大风险 | ✅ |
| unexpected_finding | 意外发现 | 推荐 |
| next_action | 下一步行动 | ✅ |
| failure_category | 失败分类（Rejected时） | 按需 |
| root_cause_summary | 根因总结 | ✅ |

**示例**：
```
Research-002 Post-Mortem
━━━━━━━━━━━━━━━━━━━━━━
Hypothesis: Analyst revision momentum predicts T+5 returns
What worked: VIF 29→1.35, three features fully orthogonal
What failed: Coverage <90% for small-cap names in 2020-2021
Biggest risk: Regime dependency — signal weakens in high-vol regimes
Unexpected finding: grade_sentiment_score has non-linear payoff
Next action: [reoptimize / paper_trading / archive]
```

---

## ⑤ Meta Statistics（组织级知识积累）

### 累计统计视图

```sql
-- 总览
SELECT * FROM research_meta_stats;

-- 按策略类型分解（哪类 Alpha 最容易成功）
SELECT * FROM research_meta_stats_by_type;
```

**长期价值示例**：
```
strategy_type   | total | A | B | C | D | F | a_rate_pct
----------------+-------+---+---+---+---+---+-----------
event-driven    |    15 | 3 | 4 | 3 | 3 | 2 |      20.0
technical       |    10 | 0 | 2 | 3 | 3 | 2 |       0.0
momentum        |     8 | 1 | 2 | 1 | 2 | 2 |      12.5
stat-arb        |     7 | 2 | 1 | 2 | 1 | 1 |      28.6
```

→ 你会知道：event-driven 和 stat-arb 的命中率最高，technical 最低。

---

## ⑥ Pipeline 完整流程（闭环）

```
Proposal (假设 + 经济机制)
    ↓
Data Validation (数据可用性检查)
    ↓
Coverage Gate (≥90% per symbol-quarter → coverage_reports)
    ↓
Missing Coverage Gate (≤10% per quarter)
    ↓
Feature Engineering (特征构建 + VIF 检查)
    ↓
Orthogonalization (VIF < 5)
    ↓
Concentration Gate (单股<25%, 单季度<30%)
    ↓
IS Backtest (样本内验证)
    ↓
Cost Gate (10bp 成本后)
    ↓
Walk Forward (滚动窗口验证)
    ↓
OOS Validation (样本外验证)
    ↓
Research Rating (A–F) + Next Action
    ↓
Post-Mortem (结构化报告)
    ↓
Research Registry (SSOT 写入)
    ↓
┌─ A → Paper Trading → Live (满足后续要求时)
├─ B → 返回 Proposal 迭代
├─ C → 修工程问题后重跑
├─ D → 归档，保留研究价值
└─ F → 归档
```

---

## 字段枚举值（固定清单，不得自造）

### status
| 值 | 含义 |
|---|------|
| `Running` | 研究进行中 |
| `Rejected` | 假设被否证，不进入实盘 |
| `Accepted` | 通过验证，待部署 |
| `Live` | 已上线实盘 |

### stage
| 值 | 含义 | 进入条件 |
|---|------|---------|
| `Idea` | 假设已提出 | 填写 hypothesis + economic_hypothesis |
| `FeatureEngineering` | 特征工程中 | 特征脚本开始运行 |
| `IS` | 样本内参数搜索 | IS 结果已生成 |
| `OOS` | 样本外验证 | OOS 运行完成 |
| `WalkForward` | 滚动窗口验证 | WF 运行完成 |
| `CrossAsset` | 跨标的验证 | 至少 3 个不同标的已测试 |
| `PaperTrading` | 模拟盘 | 策略代码部署到 Paper Trading |
| `Completed` | 研究完成（Accepted） | 决策已归档 |
| `Archived` | 已归档（Rejected 或 Completed） | 最终状态 |

### failure_category（仅 Rejected 时必填）
| 值 | 含义 | 典型表现 |
|---|------|---------|
| `Lookahead` | 回测存在未来函数 | IS Sharpe >> OOS Sharpe |
| `Overfit` | 过拟合 IS 噪声 | IS 高但 OOS 接近 0 |
| `RegimeShift` | 特定市场状态下失效 | WF 连续负 Sharpe |
| `Capacity` | 容量不足 | 信号稀少或冲击成本过大 |
| `Cost` | 扣除交易成本后失效 | Gross 正但 net 负 |
| `Execution` | 执行层面不可行 | 时间窗口太短 |
| `LowSample` | 样本量不足 | 有效交易 < 200 |
| `DataIssue` | 数据质量问题 | 缺失率高、幸存者偏差 |
| `NoEdgeFound` | 假设本身不成立 | IS 无显著边际 |
| `Other` | 其他 | 需在 root_cause 中说明 |

### strategy_type
| 值 | 含义 |
|---|------|
| `technical` | 纯技术面 |
| `event-driven` | 事件驱动 |
| `stat-arb` | 统计套利 |
| `regime-aware` | 市场状态感知 |
| `momentum` | 动量/趋势跟踪 |
| `mean-reversion` | 均值回归 |
| `volatility` | 波动率策略 |

---

## Grade 评分标准

**IS Grade:**
| Grade | Sharpe | 说明 |
|-------|--------|------|
| A | ≥ 2.0 | 强信号 |
| B+ | 1.5 ~ 2.0 | 有意义的边际 |
| B | 1.0 ~ 1.5 | 边际可行 |
| C | 0.5 ~ 1.0 | 弱信号 |
| D | < 0.5 | 不显著 |

**OOS Grade:**
| Grade | Sharpe | 说明 |
|-------|--------|------|
| A | ≥ 1.5 | 优秀迁移 |
| B | 1.0 ~ 1.5 | 良好迁移 |
| C | 0.5 ~ 1.0 | 边际迁移 |
| D | 0 ~ 0.5 | 衰减明显 |
| F | < 0 | 完全失效 |

**Walk-Forward Grade:**
| Grade | +Ratio | MeanSharpe | 说明 |
|-------|--------|------------|------|
| A | ≥ 70% | ≥ 0.5 | 稳定 |
| B | ≥ 60% | ≥ 0.3 | 有边际 |
| C | ≥ 50% | — | 混合结果 |
| D | < 50% 或极端方差 | — | 不稳定 |

**注意 WF 陷阱**：Sharpe Std > 2.0 时即使 Mean > 0 也应降一档。

---

## lookahead_audit_status 流程

1. **默认值**：`Unaudited`
2. **人工审查后**：
   - `Audited-Clean` — 确认无 look-ahead bias
   - `Audited-Issues-Fixed` — 发现并修复了 look-ahead bias
3. **审查要点**：
   - 特征计算是否使用了当前时刻之后的数据？
   - `expanding().max()` vs `max()` 区别
   - 信号触发到执行是否有足够延迟
   - `merge_asof` 的 `direction` 参数是否正确

---

## 填表流程

### 新 Research 启动时
```bash
mkdir -p research/archive/<date>/research_NNN_<name>/
# 写 hypothesis.md, protocol.md
python archive_research.py /path/to/research_dir/ \
  --economic-hypothesis "One-sentence economic mechanism."
```

### 研究完成时
```bash
python archive_research.py /path/to/research_dir/
# 手动补充: research_rating, rating_next_action, failure_category, regime_notes
```

### 查询
```bash
python query_registry.py --failure-category RegimeShift
python query_registry.py --strategy-type technical

# Meta Statistics
psql -c "SELECT * FROM research_meta_stats;"
psql -c "SELECT * FROM research_meta_stats_by_type;"

# Coverage Report
psql -c "SELECT * FROM coverage_reports WHERE research_id='Research-002';"
```

---

## 不应留空的字段（按研究阶段）

| 阶段 | 必填字段 |
|------|---------|
| Idea | research_id, hypothesis, economic_hypothesis, strategy_type, stage |
| IS 完成 | is_sharpe, is_grade, is_trades |
| OOS 完成 | oos_sharpe, oos_grade, oos_trades, cost_test_passed |
| WF 完成 | walk_forward_grade, walk_forward_windows, walk_forward_positive_ratio |
| Rating | research_rating, rating_next_action |
| Post-Mortem | hypothesis, what_worked, what_failed, biggest_risk, next_action |
| Rejected | failure_category, rejection_reason, final_decision |
| Accepted | cross_asset_tested, correlation_to_existing, capacity_estimate_usd |

---

**最后：别让 Registry 成为日志。它是唯一事实来源。**
