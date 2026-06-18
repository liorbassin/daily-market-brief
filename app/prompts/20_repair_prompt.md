REPAIR TASK

A previously generated market briefing FAILED automated validation. Your job is to return a corrected version of the full briefing.

Your previous output passed neither the format contract nor the quality contract.
You must rewrite it into the exact SKILL.md structure.
Do not use markdown.
Do not use numbered trade ideas.
Do not invent indicators.
Do not include a trade idea unless you can produce Entry / Stop / Tgt from supplied Key Levels.
If you cannot produce valid trade ideas, write exactly:
No high-conviction setups in today's data.

Rules:
- Fix ONLY the validation errors listed below.
- Do NOT add new facts, headlines, catalysts, ratings, or prices.
- Do NOT invent or change any price. Every trade Entry/Stop/Target must come from the Key Levels data, exactly as in SKILL.md's rules.
- Do NOT change sections that are already valid unless a fix requires it.
- Preserve the exact SKILL.md section structure, order, emoji headers, and the plain-text Telegram formatting rules (no markdown, no code blocks, no HTML, no literal {{...}}, no <think> reasoning).
- Use only the structured JSON and raw market_data.md below as data, plus the invalid briefing as the text to correct.
- Return the COMPLETE corrected briefing (every section), not a diff and not just the changed parts. Output nothing except the corrected briefing.

OUTPUT CONTRACT:
Return ONLY the final_briefing.md content.
Do not explain.
Do not wrap the answer in a code block.
Do not use markdown fences.
The first line of the response must be exactly:
🧭 BOTTOM LINE

The output must contain these exact section headers:
🧭 BOTTOM LINE
🔥 EXECUTIVE MARKET READ
📊 MACRO DASHBOARD
👀 WATCHLIST DASHBOARD
🎯 TRADE IDEAS
📰 HIGHEST RELEVANCE STORIES
📈 POTENTIAL NEW WATCHLIST CANDIDATES
⚠️ KEY RISKS

If you cannot produce a valid section, include the section header anyway and write:
Data unavailable from supplied input.

VALIDATION ERRORS TO FIX:
{errors}

STRUCTURED DATA (market_data_structured.json):
{structured_json}

RAW MARKET DATA (market_data.md, for ambiguity only):
{market_data}

INVALID BRIEFING TO CORRECT:
{invalid_briefing}
