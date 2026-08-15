# Contributing to QuantDesk

Thanks for taking an interest. QuantDesk is a research / paper-first US equities stack with a **fail-closed** live path. Contributions that improve safety, clarity, and test coverage are especially welcome.

## Development setup

```bash
# API
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Web
cd web && npm install && npm run dev
```

## Guidelines

1. **Keep live locked.** Do not weaken fail-closed gates (`LIVE_TRADING_ENABLED`, `LIVE_EXECUTION_ARMED`, account whitelist, read-only default) without an explicit design review and tests.
2. **Shared strategy interface.** Prefer `generate_signals(close, params) -> (entries, exits)` so backtest / paper / live stay aligned.
3. **No secrets in PRs.** Never commit `.env`, API keys, tokens, or personal infrastructure details.
4. **Tests.** Add or update tests under `tests/` for risk, OMS, and live-lock behavior.
5. **Docs.** Update `docs/` when you change APIs, event flow, or deployment.

## Pull requests

- Keep PRs focused (one concern per PR).
- Describe what changed and how you verified it (`pytest`, manual smoke, Docker).
- Link related issues when applicable.

## Code of conduct

Be respectful. This is a trading-adjacent project — treat capital safety and other people's money as sacred, even in paper mode.
