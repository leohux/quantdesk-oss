# ❌ Hard Gates Validation Report

**Strategy:** VolatilityTargetStrategy
**Symbol:** SPY
**Timestamp:** 2026-07-11T16:43:51.925048
**Overall:** FAIL (8/9 passed)

## Gate Summary

| # | Gate | Status | Message |
|---|------|--------|---------|
| 1 | Sector Concentration | ✅ PASS | 单标的策略 (SPY)，行业检查不适用 |
| 2 | Universe Pollution | ✅ PASS | Universe 干净，1 个标的全部通过 |
| 3 | IS/OOS Gap | ✅ PASS | IS/OOS Gap = -0.461，无过拟合迹象 |
| 4 | Cost Sensitivity | ✅ PASS | 成本稳健，10bps 下 Sharpe = 0.813 |
| 5 | Single Asset Contribution | ✅ PASS | 单标的策略 (SPY) |
| 6 | Event Contribution | ⚠️ WARNING | 单笔最大交易贡献 100.0%，可能是事件驱动（无财报数据确认） |
| 7 | Stability | ✅ PASS | 稳定性良好，100% 窗口为正，CV=0.35 |
| 8 | Benchmark Comparison | ❌ FAIL | 未打败任何基准 (SPY, QQQ, XLK) |
| 9 | Risk Diagnostics | ✅ PASS | 风险可控: Vol=17.9%, MaxDD=-24.5%, VaR=-1.67% |

## Backtest Statistics

- **sharpe:** 0.8177
- **cagr:** 0.1394
- **maxdd:** -0.2450
- **sortino:** 1.0006
- **ann_vol:** 0.1792
- **win_rate:** 55.11%
- **turnover:** 4.5000
- **n_years:** 9.9700
- **total_return:** 267.13%
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

> IS/OOS Gap = -0.461，无过拟合迹象

- **is_sharpe:** 0.6885
- **oos_sharpe:** 1.15
- **gap:** -0.4612
- **is_period:** 2016-07-13 00:00:00 ~ 2023-07-07 00:00:00
- **oos_period:** 2023-07-10 00:00:00 ~ 2026-07-10 00:00:00
- **is_bars:** 1758
- **oos_bars:** 754

### Gate 4: Cost Sensitivity — ✅ PASS

> 成本稳健，10bps 下 Sharpe = 0.813

- **sharpe_0bps:** 0.8177
- **sharpe_5bps:** 0.8155
- **sharpe_10bps:** 0.8133
- **sharpe_20bps:** 0.8089
- **decay_0to10:** 0.0044
- **decay_0to20:** 0.0088

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

> 稳定性良好，100% 窗口为正，CV=0.35

- **window_years:** 3.00
- **n_windows:** 24
- **mean_sharpe:** 0.7841
- **std_sharpe:** 0.2750
- **min_sharpe:** 0.4044
- **max_sharpe:** 1.30
- **pct_positive:** 100.00
- **median_sharpe:** 0.7408

### Gate 8: Benchmark Comparison — ❌ FAIL

> 未打败任何基准 (SPY, QQQ, XLK)

- **comparisons:**
  - **SPY:**
    - **strat_sharpe:** 0.8177
    - **bench_sharpe:** 0.8829
    - **excess_sharpe:** -0.0652
    - **strat_cagr:** 0.1394
    - **bench_cagr:** 0.1527
    - **excess_cagr:** -0.0133
    - **strat_maxdd:** -0.2450
    - **bench_maxdd:** -0.3372
  - **QQQ:**
    - **strat_sharpe:** 0.8177
    - **bench_sharpe:** 0.9823
    - **excess_sharpe:** -0.1646
    - **strat_cagr:** 0.1394
    - **bench_cagr:** 0.2154
    - **excess_cagr:** -0.0760
    - **strat_maxdd:** -0.2450
    - **bench_maxdd:** -0.3512
  - **XLK:**
    - **strat_sharpe:** 0.8177
    - **bench_sharpe:** 1.02
    - **excess_sharpe:** -0.2063
    - **strat_cagr:** 0.1394
    - **bench_cagr:** 0.2496
    - **excess_cagr:** -0.1102
    - **strat_maxdd:** -0.2450
    - **bench_maxdd:** -0.3356
- **benchmarks_beaten:** 0

### Gate 9: Risk Diagnostics — ✅ PASS

> 风险可控: Vol=17.9%, MaxDD=-24.5%, VaR=-1.67%

- **ann_volatility:** 0.1792
- **max_drawdown:** -0.2450
- **var_5pct:** -0.0167
- **tail_risk_avg:** -0.0273
- **exposure:** 0.9896
- **beta:** 0.5572
- **correlation:** 0.5572

## Decision

❌ **REJECTED** — 策略未通过以下 Gate:
  - Benchmark Comparison

在修复上述问题之前，不得进入 Paper Trading。
