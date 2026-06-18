GENERATION CONTRACT (machine-checked after you finish — violations are repaired automatically, so get them right the first time)

Primary data source:
- Use market_data_structured.json as the PRIMARY source of truth.
- Use the raw market_data.md only to resolve ambiguity in the structured JSON.
- For any news story whose fetch_status is not "success", treat the article body as unavailable: do NOT invent its details. Do not cite or analyze an article's content unless its text, summary, title, or catalyst is actually present in the structured JSON.
- Source URLs must be copied EXACTLY from the structured JSON. Never shorten, guess, or fabricate a link.

Required sections — output ALL of these, in this order, each with its exact emoji + ALL-CAPS label:
🧭 BOTTOM LINE
🔥 EXECUTIVE MARKET READ
📊 MACRO DASHBOARD
👀 WATCHLIST DASHBOARD
🎯 TRADE IDEAS
📰 HIGHEST RELEVANCE STORIES
📈 POTENTIAL NEW WATCHLIST CANDIDATES
⚠️ KEY RISKS

Watchlist dashboard:
- One entry per ticker in the structured watchlist.
- Each entry's first line is EXACTLY "• TICKER" (bullet + symbol only). Not "• NVDA bullish", not "• NVDA - bullish", not "• NVDA (Bullish)".

Trade ideas:
- Entry/Stop/Tgt must each be a value from that ticker's Key Levels row (PRICE, SMA20, SMA50, S20, R20, S55, R55, MAXPAIN, HOC, HOP, GWALL), the current price, or current price ± a stated ATR multiple.
- LONG → Stop below Entry, Target above Entry. SHORT → Stop above Entry, Target below Entry.
- If a ticker has no Key Levels row, write "Entry/Stop/Tgt: n/a (no level data)" instead of inventing numbers.

Potential new watchlist candidates:
- Draw from the structured non_watchlist_mentions list (tickers appearing in the news/Reddit flow that are NOT in the price table). Do not force candidates; include only names with real narrative momentum.

Telegram splitting:
- Insert "[[SPLIT]]" only between top-level sections; aim for ~3,500 characters per segment and never exceed 4,096.

Output plain text only — no markdown, no code blocks, no HTML, no literal {{...}} tokens, and no <think> reasoning.
