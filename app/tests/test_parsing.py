import validate_briefing as vb

MARKET = """# Daily Market Data

## 11. Key Levels (Entry/Stop/Target must be drawn from this table)
```
TICKER   PRICE   SMA20   SMA50     ATR     S20     R20     S55     R55  MAXPAIN     HOC     HOP
AAPL    312.06  297.46  275.11    4.97  274.61  315.00  245.28  315.00   300.00  320.00  290.00
INTC    114.68  115.16   82.45    9.34   92.61  132.75   40.63  132.75   110.00  120.00  100.00
```
"""

BRIEF = """🎯 TRADE IDEAS

🟢 AAPL · LONG · swing 3-5d · HIGH
Why: holds SMA20
Entry 297.46  Stop 274.61  Tgt 315.00
Invalid: loses SMA50

⚪ INTC · AVOID · n/a · LOW
Why: choppy
Entry/Stop/Tgt: n/a (no level data)

📰 HIGHEST RELEVANCE STORIES
- x
"""


def test_parse_key_levels():
    levels = vb.parse_key_levels(MARKET)
    assert set(levels) == {"AAPL", "INTC"}
    assert levels["AAPL"]["sma20"] == 297.46
    assert levels["AAPL"]["r20"] == 315.00
    assert levels["AAPL"]["maxpain"] == 300.00  # option column parsed


def test_match_level_named_atr_and_closest():
    row = vb.parse_key_levels(MARKET)["AAPL"]
    assert vb.match_level(297.46, row) == "SMA20"
    assert vb.match_level(274.61, row) == "S20"          # closest, not SMA50 (275.11)
    assert vb.match_level(307.09, row) == "PRICE-1.0ATR"  # 312.06 - 1*4.97
    assert vb.match_level(300.00, row) == "MAXPAIN"       # option-implied level
    assert vb.match_level(999.99, row) is None


def test_parse_trade_cards():
    by = {c["ticker"]: c for c in vb.parse_trade_cards(BRIEF, {"AAPL", "INTC"})}
    assert by["AAPL"]["direction"] == "long"
    assert by["AAPL"]["conviction"] == "high"
    assert by["AAPL"]["entry"] == 297.46
    assert by["AAPL"]["target"] == 315.00
    assert by["AAPL"]["in_watchlist"] is True
    assert by["INTC"]["direction"] == "avoid"
    assert by["INTC"]["entry"] is None       # n/a card → no numeric levels


def test_validate_trades_flags_fabricated():
    levels = vb.parse_key_levels(MARKET)
    brief = BRIEF.replace(
        "Entry 297.46  Stop 274.61  Tgt 315.00",
        "Entry 999.99  Stop 111.11  Tgt 888.88",
    )
    results = vb.validate_trades(brief, levels)
    aapl = next(r for r in results if r["ticker"] == "AAPL")
    assert any(matched is None for _l, _v, matched in aapl["checks"])
