# ❌ Hard Gates Validation Report

**Strategy:** CompositeTrendFilterStrategy
**Symbol:** SPY
**Timestamp:** 2026-07-11T16:44:38.913577
**Overall:** FAIL (8/9 passed)

## Gate Summary

| # | Gate | Status | Message |
|---|------|--------|---------|
| 1 | Sector Concentration | ✅ PASS | 单标的策略 (SPY)，行业检查不适用 |
| 2 | Universe Pollution | ✅ PASS | Universe 干净，1 个标的全部通过 |
| 3 | IS/OOS Gap | ✅ PASS | IS/OOS Gap = -0.421，无过拟合迹象 |
| 4 | Cost Sensitivity | ✅ PASS | 成本稳健，10bps 下 Sharpe = 0.252 |
| 5 | Single Asset Contribution | ✅ PASS | 单标的策略 (SPY) |
| 6 | Event Contribution | ✅ PASS | 仅 1 笔交易，样本不足，跳过事件分析 |
| 7 | Stability | ✅ PASS | 稳定性良好，96% 窗口为正，CV=0.47 |
| 8 | Benchmark Comparison | ❌ FAIL | 未打败任何基准 (SPY, QQQ, XLK) |
| 9 | Risk Diagnostics | ✅ PASS | 风险可控: Vol=17.9%, MaxDD=-26.4%, VaR=-1.67% |

## Backtest Statistics

- **sharpe:** 0.3575
- **cagr:** 0.0492
- **maxdd:** -0.2641
- **sortino:** 0.4507
- **ann_vol:** 0.1795
- **win_rate:** 52.83%
- **turnover:** 92.5000
- **n_years:** 9.9700
- **total_return:** 61.41%
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

> IS/OOS Gap = -0.421，无过拟合迹象

- **is_sharpe:** 0.2966
- **oos_sharpe:** 0.7173
- **gap:** -0.4207
- **is_period:** 2016-07-13 00:00:00 ~ 2023-07-07 00:00:00
- **oos_period:** 2023-07-10 00:00:00 ~ 2026-07-10 00:00:00
- **is_bars:** 1758
- **oos_bars:** 754

### Gate 4: Cost Sensitivity — ✅ PASS

> 成本稳健，10bps 下 Sharpe = 0.252

- **sharpe_0bps:** 0.3575
- **sharpe_5bps:** 0.3045
- **sharpe_10bps:** 0.2519
- **sharpe_20bps:** 0.1480
- **decay_0to10:** 0.1056
- **decay_0to20:** 0.2095

### Gate 5: Single Asset Contribution — ✅ PASS

> 单标的策略 (SPY)

- **symbol:** SPY
- **note:** 单标的策略，不适用

### Gate 6: Event Contribution — ✅ PASS

> 仅 1 笔交易，样本不足，跳过事件分析

- **n_trades:** 1
- **total_return:** 1.57
- **note:** 仅 1 笔交易，样本不足，无法验证事件贡献

### Gate 7: Stability — ✅ PASS

> 稳定性良好，96% 窗口为正，CV=0.47

- **window_years:** 3.00
- **n_windows:** 24
- **mean_sharpe:** 0.3917
- **std_sharpe:** 0.1836
- **min_sharpe:** -0.0394
- **max_sharpe:** 0.6988
- **pct_positive:** 95.80
- **median_sharpe:** 0.4204

### Gate 8: Benchmark Comparison — ❌ FAIL

> 未打败任何基准 (SPY, QQQ, XLK)

- **comparisons:**
  - **SPY:**
    - **strat_sharpe:** 0.3575
    - **bench_sharpe:** 0.8829
    - **excess_sharpe:** -0.5254
    - **strat_cagr:** 0.0492
    - **bench_cagr:** 0.1527
    - **excess_cagr:** -0.1035
    - **strat_maxdd:** -0.2641
    - **bench_maxdd:** -0.3372
  - **QQQ:**
    - **strat_sharpe:** 0.3575
    - **bench_sharpe:** 0.9823
    - **excess_sharpe:** -0.6248
    - **strat_cagr:** 0.0492
    - **bench_cagr:** 0.2154
    - **excess_cagr:** -0.1662
    - **strat_maxdd:** -0.2641
    - **bench_maxdd:** -0.3512
  - **XLK:**
    - **strat_sharpe:** 0.3575
    - **bench_sharpe:** 1.02
    - **excess_sharpe:** -0.6665
    - **strat_cagr:** 0.0492
    - **bench_cagr:** 0.2496
    - **excess_cagr:** -0.2004
    - **strat_maxdd:** -0.2641
    - **bench_maxdd:** -0.3356
- **benchmarks_beaten:** 0

### Gate 9: Risk Diagnostics — ✅ PASS

> 风险可控: Vol=17.9%, MaxDD=-26.4%, VaR=-1.67%

- **ann_volatility:** 0.1795
- **max_drawdown:** -0.2641
- **var_5pct:** -0.0167
- **tail_risk_avg:** -0.0264
- **exposure:** 0.9924
- **beta:** -0.2639
- **correlation:** -0.2636

## Decision

❌ **REJECTED** — 策略未通过以下 Gate:
  - Benchmark Comparison

在修复上述问题之前，不得进入 Paper Trading。
