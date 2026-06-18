"""Tests for the output-structure validation added to validate_briefing
(validate_all + the individual checks). Fatal vs warning split per the refactor."""
import generate_briefing as g
import validate_briefing as v

MARKET_DATA = """# Daily Market Data
Session: CLOSED

## 1. Live Ticker Dashboard
```
TICKER  PRICE    VOL   PRE    1D    5D   52HI  52LO CAT
NVDA  $205.19  112M  +0.1% +0.2% +0.0% -12%  +44% NEWS
```

## 11. Key Levels
```
TICKER  PRICE   SMA20   SMA50    ATR    S20    R20    S55    R55 MAXPAIN  HOC  HOP GWALL
NVDA   205.19  214.46  206.70   8.33 199.34 232.01 164.08 236.26  207.50 210.00 165.00 210.00
```
"""

GOOD = """🧭 BOTTOM LINE
SpaceX flows dominate the tape.
Risk-on, pre-market.
Top idea: 🟢 NVDA long 205.19→232.01

🔥 EXECUTIVE MARKET READ
One dense paragraph about positioning and rates.

📊 MACRO DASHBOARD
S&P futures: ES 7435 (+0.5%)
Nasdaq futures: NQ 29662 (+0.7%)
10Y yield 4.49% (+0.5%)
VIX 17.68 (-9.1%)
Dollar DXY 99.75 (-0.1%)
WTI Oil 84.88 (-3.2%)
Bitcoin 64322 (-0.2%)
Conditions: risk-on, calm vol.

👀 WATCHLIST DASHBOARD
• NVDA
Trend: Bullish
Risk: Medium

🎯 TRADE IDEAS
🟢 NVDA · LONG · swing 3-5d · HIGH
Why: momentum holding above S20
Entry 205.19  Stop 199.34  Tgt 232.01
Invalid: loses SMA50
Not financial advice. Ideas are derived mechanically from the provided data.

📰 HIGHEST RELEVANCE STORIES
SpaceX IPO matters because it pulls retail flow.

📈 POTENTIAL NEW WATCHLIST CANDIDATES
None with real momentum.

⚠️ KEY RISKS
Sticky CPI could reprice rates.
"""


def test_good_briefing_has_no_fatal():
    fatal, _warnings = v.validate_all(GOOD, MARKET_DATA)
    assert fatal == [], fatal


def test_missing_section_is_fatal():
    broken = GOOD.replace("⚠️ KEY RISKS\nSticky CPI could reprice rates.\n", "")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("KEY RISKS" in e for e in fatal)


def test_placeholder_is_fatal():
    broken = GOOD.replace("• NVDA", "• NVDA\n{{WATCHLIST}}")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("placeholder" in e for e in fatal)


def test_malformed_watchlist_line_is_fatal():
    broken = GOOD.replace("• NVDA", "• NVDA bullish")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("watchlist ticker line" in e for e in fatal)


def test_think_leakage_is_fatal():
    broken = "<think>let me reason</think>\n" + GOOD
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("think" in e.lower() for e in fatal)


def test_ungrounded_trade_level_is_fatal():
    broken = GOOD.replace("Entry 205.19", "Entry 999.99")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("not grounded" in e for e in fatal)


def test_wrong_direction_ordering_is_fatal():
    # LONG with stop above entry (199.34 and 205.19 are both grounded levels).
    broken = GOOD.replace("Entry 205.19  Stop 199.34  Tgt 232.01",
                          "Entry 199.34  Stop 205.19  Tgt 232.01")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("LONG" in e and "stop<entry<target" in e for e in fatal)


def test_oversize_segment_is_fatal_padding_is_warning():
    # One giant segment (> 4096) is fatal.
    huge = GOOD + "\n" + ("x " * 3000)
    fatal, _ = v.validate_all(huge, MARKET_DATA)
    assert any("Telegram limit" in e for e in fatal)

    # 3500 < size < 4096 -> warning, not fatal. Size the pad to land ~3800.
    pad = "y " * ((3800 - len(GOOD)) // 2)
    medium = GOOD + "\n" + pad
    assert 3500 < len(medium) < 4096
    fatal2, warns2 = v.validate_all(medium, MARKET_DATA)
    assert all("Telegram limit" not in e for e in fatal2)
    assert any("target" in w for w in warns2)


def test_markdown_bold_is_fatal():
    noisy = GOOD.replace("Trend: Bullish", "Trend: **Bullish**")
    fatal, _ = v.validate_all(noisy, MARKET_DATA)
    assert any("markdown bold/italic" in e for e in fatal)


def test_code_fence_is_fatal():
    fenced = GOOD.replace("🔥 EXECUTIVE MARKET READ\n",
                          "🔥 EXECUTIVE MARKET READ\n```\nx\n```\n")
    fatal, _ = v.validate_all(fenced, MARKET_DATA)
    assert any("code fence" in e for e in fatal)


# --- watchlist content -----------------------------------------------------

def test_empty_watchlist_cards_is_fatal():
    broken = GOOD.replace("• NVDA\nTrend: Bullish\nRisk: Medium",
                          "No tickers today.")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("no valid '• TICKER'" in e for e in fatal)


def test_numbered_watchlist_bullet_is_fatal():
    broken = GOOD.replace("• NVDA", "1. NVDA")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    # numbered bullet flagged by markdown and/or watchlist-content checks
    assert any("numbered" in e or "no valid" in e for e in fatal)


def test_invented_watchlist_ticker_is_fatal_with_structured():
    structured = {"watchlist": ["AMD"], "macro_dashboard": {}}
    fatal, _ = v.validate_all(GOOD, MARKET_DATA, structured)
    assert any("not in the structured watchlist" in e for e in fatal)


# --- macro dashboard -------------------------------------------------------

def test_macro_missing_required_item_is_fatal():
    broken = GOOD.replace("Bitcoin 64322 (-0.2%)\n", "")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("missing required item: Bitcoin" in e for e in fatal)


def test_macro_off_contract_item_is_fatal():
    broken = GOOD.replace("Conditions: risk-on, calm vol.",
                          "Fed Funds Rate 4.50%")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("off-contract" in e for e in fatal)


# --- trade ideas format ----------------------------------------------------

def test_numbered_trade_ideas_is_fatal():
    broken = GOOD.replace(
        "🟢 NVDA · LONG · swing 3-5d · HIGH\n"
        "Why: momentum holding above S20\n"
        "Entry 205.19  Stop 199.34  Tgt 232.01\n"
        "Invalid: loses SMA50\n",
        "1. NVDA: monitor for a breakout.\n2. TSLA: watch support.\n")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("numbered-list" in e for e in fatal)


def test_trade_idea_missing_levels_is_fatal():
    broken = GOOD.replace("Entry 205.19  Stop 199.34  Tgt 232.01\n", "")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("missing 'Entry" in e for e in fatal)


def test_unsupported_indicator_is_fatal():
    broken = GOOD.replace("Why: momentum holding above S20",
                          "Why: RSI divergence and MACD crossover")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("rsi" in e.lower() for e in fatal)
    assert any("macd" in e.lower() for e in fatal)


def test_no_setup_line_is_accepted():
    no_setup = GOOD.replace(
        "🟢 NVDA · LONG · swing 3-5d · HIGH\n"
        "Why: momentum holding above S20\n"
        "Entry 205.19  Stop 199.34  Tgt 232.01\n"
        "Invalid: loses SMA50\n"
        "Not financial advice. Ideas are derived mechanically from the provided data.\n",
        "No high-conviction setups in today's data.\n"
    ).replace("Top idea: 🟢 NVDA long 205.19→232.01",
              "Top idea: none — no high-conviction setup today.")
    fatal, _ = v.validate_all(no_setup, MARKET_DATA)
    assert fatal == [], fatal


def test_na_levels_trade_idea_is_accepted():
    na = GOOD.replace(
        "Why: momentum holding above S20\n"
        "Entry 205.19  Stop 199.34  Tgt 232.01\n"
        "Invalid: loses SMA50",
        "Why: thin level data\n"
        "Entry/Stop/Tgt: n/a (no level data)\n"
        "Invalid: n/a"
    ).replace("Top idea: 🟢 NVDA long 205.19→232.01",
              "Top idea: none — no high-conviction setup today.")
    fatal, _ = v.validate_all(na, MARKET_DATA)
    assert fatal == [], fatal


# --- bottom line -----------------------------------------------------------

def test_bottom_line_top_idea_mismatch_is_fatal():
    broken = GOOD.replace("Top idea: 🟢 NVDA long 205.19→232.01",
                          "Top idea: 🟢 TSLA long 400.00→450.00")
    fatal, _ = v.validate_all(broken, MARKET_DATA)
    assert any("TSLA" in e and "TRADE IDEAS" in e for e in fatal)


def test_validate_quality_contract_clean():
    assert v.validate_quality_contract(GOOD, None) == []


# --- _strip_wrapping_code_fence (generate_briefing) ------------------------

def test_strip_outer_fence_with_lang_tag():
    wrapped = "```markdown\n" + GOOD.strip() + "\n```"
    out = g._strip_wrapping_code_fence(wrapped)
    assert out == GOOD.strip()
    assert not out.startswith("```")


def test_strip_outer_fence_plain():
    wrapped = "```\n🧭 BOTTOM LINE\nbody\n```"
    assert g._strip_wrapping_code_fence(wrapped) == "🧭 BOTTOM LINE\nbody"


def test_no_fence_is_untouched():
    assert g._strip_wrapping_code_fence(GOOD).strip() == GOOD.strip()


def test_internal_fence_only_is_preserved():
    # A single internal fence (not wrapping the whole thing) must NOT be stripped.
    text = "🧭 BOTTOM LINE\n```\ncode\n```\nmore"
    assert g._strip_wrapping_code_fence(text) == text.strip()


def test_wrapped_with_internal_fences_left_alone():
    # Outer fence + internal fences => more than 2 fence lines => leave as-is
    # (we only strip when the outer fence is unambiguous).
    text = "```\n🧭 BOTTOM LINE\n```\ninner\n```\nend\n```"
    assert g._strip_wrapping_code_fence(text) == text.strip()
