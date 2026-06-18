import bot
import core

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


def test_broadcast_retry_sends_no_duplicate(monkeypatch):
    """A TRANSIENT failure must re-send ONLY to the chat that missed it — a chat
    that already received the brief is never sent a duplicate on retry, and the
    watermark is held until the transient failure clears."""
    sent = []
    state = {"fail_once_chat2": True}

    def fake_send(text, parse_mode=None, chat_id=None):
        sent.append(chat_id)
        if chat_id == 2 and state["fail_once_chat2"]:
            state["fail_once_chat2"] = False
            return core.SEND_FAILED  # chat 2 has a transient blip on attempt 1
        return core.SEND_OK

    monkeypatch.setattr(bot, "send_telegram_status", fake_send)
    monkeypatch.setattr(bot, "is_allowed_chat", lambda c: True)
    monkeypatch.setattr(bot.db, "list_active_subscribers_with_prefs",
                        lambda: [(1, "en", "full"), (2, "en", "full")])
    monkeypatch.setattr(bot, "_translation_for", lambda lang, b, sig: (b, True))
    monkeypatch.setattr(bot, "_personalize_text", lambda t, c, f: t)
    monkeypatch.setattr(bot, "is_supported_language", lambda lang: True)

    delivered = set()
    ok1 = bot._broadcast_briefing("brief", "sig1", delivered)
    assert ok1 is False            # chat 2 failed -> watermark won't advance
    assert delivered == {1}        # only chat 1 delivered

    ok2 = bot._broadcast_briefing("brief", "sig1", delivered)
    assert ok2 is True             # retry reaches chat 2
    assert delivered == {1, 2}

    assert sent.count(1) == 1      # chat 1 NEVER got a duplicate
    assert sent.count(2) == 2      # chat 2 retried once


def test_broadcast_blocked_chat_is_deactivated_and_advances(monkeypatch):
    """A BLOCKED chat (403) must be deactivated and must NOT pin the watermark:
    the brief reached everyone reachable, so the broadcast reports success."""
    deactivated = []

    def fake_send(text, parse_mode=None, chat_id=None):
        return core.SEND_BLOCKED if chat_id == 2 else core.SEND_OK

    monkeypatch.setattr(bot, "send_telegram_status", fake_send)
    monkeypatch.setattr(bot, "is_allowed_chat", lambda c: True)
    monkeypatch.setattr(bot.db, "list_active_subscribers_with_prefs",
                        lambda: [(1, "en", "full"), (2, "en", "full")])
    monkeypatch.setattr(bot.db, "deactivate_subscriber", deactivated.append)
    monkeypatch.setattr(bot, "_translation_for", lambda lang, b, sig: (b, True))
    monkeypatch.setattr(bot, "_personalize_text", lambda t, c, f: t)
    monkeypatch.setattr(bot, "is_supported_language", lambda lang: True)

    delivered = set()
    ok = bot._broadcast_briefing("brief", "sig1", delivered)
    assert ok is True              # blocked chat doesn't hold the watermark
    assert delivered == {1}        # only the reachable chat counts as delivered
    assert deactivated == [2]      # the blocked chat was dropped


def test_broadcast_skips_non_allowlisted(monkeypatch):
    """An active subscriber that isn't on the allowlist is never sent to."""
    sent = []
    monkeypatch.setattr(bot, "send_telegram_status",
                        lambda t, parse_mode=None, chat_id=None: sent.append(chat_id) or core.SEND_OK)
    monkeypatch.setattr(bot, "is_allowed_chat", lambda c: c == 1)
    monkeypatch.setattr(bot.db, "list_active_subscribers_with_prefs",
                        lambda: [(1, "en", "full"), (2, "en", "full")])
    monkeypatch.setattr(bot, "_translation_for", lambda lang, b, sig: (b, True))
    monkeypatch.setattr(bot, "_personalize_text", lambda t, c, f: t)
    monkeypatch.setattr(bot, "is_supported_language", lambda lang: True)

    bot._broadcast_briefing("brief", "sig1", set())
    assert sent == [1]             # chat 2 (not allowlisted) was skipped
