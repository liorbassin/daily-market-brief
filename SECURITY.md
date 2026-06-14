# Security

## Secret handling

All credentials (Telegram bot token, chat ID, FRED / Financial Modeling Prep / Tradier API
keys) live **only** in `secrets/.env`, which is loaded at runtime by `app/core.py`.

- `secrets/` and `.env` are git-ignored and **must never be committed**. Only
  `app/.env.example` (placeholders, no real values) is tracked.
- Nothing reads or transmits the values except the scripts that need them, via environment
  variables.
- The **Tradier** token is a free **sandbox** credential — market data only, no funded account
  and no trading/order access.

## If a credential is ever exposed

Rotate the affected secret immediately; the old value should be treated as compromised:

- **Telegram bot token** — revoke/re-issue via [@BotFather](https://t.me/BotFather)
  (`/revoke` → new token), then update `secrets/.env`.
- **Financial Modeling Prep / FRED** — regenerate the key in your account dashboard.
- **Tradier** — reset the sandbox access token in the developer dashboard.

If a secret was committed to git, rotate it **and** remove it from history (e.g. a fresh clean
baseline commit, or `git filter-repo`) before pushing — git history is published in full.

## Reporting a vulnerability

This is a personal project. Please open a GitHub issue for non-sensitive reports, or contact the
maintainer directly for anything sensitive. There is no formal SLA.
