"""Tests for the admin-approval access flow: a stranger's request, the admin's
/approve, /deny, /kick, /pending commands, and the private-bot gate."""
import pytest

import db
import core
import bot

ADMIN = 999


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + a known admin chat, with Telegram sends captured instead
    of hitting the network. Returns the list of (chat_id, message) sends."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(db, "LEGACY_WATCHLIST_JSON", tmp_path / "absent.json")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    db.init()

    monkeypatch.setattr(bot, "CHAT_ID", str(ADMIN))
    monkeypatch.setattr(core, "CHAT_ID", str(ADMIN))
    monkeypatch.setattr(core, "ALLOWED_CHAT_IDS", {ADMIN})

    sent = []
    monkeypatch.setattr(
        bot, "send_telegram",
        lambda msg, parse_mode=None, chat_id=None: sent.append((chat_id, msg)) or True,
    )
    return sent


def test_stranger_request_notifies_admin_once(env):
    sent = env
    reply = bot.handle_message("/start", 100, "joe", "Joe")
    assert "access request has been sent" in reply.lower()
    assert any(str(cid) == str(ADMIN) and "access request" in msg.lower()
               for cid, msg in sent)
    assert not core.is_allowed_chat(100)      # not allowed yet
    assert not db.is_subscriber(100)          # and not subscribed

    sent.clear()                              # a repeat must NOT re-ping the admin
    reply2 = bot.handle_message("hi again", 100, "joe", "Joe")
    assert "pending" in reply2.lower()
    assert sent == []


def test_approve_subscribes_and_notifies(env):
    sent = env
    bot.handle_message("/start", 100, "joe", "Joe")
    sent.clear()

    reply = bot.handle_message("/approve 100", ADMIN)
    assert "Approved" in reply
    assert core.is_allowed_chat(100)          # now allowed, no restart needed
    assert db.is_subscriber(100)              # auto-subscribed
    assert db.load_watchlist(100)             # default watchlist seeded
    assert any(cid == 100 and "approved" in msg.lower() for cid, msg in sent)

    # And they can now actually use a subscriber command.
    wl = bot.handle_message("/watchlist", 100, "joe", "Joe")
    assert wl != bot._NOT_AUTHORIZED_TEXT


def test_deny_blocks_and_stays_silent(env):
    sent = env
    bot.handle_message("/start", 100, "joe", "Joe")
    reply = bot.handle_message("/deny 100", ADMIN)
    assert "Denied 100" in reply
    assert not core.is_allowed_chat(100)

    sent.clear()                              # denied chat: generic reply, no re-ping
    r = bot.handle_message("/start", 100, "joe", "Joe")
    assert r == bot._NOT_AUTHORIZED_TEXT
    assert sent == []


def test_kick_unsubscribes_active_subscriber(env):
    env
    bot.handle_message("/start", 100, "joe", "Joe")
    bot.handle_message("/approve 100", ADMIN)
    assert db.is_subscriber(100)

    reply = bot.handle_message("/kick 100", ADMIN)
    assert "Kicked 100" in reply
    assert not db.is_subscriber(100)
    assert not core.is_allowed_chat(100)


def test_pending_lists_requests(env):
    env
    bot.handle_message("/start", 100, "joe", "Joe")
    bot.handle_message("/start", 101, None, "Ann")
    reply = bot.handle_message("/pending", ADMIN)
    assert "100" in reply and "101" in reply


def test_non_admin_cannot_use_admin_commands(env):
    env
    bot.handle_message("/start", 100, "joe", "Joe")
    bot.handle_message("/approve 100", ADMIN)          # 100 is now a normal user

    reply = bot.handle_message("/approve 200", 100, "joe", "Joe")
    assert "Approved" not in reply                     # not treated as admin
    assert not core.is_allowed_chat(200)
    assert not db.is_subscriber(200)


def test_approve_usage_and_validation(env):
    env
    assert "Usage" in bot.handle_message("/approve", ADMIN)
    assert "Invalid" in bot.handle_message("/approve notanid", ADMIN)
