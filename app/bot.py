"""
bot.py
=======
ALWAYS-ON Telegram process. Does TWO things in the same loop:

  1. INBOUND  — long-polls Telegram for /add, /remove, /watchlist commands
                and edits watchlist.json accordingly.
  2. OUTBOUND — watches final_briefing.md for changes and forwards its
                contents to Telegram when something else (e.g. a Claude
                analysis step) writes a new version.

The two roles share one loop because they both need to be alive at the
same time, share the same Telegram credentials, and supervising one
process is simpler than supervising two.

Trade-off worth knowing:
  Telegram long-polling blocks for up to 25s waiting for incoming messages.
  The file watcher only re-checks AFTER each long-poll returns. So worst-
  case file-change detection latency is ~25s. For a once-a-day briefing
  this is invisible.

Run modes:
  - For testing:   python bot.py        (kill with Ctrl+C)
  - As a Windows service: wrap with NSSM, set restart-on-failure
  - From Task Scheduler:  trigger "At log on" with "Run whether user is
                          logged on or not" and "Restart on failure"
"""

# parse Watchlist Dashboard section + ticker blocks
import re
import time                                   # backoff sleep on errors in the loop
import threading                              # background worker for /refresh
import requests                               # HTTP for Telegram's REST API
# filesystem path for the briefing watcher
from pathlib import Path

# Anything that's also used by the daily brief is imported from core, so
# both files stay in sync if the underlying logic ever changes.
from core import (
    BOT_TOKEN,
    CHAT_ID,
    DEFAULT_FORMAT,
    DEFAULT_LANGUAGE,
    DEFAULT_WATCHLIST,
    SUPPORTED_FORMATS,
    SUPPORTED_LANGUAGES,
    add_tickers,
    check_required_env,
    is_allowed_chat,
    is_supported_format,
    is_supported_language,
    remove_tickers,
    seed_new_subscriber,
    send_telegram,
    send_telegram_status,
    show_watchlist,
    translate_text,
    SEND_OK,
    SEND_BLOCKED,
)

# Subscriber persistence. db.init() creates the schema + runs the one-shot
# migration of watchlist.json on first run.
import db

# /refresh runs the same pipeline as the daily job, in-process: market_brief
# builds the data, generate_briefing runs the LLM. Imported lazily-safe here
# (no import cycle: neither imports bot).
import market_brief
import generate_briefing

# --- /refresh state -------------------------------------------------------
# Single-run lock: only ONE refresh fetches data + hits Ollama at a time, so
# the heavy work and market_brief's module-level run caches are never touched
# by two threads at once. Per-chat cooldown: the admin (env CHAT_ID) is exempt;
# everyone else is limited to once per _REFRESH_COOLDOWN_SEC. Only the main
# loop thread touches _refresh_last (in handle_message), so it needs no lock.
_refresh_lock = threading.Lock()
# chat_id -> time.monotonic() of last refresh
_refresh_last = {}
_REFRESH_COOLDOWN_SEC = 1800                  # 30 min for non-admin subscribers


# ----------------------------------------------------------------------------
# OUTBOUND: file-watching configuration (re-added — was lost in a prior edit)
# ----------------------------------------------------------------------------
# We watch BRIEFING_PATH for changes. Whenever its mtime/size differs from
# the signature we previously sent, we read it and forward the contents to
# Telegram. SENT_HASH_PATH persists the last-sent signature so a restart
# doesn't accidentally re-send the same briefing.
APP_DIR = Path(__file__).resolve().parent
BRIEFING_PATH = APP_DIR / "final_briefing.md"
SENT_HASH_PATH = APP_DIR / ".last_sent"

# Per-language translation cache for one briefing version. Keyed by language
# code; the whole dict is cleared on every NEW file signature so we never
# serve a stale translation of yesterday's briefing. In-memory only — a bot
# restart recomputes translations on the next file change, which is fine
# because translations are cheap (one HTTP call per chunk per language).
_translation_cache = {}
_translation_cache_sig = None

# Notice prefixed in front of the English original when translation fails.
# Sent as a separate message so the user immediately sees the system error
# distinctly from the briefing text itself.
_TRANSLATION_ERROR_NOTICE = (
    "⚠️ Translation system down — sending English original below."
)


def file_signature(path):
    """Cheap "did this file change?" fingerprint: mtime + size, no hashing.
    For a daily briefing this is plenty — meaningful edits always change
    at least one of those two values."""
    if not path.exists():
        return None
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def load_last_sent():
    """Signature of the briefing we last forwarded. Returns None on first
    run, so the very first briefing always sends."""
    if not SENT_HASH_PATH.exists():
        return None
    return SENT_HASH_PATH.read_text(encoding="utf-8").strip()


def save_last_sent(signature):
    """Persist the signature so a restart doesn't re-send the same file."""
    SENT_HASH_PATH.write_text(signature, encoding="utf-8")


# Watchlist-section start anchor. The 👀 emoji is the only language-
# invariant signal in the section header (the trailing label gets
# translated). Match the line that BEGINS with the emoji.
_WATCHLIST_SECTION_RE = re.compile(r"^👀.*$", re.MULTILINE)

# Other section header anchors used to find where the 👀 section ENDS.
# Any line starting with one of these emojis terminates the watchlist
# block. Order doesn't matter — we just look for the next match after
# the watchlist header. 🎯 (Trade Ideas) is included so the 👀 section
# terminates correctly when Trade Ideas follows it.
_OTHER_SECTION_EMOJIS = ("🧭", "🔥", "📊", "🎯", "📰", "📈", "⚠️")
_OTHER_SECTION_RE = re.compile(
    r"^(" + "|".join(re.escape(e) for e in _OTHER_SECTION_EMOJIS) + r").*$",
    re.MULTILINE,
)

# Short-format section control. Every top-level briefing section begins with
# one of these emojis (from SKILL.md's required structure). The SHORT brief
# keeps only the leading set; the heavier 📰 Stories, 📈 Candidates, and
# ⚠️ Risks sections are dropped. Emojis are language-invariant, so this works
# on the post-translation text just like the watchlist filter does.
# 🧭 BOTTOM LINE is the first section (the BLUF) — always kept in short format.
_ALL_SECTION_EMOJIS = ("🧭", "🔥", "📊", "👀", "🎯", "📰", "📈", "⚠️")
_SHORT_KEEP_EMOJIS = ("🧭", "🔥", "📊", "👀", "🎯")
_ANY_SECTION_RE = re.compile(
    r"^(" + "|".join(re.escape(e) for e in _ALL_SECTION_EMOJIS) + r").*$",
    re.MULTILINE,
)

# Ticker-block start: a line containing ONLY a bullet + ticker symbol,
# nothing else. Tolerates `•` (canonical), `*`, or `-` because some
# translation passes normalize the bullet character. Ticker shape matches
# core.normalize_ticker (uppercase letters, dot, caret, equals).
_TICKER_LINE_RE = re.compile(
    r"^\s*[•*\-]\s+([A-Z][A-Z0-9.\^=]{0,9})\s*$"
)


def _filter_briefing_for_watchlist(briefing, watchlist):
    """Return `briefing` with the 👀 Watchlist Dashboard section trimmed
    to only the ticker blocks present in `watchlist`.

    Behavior:
      - If there is no 👀 section, return `briefing` unchanged.
      - If `watchlist` is empty, drop the entire 👀 section (header + body)
        so the user doesn't see an orphan header.
      - If none of the section's ticker blocks match `watchlist`, drop
        the entire 👀 section for the same reason.
      - Otherwise keep the 👀 header line, the prose before the first
        ticker block (Claude sometimes puts a one-line lead-in), and only
        the ticker blocks whose symbol is in `watchlist`. Tickers in the
        watchlist that Claude didn't write about are silently omitted.

    Pure function (no I/O); safe to call repeatedly with the same cached
    translation per subscriber.
    """
    section_match = _WATCHLIST_SECTION_RE.search(briefing)
    if section_match is None:
        return briefing                                  # no 👀 — nothing to filter

    section_start = section_match.start()
    body_start = section_match.end()

    # Find the next section emoji AFTER the watchlist header.
    after = _OTHER_SECTION_RE.search(briefing, pos=body_start)
    section_end = after.start() if after else len(briefing)

    head = briefing[:section_start]
    section_text = briefing[section_start:section_end]
    tail = briefing[section_end:]

    # Empty watchlist → drop the section entirely. Strip a trailing newline
    # off `head` so we don't leave a double blank where the section was.
    if not watchlist:
        return head.rstrip("\n") + ("\n\n" if tail.strip() else "") + tail

    # Walk the section line by line, collecting ticker blocks. A "block"
    # is the ticker line plus every following line up to (but not including)
    # the next ticker line. Pre-ticker lines (header + any lead-in prose)
    # are kept verbatim.
    lines = section_text.split("\n")
    watchset = set(watchlist)
    # list of (ticker_or_None, list_of_lines)
    blocks = []
    current_ticker = None
    current_lines = []

    for line in lines:
        m = _TICKER_LINE_RE.match(line)
        if m:
            # Flush the previous block before starting a new one.
            if current_lines:
                blocks.append((current_ticker, current_lines))
            current_ticker = m.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append((current_ticker, current_lines))

    # First block has ticker=None (the header + any prose). Keep it only
    # if at least one ticker block matches; otherwise drop the whole section.
    keep_blocks = [b for b in blocks if b[0] is None or b[0] in watchset]
    has_match = any(b[0] is not None for b in keep_blocks)
    if not has_match:
        return head.rstrip("\n") + ("\n\n" if tail.strip() else "") + tail

    # Normalize spacing: strip each block's trailing blank lines and rejoin
    # with a single blank line between blocks. Otherwise a dropped trailing
    # block leaves the kept block visually crammed against the next section.
    parts = []
    for _, block_lines in keep_blocks:
        while block_lines and block_lines[-1] == "":
            block_lines = block_lines[:-1]
        parts.append("\n".join(block_lines))
    filtered_section = "\n\n".join(parts)
    # Trailing blank line before the next section, matching the original.
    if tail.strip():
        filtered_section += "\n\n"

    return head + filtered_section + tail


def _trim_briefing_to_short(briefing):
    """Return `briefing` reduced to the SHORT section set: keep only sections
    whose header starts with a `_SHORT_KEEP_EMOJIS` emoji (🔥 Exec, 📊 Macro,
    👀 Watchlist, 🎯 Trade Ideas), plus any preamble before the first section
    header (the generate_briefing metadata line). Drops 📰 Stories, 📈
    Candidates, ⚠️ Risks.

    Anchors on the leading emoji of each section header — language-invariant,
    so this runs on the already-translated text, mirroring how the watchlist
    filter works. If no recognizable section headers are found, returns the
    briefing unchanged (don't silently blank out an unexpected format).
    """
    matches = list(_ANY_SECTION_RE.finditer(briefing))
    if not matches:
        return briefing

    # Preamble before the first section header (e.g. the "_Generated …_" line).
    parts = [briefing[:matches[0].start()].rstrip("\n")]
    for i, m in enumerate(matches):
        emoji = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(briefing)
        if emoji in _SHORT_KEEP_EMOJIS:
            parts.append(briefing[m.start():end].rstrip("\n"))

    # Re-join kept blocks with one blank line; drop an empty preamble.
    return "\n\n".join(p for p in parts if p.strip()) + "\n"


# Lines that get bolded in the HTML-formatted send. Every anchor is
# language-invariant (section emoji / ASCII ticker / the Entry-Stop-Tgt line),
# so formatting is applied AFTER translation without depending on English words.
_TRADE_HEADER_EMOJIS = ("🟢", "🔴", "⚪")
_TRADE_LEVEL_RE = re.compile(r"^\s*Entry\b.*\bStop\b", re.IGNORECASE)


def _apply_html_formatting(text):
    """Convert the plain-text briefing into Telegram-HTML, bolding the
    structural lines a reader scans for: section headers (🧭/🔥/📊/…), watchlist
    ticker lines (• TICKER), trade-card header lines (🟢/🔴/⚪ …), and the
    Entry/Stop/Tgt level line.

    Runs as the LAST step before send (after translate → trim → filter): the
    LLM never emits markup and translation can't mangle it. Body text is HTML-
    escaped FIRST; only then are <b> tags injected on the structural lines, so a
    stray '<', '>' or '&' in a headline can't break Telegram parsing. If a tag
    still trips the API, core._send_one falls back to plain text per chunk.
    """
    out = []
    for raw in text.split("\n"):
        line = raw.replace("&", "&amp;").replace(
            "<", "&lt;").replace(">", "&gt;")
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        is_struct = (
            stripped.startswith(_ALL_SECTION_EMOJIS)
            or stripped.startswith(_TRADE_HEADER_EMOJIS)
            or _TICKER_LINE_RE.match(raw)
            or _TRADE_LEVEL_RE.match(raw)
        )
        out.append(
            f"{indent}<b>{stripped}</b>" if (is_struct and stripped) else line)
    return "\n".join(out)


def _translation_for(lang, briefing, sig):
    """Return (text_to_send, ok) for `lang`. `text_to_send` is what the
    subscriber will receive; `ok` is False when we fell back to English
    because translation failed (caller may want to send an extra notice).

    Cached per file signature: the first subscriber in a language pays
    the API cost, the rest are served from memory until a new briefing
    arrives.
    """
    global _translation_cache, _translation_cache_sig
    if _translation_cache_sig != sig:
        # New briefing — wipe last run's translations so nobody gets a
        # stale Russian copy of yesterday's English.
        _translation_cache = {}
        _translation_cache_sig = sig

    if lang in _translation_cache:
        return _translation_cache[lang]

    translated, err = translate_text(briefing, lang)
    if translated is None:
        # Failure — fall back to English. Stash the (briefing, False)
        # tuple in the cache so we don't hammer a broken endpoint once
        # per subscriber in the same language group.
        print(f"Translation to '{lang}' failed: {err}")
        result = (briefing, False)
    else:
        result = (translated, True)
    _translation_cache[lang] = result
    return result


def _broadcast_briefing(briefing, sig, delivered=None):
    """Send `briefing` to every active subscriber in their chosen language.
    Returns True only if every subscriber who still needed it was reached — the
    caller uses that to decide whether to advance the .last_sent watermark.

    `delivered` is a set of chat_ids that already received THIS briefing
    signature on a prior attempt. Those chats are skipped, so a retry after a
    partial failure only re-sends to the chats that actually missed it — a chat
    that already got the brief is never sent a duplicate. Each chat is added to
    `delivered` the moment its main briefing send succeeds.

    Formatting: the briefing is plain text from the LLM; bold is added in
    code by _apply_html_formatting as the final step (after translate → trim →
    filter) and sent with parse_mode="HTML". Body text is escaped before any
    tags are injected, and core._send_one retries any rejected chunk as plain
    text, so a stray character can never make a subscriber miss the brief.
    The translation-failure notice stays plain (parse_mode=None) — it's a
    fixed string with nothing to format.
    """
    delivered = delivered if delivered is not None else set()
    # Allowlist filter: an existing active subscriber who isn't (or is no
    # longer) on the allowlist — e.g. a stranger who subscribed before the
    # allowlist existed — is silently dropped here, so the /start gate and the
    # broadcast can never disagree about who's a valid recipient.
    recipients = [
        r for r in db.list_active_subscribers_with_prefs() if is_allowed_chat(r[0])
    ]
    if not recipients:
        print("  no active allowlisted subscribers — nothing to broadcast.")
        return True

    overall_ok = True
    for chat_id, lang, fmt in recipients:
        if chat_id in delivered:
            continue  # already received this exact briefing — never resend

        # Unknown language code in the DB (e.g. someone removed an entry
        # from SUPPORTED_LANGUAGES) — fall back to the default.
        if not is_supported_language(lang):
            lang = DEFAULT_LANGUAGE

        text, translated_ok = _translation_for(lang, briefing, sig)

        # Per-subscriber transform (short-trim → watchlist-filter → bold) on
        # top of the cached translation, so we don't re-translate per subscriber.
        personalized = _personalize_text(text, chat_id, fmt)

        if not translated_ok and lang != DEFAULT_LANGUAGE:
            # Tell the user their language is unavailable, then send the
            # English original so they're not left without a brief. Best-effort:
            # the brief send below is what decides this chat's disposition, so a
            # blip on this notice doesn't independently pin the watermark.
            send_telegram(
                _TRANSLATION_ERROR_NOTICE, parse_mode=None, chat_id=chat_id
            )

        status = send_telegram_status(
            personalized, parse_mode="HTML", chat_id=chat_id)
        if status == SEND_OK:
            delivered.add(chat_id)
            print(f"  sent to chat {chat_id} (lang={lang}, fmt={fmt}).")
        elif status == SEND_BLOCKED:
            # Permanently unreachable (blocked the bot / deleted account). Drop
            # it from the active set so it can NEVER pin the watermark again,
            # and DON'T flip overall_ok — the brief reached everyone reachable,
            # so the watermark should advance. This is the fix for the stuck-
            # watermark re-send loop a single blocked stranger used to cause.
            db.deactivate_subscriber(chat_id)
            print(f"  chat {chat_id} unreachable (403/blocked) — deactivated; "
                  "won't retry.")
        else:  # SEND_FAILED — transient; hold the watermark and retry next loop
            print(f"  send to chat {chat_id} (lang={lang}) failed (transient); "
                  "will retry.")
            overall_ok = False
    return overall_ok


def _personalize_text(text, chat_id, fmt):
    """Apply the per-subscriber transforms to an (already-translated) briefing:
    drop heavy sections for SHORT format, filter the 👀 Watchlist Dashboard to
    this chat's tickers, then add HTML bold as the final step. Shared by the
    daily broadcast and /refresh so formatting stays identical. All anchors are
    language-invariant (section emojis / ASCII tickers), so this runs fine on
    translated text. Bolding is applied LAST so markup can't be mangled
    upstream by translation."""
    if fmt == "short":
        text = _trim_briefing_to_short(text)
    text = _filter_briefing_for_watchlist(text, db.load_watchlist(chat_id))
    return _apply_html_formatting(text)


def _deliver_to_chat(briefing, chat_id):
    """Translate (per the chat's language), personalize, and send `briefing` to
    a SINGLE chat. Used by /refresh — independent of the broadcast translation
    cache (this is one-off content). Returns True if the briefing send
    succeeded."""
    lang = db.get_language(chat_id) or DEFAULT_LANGUAGE
    if not is_supported_language(lang):
        lang = DEFAULT_LANGUAGE
    fmt = db.get_format(chat_id) or DEFAULT_FORMAT
    if not is_supported_format(fmt):
        fmt = DEFAULT_FORMAT

    text, err = translate_text(briefing, lang)
    if text is None:
        print(f"Translation to '{lang}' failed for /refresh: {err}")
        send_telegram(_TRANSLATION_ERROR_NOTICE,
                      parse_mode=None, chat_id=chat_id)
        text = briefing                                  # English fallback

    personalized = _personalize_text(text, chat_id, fmt)
    return send_telegram(personalized, parse_mode="HTML", chat_id=chat_id)


def _run_refresh(chat_id):
    """Background worker for /refresh: rebuild the brief for `chat_id`'s
    watchlist IN MEMORY and send it ONLY to that chat. Never writes the shared
    market_data.md / final_briefing.md, so the file watcher doesn't broadcast
    it to everyone. Always releases the single-run lock."""
    try:
        watchlist = db.load_watchlist(chat_id)
        market_data = market_brief.build_market_data(watchlist)
        briefing, _ok = generate_briefing.build_briefing_text(market_data)
        _deliver_to_chat(briefing, chat_id)
        print(f"/refresh delivered to chat {chat_id}.")
    except Exception as e:  # noqa: BLE001 — worker must never crash the bot
        print(f"/refresh for chat {chat_id} failed: {type(e).__name__}: {e}")
        try:
            send_telegram(
                f"⚠️ Refresh failed ({type(e).__name__}). Your scheduled "
                "brief is unaffected — try again in a bit.",
                chat_id=chat_id,
            )
        except Exception:                                # nothing more we can do
            pass
    finally:
        _refresh_lock.release()


def _handle_refresh_command(chat_id):
    """Validate cooldown + single-run lock, then kick off a background refresh.
    Returns the immediate reply. Admin (env CHAT_ID) bypasses the cooldown;
    other subscribers are limited to once per _REFRESH_COOLDOWN_SEC."""
    is_admin = _is_admin(chat_id)
    now = time.monotonic()

    if not is_admin:
        last = _refresh_last.get(chat_id)
        if last is not None and now - last < _REFRESH_COOLDOWN_SEC:
            wait_min = int((_REFRESH_COOLDOWN_SEC - (now - last)) // 60) + 1
            return (
                f"You refreshed recently — try again in ~{wait_min} min. "
                "(The scheduled brief still arrives automatically.)"
            )

    if not _refresh_lock.acquire(blocking=False):
        return ("A refresh is already running — you'll get the updated brief "
                "shortly.")

    if not is_admin:
        _refresh_last[chat_id] = now
    threading.Thread(target=_run_refresh, args=(chat_id,), daemon=True).start()
    return ("🔄 Refreshing with the latest market data — this takes a minute "
            "or two. I'll send your updated brief here when it's ready.")


# ----------------------------------------------------------------------------
# INBOUND: low-level Telegram polling
# ----------------------------------------------------------------------------

def get_updates(offset=None):
    """Long-poll Telegram for new updates.

    `offset` is the next update_id we expect. Passing it tells Telegram
    "I've already processed everything before this id, don't send it again".
    Without offset, Telegram would re-deliver the same updates each call.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    # `timeout` here is the LONG-POLL timeout sent to Telegram: the server
    # holds the connection open up to this many seconds, returning the
    # instant a new message arrives. So the bot reacts in ~100ms when you
    # send a message, NOT every 25s.
    #
    # `allowed_updates` filters out edited messages, channel posts, etc. —
    # we only handle plain incoming messages.
    params = {"timeout": 25, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset

    # The HTTP-side timeout MUST be larger than the long-poll timeout,
    # otherwise the requests library gives up before Telegram has had a
    # chance to reply. 30 > 25 leaves 5s of slack for network latency.
    response = requests.get(url, params=params, timeout=30)
    return response.json()


def delete_webhook():
    """Clear any leftover webhook on startup.

    If a webhook was ever registered for this bot, getUpdates returns
    nothing — Telegram pushes updates to the webhook URL instead, and the
    polling loop sits there forever receiving empty results. Calling
    deleteWebhook is idempotent and safe to do every time the bot starts.
    """
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": False},   # keep queued messages
            timeout=10,
        )
    except requests.RequestException as e:
        print(
            f"deleteWebhook failed (continuing anyway): {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------
# INBOUND: command dispatch
# ----------------------------------------------------------------------------

# Shown for /start (initial join + later /help-style fall-through). The
# DEFAULT_WATCHLIST line is interpolated at call time so it's always
# accurate even if the constant changes.
_HELP_TEXT = """Commands:
/watchlist          show your watchlist
/add NVDA, TSLA     add one or more tickers
/remove SPY         remove one or more
/language           show language +  options
/language ru        set language
/format             show brief length + options
/format short       set brief length (short or full)
/refresh            rebuild your brief now with the latest data
/stop               unsubscribe""".strip()

# Sent to a denied chat (or as the generic fallback). Kept deliberately vague —
# no command list, no hint the bot does anything useful — so an unknown chat
# can't probe it. The allowlist lives in core (admin CHAT_ID + ALLOWED_CHAT_IDS)
# plus the DB-backed approvals in db.access.
_NOT_AUTHORIZED_TEXT = "This is a private bot."

# First message from a chat we've never seen: we file an access request and tell
# them it's been forwarded to the admin.
_ACCESS_REQUESTED_TEXT = (
    "This is a private bot. Your access request has been sent to the admin — "
    "you'll get a message here if you're approved."
)

# Repeat message from a chat whose request is already on the admin's queue.
_ACCESS_PENDING_TEXT = (
    "Your access request is still pending the admin's review. You'll be "
    "notified here once it's decided."
)

# Sent to a chat the moment the admin approves it.
_ACCESS_GRANTED_TEXT = "✅ You've been approved! You'll start receiving the daily brief here."

# Admin-only commands, routed before the private-bot gate when sent by the admin.
_ACCESS_ADMIN_COMMANDS = ("/approve", "/deny", "/kick", "/pending")


def _format_supported_languages():
    """Pretty list of supported codes for use in /language replies.
    Shape: 'en (English), ru (Русский), he (עברית)'."""
    return ", ".join(
        f"{code} ({meta['display']})"
        for code, meta in SUPPORTED_LANGUAGES.items()
    )


def _handle_language_command(chat_id, args):
    """Implementation of /language with and without arguments. Returns
    the reply string. Assumes the caller has already verified that
    `chat_id` is an active subscriber."""
    if not args:
        current = db.get_language(chat_id) or DEFAULT_LANGUAGE
        return (
            f"Your briefing language: {current}\n"
            f"Supported: {_format_supported_languages()}\n"
            "Change with /language <code>, e.g. /language ru"
        )
    code = args[0].lower().strip()
    if not is_supported_language(code):
        return (
            f"Unsupported language '{code}'.\n"
            f"Supported: {_format_supported_languages()}"
        )
    if db.set_language(chat_id, code):
        return f"Briefing language set to {code} ({SUPPORTED_LANGUAGES[code]['display']})."
    # The is_subscriber gate upstream should make this unreachable, but
    # keep a sane message in case the row vanished between checks.
    return "Could not update language — please /start first."


def _format_supported_formats():
    """Pretty list of supported brief lengths for use in /format replies.
    Shape: 'full (Full (all sections)), short (Short (…))'."""
    return ", ".join(
        f"{code} ({meta['display']})"
        for code, meta in SUPPORTED_FORMATS.items()
    )


def _handle_format_command(chat_id, args):
    """Implementation of /format with and without arguments. Returns the
    reply string. Assumes the caller has already verified that `chat_id`
    is an active subscriber. Mirrors _handle_language_command."""
    if not args:
        current = db.get_format(chat_id) or DEFAULT_FORMAT
        return (
            f"Your brief length: {current}\n"
            f"Options: {_format_supported_formats()}\n"
            "Change with /format <option>, e.g. /format short"
        )
    value = args[0].lower().strip()
    if not is_supported_format(value):
        return (
            f"Unsupported format '{value}'.\n"
            f"Options: {_format_supported_formats()}"
        )
    if db.set_format(chat_id, value):
        return f"Brief length set to {value} ({SUPPORTED_FORMATS[value]['display']})."
    # is_subscriber gate upstream should make this unreachable.
    return "Could not update format — please /start first."


def _welcome_text(is_returning):
    """Welcome banner sent on /start. Tailored slightly so a returning
    user (post /stop, then /start again) isn't told their watchlist
    starts with the defaults — theirs was preserved on the soft-delete."""
    if is_returning:
        return (
            "Welcome back. You're re-subscribed and your previous watchlist"
            " was preserved.\n\n" + _HELP_TEXT
        )
    return (
        "Welcome to Daily Market Brief.\n\n"
        "You're subscribed — you'll get the daily pre-market brief here"
        " each morning.\n"
        "Your watchlist starts with: " + ", ".join(DEFAULT_WATCHLIST) + ".\n\n"
        + _HELP_TEXT
    )


# ----------------------------------------------------------------------------
# Access control: stranger request flow + admin approve/deny/kick
# ----------------------------------------------------------------------------

def _is_admin(chat_id):
    """True if `chat_id` is the env-configured admin chat. Compared as strings
    because CHAT_ID comes from env as a string and Telegram sends ints."""
    return bool(CHAT_ID) and str(chat_id) == str(CHAT_ID)


def _parse_chat_id_arg(s):
    """Parse a /approve|/deny|/kick argument into an int chat_id, or None."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _notify_admin_of_request(chat_id, username, first_name):
    """DM the admin that a new chat wants access, with one-tap approve/deny
    commands. Best-effort: a delivery failure here just means the admin can
    still find the request via /pending."""
    if not CHAT_ID:
        return
    name = first_name or "(no name)"
    handle = f"@{username}" if username else "(no username)"
    msg = (
        "🔔 New access request\n"
        f"Name: {name} {handle}\n"
        f"chat_id: {chat_id}\n\n"
        f"Approve: /approve {chat_id}\n"
        f"Deny:    /deny {chat_id}"
    )
    try:
        send_telegram(msg, parse_mode=None, chat_id=CHAT_ID)
    except Exception as e:  # noqa: BLE001 — notifying must never break the reply
        print(f"Failed to notify admin of access request from {chat_id}: {e}")


def _handle_unapproved(chat_id, username, first_name):
    """Reply for a chat that isn't allowed yet. Files an access request the
    first time and pings the admin; stays quiet (no re-ping) for repeats and
    for chats the admin already denied."""
    status = db.record_access_request(chat_id, username, first_name)
    if status == "new":
        _notify_admin_of_request(chat_id, username, first_name)
        print(f"New access request from chat_id={chat_id} (@{username or '-'}).")
        return _ACCESS_REQUESTED_TEXT
    if status == "pending":
        return _ACCESS_PENDING_TEXT
    # 'denied' (or any stale status) — generic refusal, no info leak, no re-ping.
    return _NOT_AUTHORIZED_TEXT


def _approve_chat(target):
    """Admin granted access to `target`: mark approved, subscribe them (seeding
    the default watchlist if brand-new), and notify them. Returns the admin's
    confirmation line."""
    acc = db.get_access(target)
    username = acc[2] if acc else None
    first_name = acc[3] if acc else None

    db.set_access_status(target, "approved", username, first_name)
    # Subscribe now so they don't have to /start; seed only a brand-new chat.
    was_new = db.add_subscriber(target, username, first_name)
    if was_new and not db.load_watchlist(target):
        seed_new_subscriber(target)

    try:
        send_telegram(_ACCESS_GRANTED_TEXT + "\n\n" + _HELP_TEXT, chat_id=target)
    except Exception as e:  # noqa: BLE001
        print(f"Approved {target} but failed to notify them: {e}")
    name = first_name or str(target)
    return f"✅ Approved {name} ({target}). They're subscribed and notified."


def _revoke_chat(target, verb):
    """Shared logic for /deny and /kick: mark denied and deactivate any
    subscription so `target` stops receiving briefs and can't re-spam the admin.
    Silent toward the target (no antagonizing message)."""
    db.set_access_status(target, "denied")
    removed = db.deactivate_subscriber(target)
    suffix = " (was an active subscriber — now unsubscribed)" if removed else ""
    return f"🚫 {verb} {target}.{suffix}"


def _format_pending():
    """Render the admin's /pending list of chats awaiting a decision."""
    rows = db.list_pending()
    if not rows:
        return "No pending access requests."
    lines = ["Pending access requests:"]
    for cid, username, first_name, _requested_at in rows:
        name = first_name or "(no name)"
        handle = f" @{username}" if username else ""
        lines.append(f"• {name}{handle} — {cid}")
    lines.append("\nApprove: /approve <chat_id>   Deny: /deny <chat_id>")
    return "\n".join(lines)


def _handle_access_admin(command, args):
    """Dispatch an admin access command. Caller has already confirmed the
    sender is the admin."""
    if command == "/pending":
        return _format_pending()
    if not args:
        return f"Usage: {command} <chat_id>"
    target = _parse_chat_id_arg(args[0])
    if target is None:
        return f"Invalid chat id: {args[0]!r}"
    if command == "/approve":
        return _approve_chat(target)
    if command == "/deny":
        return _revoke_chat(target, "Denied")
    if command == "/kick":
        return _revoke_chat(target, "Kicked")
    return _HELP_TEXT  # unreachable — command was in _ACCESS_ADMIN_COMMANDS


def handle_message(text, chat_id, username=None, first_name=None):
    """Map a raw text message to one of the bot commands. Returns the
    reply string that will be sent back to the caller.

    /start is the only command available to non-subscribers — every other
    command is gated on db.is_subscriber so an unregistered chat can't
    drive watchlist edits."""
    parts = text.strip().split()                    # split on whitespace
    if not parts:
        return "Empty command."

    # commands case-insensitive
    command = parts[0].lower()

    # Admin-only access management (/approve, /deny, /kick, /pending). Only the
    # admin chat can drive these; for anyone else they fall through to the gate
    # below (so a stranger typing /approve learns nothing).
    if command in _ACCESS_ADMIN_COMMANDS and _is_admin(chat_id):
        return _handle_access_admin(command, parts[1:])

    # Private-bot gate. This is the door that used to be open: /start
    # auto-subscribed anyone. A non-allowlisted chat can't subscribe or run
    # commands — instead its first message files an access request the admin
    # can approve from Telegram. The admin + ALLOWED_CHAT_IDS + already-approved
    # chats pass straight through.
    if not is_allowed_chat(chat_id):
        return _handle_unapproved(chat_id, username, first_name)

    if command == "/start":
        # add_subscriber returns True for brand-new OR previously-inactive
        # chats. We seed the default watchlist ONLY for brand-new ones —
        # check the existing watchlist count to distinguish.
        was_change = db.add_subscriber(chat_id, username, first_name)
        existing = db.load_watchlist(chat_id)
        if was_change and not existing:
            seed_new_subscriber(chat_id)
            return _welcome_text(is_returning=False)
        # Reactivated (had a watchlist already) OR already-active /start.
        return _welcome_text(is_returning=bool(existing))

    if command == "/stop":
        if db.deactivate_subscriber(chat_id):
            return (
                "Unsubscribed. Your watchlist is preserved if you /start"
                " again later."
            )
        return "You're not currently subscribed. Send /start to join."

    # Everything past this point requires an active subscription.
    if not db.is_subscriber(chat_id):
        return "Send /start first to subscribe to the daily brief."

    if command == "/add" and len(parts) >= 2:
        return add_tickers(chat_id, parts[1:])
    if command == "/remove" and len(parts) >= 2:
        return remove_tickers(chat_id, parts[1:])
    if command == "/watchlist":                     # no args expected
        return show_watchlist(chat_id)
    if command == "/language":                      # 0 or 1 args
        return _handle_language_command(chat_id, parts[1:])
    if command == "/format":                        # 0 or 1 args
        return _handle_format_command(chat_id, parts[1:])
    if command == "/refresh":                       # no args expected
        return _handle_refresh_command(chat_id)

    # Unrecognized command or plain text — show help. The admin also sees the
    # access-management commands so they're discoverable beyond the per-request
    # notification.
    if _is_admin(chat_id):
        return _HELP_TEXT + (
            "\n\nAdmin:\n"
            "/pending            list access requests\n"
            "/approve <chat_id>  grant access\n"
            "/deny <chat_id>     reject a request\n"
            "/kick <chat_id>     remove a subscriber"
        )
    return _HELP_TEXT


# ----------------------------------------------------------------------------
# Main loop — handles INBOUND messages AND OUTBOUND file changes
# ----------------------------------------------------------------------------

def run_bot():
    missing = check_required_env()
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        return

    db.init()                                       # schema + first-run migration
    delete_webhook()                                # ensure long polling works

    print("Bot listener started.")
    print(f"  Admin (env) chat ID:    {CHAT_ID}")
    print(f"  Active subscribers:     {db.list_active_chat_ids()}")
    print(f"  Watching briefing file: {BRIEFING_PATH}")

    offset = None                                   # last seen Telegram update_id + 1
    # last forwarded briefing signature
    last_sig = load_last_sent()
    # Per-signature set of chat_ids already delivered, so a retry after a
    # partial failure never re-sends to a chat that already got the brief.
    delivered_by_sig = {}

    while True:
        try:
            # ---- INBOUND: long-poll Telegram (blocks up to 25s) -----------
            updates = get_updates(offset)

            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message", {})
                text = message.get("text", "")
                chat = message.get("chat", {}) or {}
                sender = message.get("from", {}) or {}
                chat_id = chat.get("id")
                if chat_id is None:                  # skip channel posts etc.
                    continue
                username = sender.get("username")
                first_name = sender.get("first_name")
                print(
                    f"Received message from chat_id={chat_id} "
                    f"(@{username or '-'}): {text}"
                )

                # No more env-CHAT_ID gate. handle_message decides whether
                # the caller is a subscriber and replies appropriately;
                # /start is the one open entry point that promotes a chat
                # into the subscribers table.
                reply = handle_message(text, chat_id, username, first_name)
                send_telegram(reply, chat_id=chat_id)

            # ---- OUTBOUND: forward briefing if it changed -----------------
            # Runs once per loop iteration (~every 25s in the quiet case).
            #
            # Broadcasts final_briefing.md to EVERY active subscriber in
            # their chosen language. Translations are computed once per
            # language (not per subscriber) and cached for the lifetime
            # of this file signature.
            sig = file_signature(BRIEFING_PATH)
            if sig and sig != last_sig:
                print("New final_briefing.md detected. Broadcasting...")
                briefing = BRIEFING_PATH.read_text(encoding="utf-8")
                delivered = delivered_by_sig.setdefault(sig, set())
                if _broadcast_briefing(briefing, sig, delivered):
                    save_last_sent(sig)
                    last_sig = sig
                    delivered_by_sig.clear()   # this sig is done; free memory
                    print("Broadcast complete.")
                else:
                    # Don't update last_sig on partial failure — retry on the
                    # next loop iteration. `delivered` carries which chats already
                    # got it, so only the chats that MISSED it are retried; nobody
                    # who already received the brief gets a duplicate.
                    print(
                        "Broadcast had failures; will retry only the missed chats "
                        "on the next loop iteration."
                    )

        except requests.RequestException as e:
            # Network blip — back off briefly and try the whole loop again.
            print(f"getUpdates failed: {type(e).__name__}: {e}")
            time.sleep(5)
        except Exception as e:
            # Catch-all so a single malformed update or file-read error
            # can't kill the bot. If you see this firing repeatedly,
            # something needs a real fix.
            print(f"bot loop error: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
