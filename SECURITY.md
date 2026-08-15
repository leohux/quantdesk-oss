# Security Policy

## Live trading is fail-closed by default

QuantDesk ships with **Interactive Brokers (IBKR) live trading locked**.

Default safe state:

```bash
QUANT_MODE=live_locked
IBKR_TRADING_MODE=paper
IBKR_GATEWAY_MODE=paper
IBKR_READ_ONLY=true
LIVE_TRADING_ENABLED=false
LIVE_EXECUTION_ARMED=false
```

Real order submission requires multiple independent unlocks (config + arming token + admin role). Opening the UI or calling a normal API endpoint will **not** place live trades.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Open a [private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-resolving-security-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository, or contact the maintainer directly.

Include:

- Affected component / path
- Steps to reproduce
- Impact (auth bypass, unintended order submission, secret exposure, etc.)
- Suggested fix if you have one

## Secrets

Never commit:

- `.env` (use `.env.example` only)
- Alpaca / IBKR credentials
- Telegram bot tokens
- Live arming tokens
- SSH keys / TLS certificates

If you accidentally push a secret, rotate it immediately and treat the old value as compromised.
