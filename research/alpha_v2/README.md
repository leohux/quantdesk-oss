# Phase 12 — True Alpha Search (Factor Research Platform)

**Status:** ACTIVE  
**Live:** LOCKED  
**Round 1 archived strategies:** do NOT optimize (`ma_trend`, `panda`, `skip5_mom`)

## Goal

Cross-sectional rank prediction, not market direction:

```
P(rank(stock_A, t+5) > rank(stock_B, t+5) | features_t)
```

Primary endpoint: **T+5** (locked by `REGISTRY_CONVENTIONS.md`).

## Tracks

| Track | Path | Status |
|-------|------|--------|
| A | `research/alpha_v2/` T+5 RankIC | **Priority** |
| B | `research/event_alpha/` PEAD / volume shocks | Scaffold only |

## Anti-leakage (fixed)

- Features at close `t` use data `<= t` only
- Label: `close[t+5] / close[t+1] - 1` (skip same-bar / next open leakage)
- No random shuffle; time splits only
- PIT S&P membership mask when available

## Splits (data starts 2021 on current Yahoo cache)

```
Train:      2021-07-01 .. 2022-12-31
Validation: 2023-01-01 .. 2023-12-31
OOS:        2024-01-01 .. 2025-12-31
Holdout:    2026-01-01 ..  (final only)
```

## Hard Gate 12 (Track A)

| Metric | Minimum | Strong |
|--------|---------|--------|
| IC | > 0.02 | > 0.03 |
| RankIC | > 0.03 | > 0.05 |
| Rolling RankIC>0 | ≥ 70% of 12m windows | — |

Then Gate13 Reality (cost Sharpe>1), Gate14 Attribution, Gate15 Stability.

## Phase 12.1 status

```bash
# Factor IC decay + Gate12-A + industry neutralization
.\.venv\Scripts\python.exe -m research.alpha_v2.run_factor_decay

# Track B event / PEAD proxy
.\.venv\Scripts\python.exe -m research.event_alpha.run_pead_baseline
```

Findings (2026-07-30):
- No single factor passes Gate12-A on 2024+
- Industry neutralization helps mildly (e.g. `return_120d`, `r_squared_60`) but still < 0.02 RankIC
- Market-excess label does **not** change CS RankIC (math identity)
- Do **not** add ML until a persistent factor exists
- PEAD earnings feed currently empty via yfinance; shock proxy shows RankIC ≈ -0.07 (reversal)

## Live candidate checklist (future)

1. Gate12 RankIC pass  
2. Gate13 cost Sharpe > 1  
3. Gate14 residual alpha after SPY/MOM/VALUE/LOWVOL  
4. Gate15 multi-year OOS stability  
