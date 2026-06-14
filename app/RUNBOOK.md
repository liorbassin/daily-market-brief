# Daily Market Agent Runbook

## Daily flow

This setup creates a daily pre-market market briefing.

1. Windows Task Scheduler runs:

```powershell
python market_brief.py collect
```

This writes `market_data.md`.

2. Task Scheduler runs:

```powershell
python generate_briefing.py
```

This reads `market_data.md` and `SKILL.md`, calls the local Ollama model, and writes `final_briefing.md`.

If Ollama is not running, the model is missing, the call times out, or the response is empty, the script still writes a fallback `final_briefing.md` containing the failure reason and the raw `market_data.md`.

3. `bot.py` watches for the new briefing and sends it to Telegram subscribers.

Each subscriber receives a personalized version based on:

* `/language`
* `/format`
* watchlist tickers

## Trade idea grounding

The briefing includes a Trade Ideas section.

Entry, stop, and target prices must be grounded in the Key Levels table from `market_data.md`, including:

* SMA20
* SMA50
* ATR(14)
* 20-day and 55-day swing support/resistance
* MAXPAIN
* high-OI call strikes
* high-OI put strikes
* gamma-wall strikes

The validator flags any trade level that is not grounded in the data or derived from a stated ATR multiple.

## Options data

Options data comes from Tradier, when configured.

The briefing may include:

* Per-ticker put/call ratios
* ATM implied volatility
* Gamma walls
* SPY put/call term structure
* Quotes and liquidity snapshot
* Bid/ask spread
* Average volume
* Market-session header

## Tracking and scorecard

After each successful briefing generation, `generate_briefing.py` logs trade ideas to:

```text
secrets/trades.db
```

It also writes a per-run heartbeat to:

```text
secrets/run_log.jsonl
```

If a trade level is not grounded, or if the run falls back, the admin is notified through Telegram.

A weekly scorecard grades prior ideas against subsequent price action.

## Folder structure

```text
daily_market/
  app/
    market_brief.py
    generate_briefing.py
    bot.py
    market_data.md
    final_briefing.md
    watchlist.json
    requirements.txt
  secrets/
    .env
```

## After restarting the PC

Open PowerShell or the VS Code terminal:

```powershell
cd path/to/daily_market/app
python bot.py
```

## Manual data collection

```powershell
python market_brief.py collect
```

## Manual briefing generation

```powershell
python generate_briefing.py
```

## Validate a briefing

```powershell
python validate_briefing.py
```

This checks `final_briefing.md` against `market_data.md`.

It flags any trade entry, stop, or target that is not a real value from the Key Levels table or derived from an ATR multiple. It also warns on leftover Markdown that would show as literal characters in Telegram.

Exit code `1` means a fabricated level was detected.

Optional custom paths:

```powershell
python validate_briefing.py <brief> <data>
```

## Track and score trade ideas

Ingest happens automatically after a successful `generate_briefing.py` run.

Manual one-off ingest of the current briefing:

```powershell
python scorecard.py ingest
```

Grade open ideas and DM the admin a weekly scorecard:

```powershell
python scorecard.py score
```

This uses `yfinance` and can run even when Ollama is down.

Suggested Task Scheduler cadence:

```text
Friday 7:10 AM
python scorecard.py score
```

## Run the tests

```powershell
pip install -r requirements-dev.txt
python -m pytest tests
```

## Manual Telegram send

```powershell
python market_brief.py send final_briefing.md
```

## Telegram commands

```text
/watchlist
/add TICKER
/remove TICKER
/language
/language ru
/format
/format short
/refresh
```

`/refresh` rebuilds the subscriber's own brief with the latest data and sends it privately.

Admin users have no cooldown. Other users are limited to one refresh every 30 minutes.
