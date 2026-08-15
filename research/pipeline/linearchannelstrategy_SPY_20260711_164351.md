# ❌ Hard Gates Validation Report

**Strategy:** LinearChannelStrategy
**Symbol:** SPY
**Timestamp:** 2026-07-11T16:43:50.233269
**Overall:** FAIL (8/9 passed)

## Gate Summary

| # | Gate | Status | Message |
|---|------|--------|---------|
| 1 | Sector Concentration | ✅ PASS | 单标的策略 (SPY)，行业检查不适用 |
| 2 | Universe Pollution | ✅ PASS | Universe 干净，1 个标的全部通过 |
| 3 | IS/OOS Gap | ✅ PASS | IS/OOS Gap = 0.288，无过拟合迹象 |
| 4 | Cost Sensitivity | ✅ PASS | 成本稳健，10bps 下 Sharpe = 0.500 |
| 5 | Single Asset Contribution | ✅ PASS | 单标的策略 (SPY) |
| 6 | Event Contribution | ⚠️ WARNING | 单笔最大交易贡献 100.0%，可能是事件驱动（无财报数据确认） |
| 7 | Stability | ✅ PASS | 稳定性良好，96% 窗口为正，CV=0.50 |
| 8 | Benchmark Comparison | ❌ FAIL | 未打败任何基准 (SPY, QQQ, XLK) |
| 9 | Risk Diagnostics | ✅ PASS | 风险可控: Vol=17.9%, MaxDD=-33.7%, VaR=-1.62% |

## Backtest Statistics

- **sharpe:** 0.5179
- **cagr:** 0.0795
- **maxdd:** -0.3372
- **sortino:** 0.6460
- **ann_vol:** 0.1786
- **win_rate:** 52.04%
- **turnover:** 16.5000
- **n_years:** 9.9700
- **total_return:** 114.39%
- **cost_bps:** 0.0000

## Gate Details

### Gate 1: Sector Concentration — ✅ PASS

> 单标的策略 (SPY)，行业检查不适用

- **symbol:** SPY
- **note:** 单标的策略，行业集中度由标的本身决定

### Gate 2: Universe Pollution — ✅ PASS

> Universe 干净，1 个标的全部通过

- **total_assets:** 1
- **all_clean:** True

### Gate 3: IS/OOS Gap — ✅ PASS

> IS/OOS Gap = 0.288，无过拟合迹象

- **is_sharpe:** 0.5757
- **oos_sharpe:** 0.2880
- **gap:** 0.2877
- **is_period:** 2016-07-13 00:00:00 ~ 2023-07-07 00:00:00
- **oos_period:** 2023-07-10 00:00:00 ~ 2026-07-10 00:00:00
- **is_bars:** 1758
- **oos_bars:** 754

### Gate 4: Cost Sensitivity — ✅ PASS

> 成本稳健，10bps 下 Sharpe = 0.500

- **sharpe_0bps:** 0.5179
- **sharpe_5bps:** 0.5090
- **sharpe_10bps:** 0.5001
- **sharpe_20bps:** 0.4822
- **decay_0to10:** 0.0178
- **decay_0to20:** 0.0357

### Gate 5: Single Asset Contribution — ✅ PASS

> 单标的策略 (SPY)

- **symbol:** SPY
- **note:** 单标的策略，不适用

### Gate 6: Event Contribution — ⚠️ WARNING

> 单笔最大交易贡献 100.0%，可能是事件驱动（无财报数据确认）

- **total_return:** 1.56
- **max_single_trade_return:** 1.56
- **max_single_trade_pct:** 100.00
- **n_trades:** 1
- **note:** 无财报数据，使用单笔最大交易启发式检查

### Gate 7: Stability — ✅ PASS

> 稳定性良好，96% 窗口为正，CV=0.50

- **window_years:** 3.00
- **n_windows:** 24
- **mean_sharpe:** 0.6803
- **std_sharpe:** 0.3373
- **min_sharpe:** -0.2595
- **max_sharpe:** 1.26
- **pct_positive:** 95.80
- **median_sharpe:** 0.7283

### Gate 8: Benchmark Comparison — ❌ FAIL

> 未打败任何基准 (SPY, QQQ, XLK)

- **comparisons:**
  - **SPY:**
    - **strat_sharpe:** 0.5179
    - **bench_sharpe:** 0.8829
    - **excess_sharpe:** -0.3650
    - **strat_cagr:** 0.0795
    - **bench_cagr:** 0.1527
    - **excess_cagr:** -0.0732
    - **strat_maxdd:** -0.3372
    - **bench_maxdd:** -0.3372
  - **QQQ:**
    - **strat_sharpe:** 0.5179
    - **bench_sharpe:** 0.9823
    - **excess_sharpe:** -0.4644
    - **strat_cagr:** 0.0795
    - **bench_cagr:** 0.2154
    - **excess_cagr:** -0.1359
    - **strat_maxdd:** -0.3372
    - **bench_maxdd:** -0.3512
  - **XLK:**
    - **strat_sharpe:** 0.5179
    - **bench_sharpe:** 1.02
    - **excess_sharpe:** -0.5061
    - **strat_cagr:** 0.0795
    - **bench_cagr:** 0.2496
    - **excess_cagr:** -0.1701
    - **strat_maxdd:** -0.3372
    - **bench_maxdd:** -0.3356
- **benchmarks_beaten:** 0

### Gate 9: Risk Diagnostics — ✅ PASS

> 风险可控: Vol=17.9%, MaxDD=-33.7%, VaR=-1.62%

- **ann_volatility:** 0.1786
- **max_drawdown:** -0.3372
- **var_5pct:** -0.0162
- **tail_risk_avg:** -0.0267
- **exposure:** 0.9638
- **beta:** 0.7556
- **correlation:** 0.7585

## Decision

❌ **REJECTED** — 策略未通过以下 Gate:
  - Benchmark Comparison

在修复上述问题之前，不得进入 Paper Trading。
