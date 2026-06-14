# Daily Market Agent

A local-first, pre-market **market-intelligence agent**. It collects market data, has a local
LLM (via [Ollama](https://ollama.com)) write a disciplined analyst briefing, and delivers it to
**Telegram** subscribers — personalized to each user's watchlist, language, and preferred
length. Trade ideas are grounded in real price levels, tracked, and graded weekly.

Everything runs on your own machine. API keys are stored locally in a git-ignored secrets/.env file and are only used for the services you configure.

---

## Features

- **Market dashboards** — per-ticker prices/volume/52-week range, a macro dashboard (ES, NQ,
  10Y, VIX, DXY, oil, BTC), pre-market movers, earnings + economic calendars, and a news scan
  (Yahoo Finance, WSJ, Reddit).
- **Options positioning (Tradier, with greeks)** — per-ticker put/call ratios, **ATM implied
  vol**, **gamma walls**, max-pain / high-OI strikes, a **SPY PCR term structure**, unusual-
  volume flags, a **quotes/liquidity** snapshot (bid/ask spread, average volume), and a
  **market-session** header.
- **LLM analyst brief** — a local Ollama model turns the data into a structured briefing led by
  a **🧭 BOTTOM LINE** (the one thing that matters + the top research idea), followed by an
  executive read, watchlist dashboard, 🎯 Trade Ideas, top stories, new candidates, and risks.
- **Grounded trade ideas** — every Entry/Stop/Target must come from the "Key Levels" table
  (SMA20/50, ATR, swing S/R, MAXPAIN, HOC/HOP, **GWALL**) or a stated ATR multiple. A validator
  flags any fabricated level.
- **Telegram delivery** — HTML-formatted (bold headers/tickers/levels), split into clean
  messages, **per-subscriber** translation (`/language`), length (`/format` short/full), and
  watchlist filtering.
- **On-demand `/refresh`** — any subscriber can rebuild *their own* brief intraday with fresh
  data, delivered privately (admin: no cooldown; others: once per 30 min).
- **Tracking + scorecard** — ideas are logged to a local database and graded against subsequent
  price action; a weekly scorecard is DM'd to the admin.

## Architecture

Several small scripts plus one always-on bot, coordinated through files on disk:
`market_brief.py collect` → `market_data.md` → `generate_briefing.py` (Ollama) →
`final_briefing.md` → `bot.py` watches that file and fans it out to Telegram. See
[`app/ARCHITECTURE.md`](app/ARCHITECTURE.md) for the full diagrams and the "two delivery paths"
explainer.

## Requirements

- **Python 3.9+**
- **[Ollama](https://ollama.com)** running locally with a pulled model (default `llama3.1:8b`)
- A **Telegram bot** token (from [@BotFather](https://t.me/BotFather))
- Optional API keys (each gracefully skipped if unset): **FRED**, **Financial Modeling Prep**,
  **Tradier** (free sandbox)

## Setup

```bash
git clone <your-repo-url> daily_market
cd daily_market

python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r app/requirements.txt

# Secrets: copy the example and fill in your values.
# Create a `secrets/` folder at the repo root and put `.env` inside it.
cp app/.env.example secrets/.env      # then edit secrets/.env

# Pull a model for the analyst layer
ollama pull llama3.1:8b
```

See [`app/.env.example`](app/.env.example) for every variable and where to get each key.
`secrets/` and `.env` are git-ignored — **never commit them**.

## Running

```bash
cd app
python market_brief.py collect      # fetch data  -> market_data.md
python generate_briefing.py         # Ollama       -> final_briefing.md
python bot.py                        # always-on: forwards the brief + handles commands
```

For a daily cadence, schedule `collect` then `generate_briefing` a few minutes apart (Windows
Task Scheduler / cron) and keep `bot.py` running. Optional weekly `python scorecard.py score`
grades the prior week's ideas. Full details and validation/scoring commands are in [`app/RUNBOOK.md`](app/RUNBOOK.md).

## Telegram commands

```
/start                 subscribe
/watchlist             show your watchlist
/add NVDA, TSLA        add tickers
/remove SPY            remove tickers
/language [code]       show / set briefing language (e.g. en, ru, he)
/format [short|full]   show / set brief length
/refresh               rebuild your brief now with the latest data (sent only to you)
/stop                  unsubscribe
```

## Security

API keys and chat IDs live only in `secrets/.env` (git-ignored). See [`SECURITY.md`](SECURITY.md)
for handling and what to do if a credential is ever exposed.

## Disclaimer

This project is for personal research and automation only. It does not provide financial advice, investment recommendations, or trading signals. It does not execute trades. Any trade plans or scorecards are hypothetical research outputs generated from market data and model analysis, which may be incomplete, delayed, or wrong. Use at your own risk.

## License

[MIT](LICENSE).
