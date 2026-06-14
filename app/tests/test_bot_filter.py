import bot

EXEC = "\U0001F525"; MACRO = "\U0001F4CA"; WATCH = "\U0001F440"; TRADE = "\U0001F3AF"
STORY = "\U0001F4F0"; CAND = "\U0001F4C8"; RISK = "⚠️"
B = "•"


def _full():
    return (
        "_meta_\n\n"
        + EXEC + " EXECUTIVE\nx\n\n"
        + MACRO + " MACRO\ny\n\n"
        + WATCH + " WATCHLIST\n" + B + " NVDA\nTrend: Bull\n\n" + B + " TSLA\nTrend: Bear\n\n"
        + TRADE + " TRADE IDEAS\n" + B + " NVDA LONG\n\n"
        + STORY + " STORIES\nz\n\n"
        + CAND + " CANDIDATES\nc\n\n"
        + RISK + " RISKS\nr\n"
    )


def test_trim_to_short_keeps_and_drops():
    short = bot._trim_briefing_to_short(_full())
    assert EXEC in short and MACRO in short and WATCH in short and TRADE in short
    assert STORY not in short and CAND not in short and "⚠" not in short
    assert "meta" in short  # preamble preserved


def test_filter_watchlist_keeps_only_subscribed():
    filt = bot._filter_briefing_for_watchlist(_full(), ["NVDA"])
    assert (B + " NVDA") in filt
    assert (B + " TSLA") not in filt
    assert TRADE in filt  # 🎯 section is outside 👀 and must survive
