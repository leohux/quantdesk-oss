# Phase 12 Track B — Event Alpha (parallel, do not mix with RankIC)

Status: PEAD reality PASS → window attribution **PASS_AS_EVENT** (EVENT_DRIVEN). Live LOCKED.

Priority experiments:
1. PEAD — earnings date + EPS surprise + price reaction → 5d/20d forward returns
2. Volume shock — volume z-score + price reaction

## Run

```text
.venv\Scripts\python.exe -m research.event_alpha.run_pead_reality
.venv\Scripts\python.exe -m research.event_alpha.run_pead_attribution
```

Shared event book: `pead_book.py`. Attribution primary gate: early (days 1–5) share of total abn > 50% → EVENT_DRIVEN.

Keep separate from `research/alpha_v2/`. Same gate chain before Live.
