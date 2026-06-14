"""
core.py
========
Shared helpers used by BOTH market_brief.py (daily one-shot) and bot.py
(always-on Telegram listener).

Anything that touches:
  - environment variables / secrets
  - the watchlist file on disk
  - the Telegram HTTP API
  - text validation/escaping
...lives here, so neither entry-point file has to duplicate it.

Reading tip: this file is "pure plumbing". Nothing in here fetches market
data or runs the bot loop. If you're tempted to add a yfinance/feedparser
call in this file, it probably belongs in market_brief.py instead.

Why a third file at all?
  bot.py and market_brief.py both need to read the watchlist, escape
  Markdown, and call sendMessage. Without core.py one of them would have
  to import the other, which would also drag in heavy modules (yfinance,
  feedparser) that the bot doesn't need. core.py keeps imports lean.
"""

# standard library: read environment variables
import os
# regex for ticker validation and Markdown escaping
import re
import json                                # parse Google Translate's JSON-ish response
# URL-encode text for the translate endpoint
from urllib.parse import quote
# HTTP client used to call Telegram's REST API
import requests
# safer filesystem paths than raw strings
from pathlib import Path
# loads a .env file into os.environ for local dev
from dotenv import load_dotenv

# db.py owns subscriber + watchlist persistence (SQLite at secrets/bot.db).
# Importing it here means env vars are loaded BEFORE db.init() reads them —
# load_dotenv() runs at module import below, then db.init() is called by
# entry points (bot.py, market_brief.py) when they're ready.
import db


# ----------------------------------------------------------------------------
# Environment loading
# ----------------------------------------------------------------------------
# Path(__file__) is THIS file's path. .resolve() makes it absolute, then
# .parent.parent walks up two directories: app/  ->  daily_market/  ->  the
# secrets folder lives at daily_market/secrets/.env. Anchoring like this
# means the script works regardless of which directory you launch it from.
env_path = Path(__file__).resolve().parent.parent / "secrets" / ".env"
# leftover debug — safe to delete
print(f"[core] Loading env from: {env_path}")
# leftover debug — safe to delete
print(f"[core] Env file exists: {env_path.exists()}")
load_dotenv(env_path)


# ----------------------------------------------------------------------------
# Public configuration constants
# ----------------------------------------------------------------------------
# Telegram bot auth token
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# destination chat id (string!)
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# User-Agent on Reddit RSS requests
REDDIT_USER_AGENT = "market-agent by u/devil2stronk"

# FMP (Financial Modeling Prep) — optional. Powers the earnings calendar
# and the pre-market-movers fallback in market_brief.py. If unset, those
# sections degrade gracefully ('section skipped' instead of crashing the
# whole brief).
FMP_API_KEY = os.getenv("FMP_API_KEY")

# FRED (St. Louis Fed) — optional. Powers the economic calendar section.
# Free key at https://fred.stlouisfed.org/docs/api/api_key.html. If unset,
# the calendar section degrades gracefully.
FRED_API_KEY = os.getenv("FRED_API_KEY")

# Tradier (sandbox) — optional. Powers the per-ticker put/call ratios in the
# options-positioning section. Free sandbox token at
# https://developer.tradier.com/ (no funding required). If unset, the
# per-watchlist PCR sub-block is skipped; the market-wide PCR + VIX-term
# sub-block still renders (those don't need Tradier).
TRADIER_TOKEN = os.getenv("TRADIER_TOKEN")

# Ollama (local LLM) — used by generate_briefing.py to write final_briefing.md
# from market_data.md. Both vars come from .env so the host/model aren't baked
# into source; the fallbacks here just keep a fresh checkout runnable against
# a default localhost install. Override OLLAMA_URL when Ollama is bound to a
# non-default host/port or proxied through Tailscale/a reverse proxy; override
# OLLAMA_MODEL to swap in any model you've pulled (`ollama pull <name>`).
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# Vars that MUST be set — the project can't function without them.
REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "TELEGRAM_CHAT_ID": CHAT_ID,
}


def check_required_env():
    """
    NEW HELPER (was inline inside main() before the split):
    Returns a list of missing required-env-var NAMES, or [] if all set.
    Both market_brief.main() and bot.run_bot() call this on startup so the
    "fail fast with the missing variable names" rule lives in one place.
    """
    return [name for name, val in REQUIRED_ENV.items() if not val]


# ----------------------------------------------------------------------------
# Watchlist persistence — now backed by SQLite (db.py); each Telegram chat
# has its own watchlist scoped by chat_id. The legacy watchlist.json is
# imported once on first DB init and then ignored.
# ----------------------------------------------------------------------------

# Reserved chars in Telegram's legacy "Markdown" parse mode. If a headline
# or post title contains any of these unescaped, Telegram returns 400 and
# the whole message is dropped silently. _MD_RE is compiled once at import
# time (faster than recompiling on every call to escape_md()).
_MD_RE = re.compile(r"([_*`\[])")

# Seed list given to brand-new /start subscribers so their first /watchlist
# isn't empty. Existing single-user setups inherit their own watchlist via
# the one-shot db.py migration of watchlist.json — they don't get this seed.
DEFAULT_WATCHLIST = ["SPY", "QQQ", "DIA", "IWM", "^VIX",
                     "^TNX", "GOOG", "NFLX", "MU", "NVDA", "TSLA", "AMD"]


def load_watchlist(chat_id):
    """Return the tickers for `chat_id` as a fresh list (callers can mutate
    without affecting the DB). Empty list if the chat has no rows.

    Note: `chat_id` must be an int. The bot stores chat IDs as ints in the
    DB; callers receiving the value from Telegram's API should cast first
    (Telegram returns ints; reading from env returns strings)."""
    return db.load_watchlist(int(chat_id))


def seed_new_subscriber(chat_id):
    """Copy DEFAULT_WATCHLIST into the DB for a newly-added subscriber.
    Called by the /start handler in bot.py right after add_subscriber()
    reports a brand-new (vs reactivated) chat. Reactivated chats keep
    their prior tickers and are NOT re-seeded."""
    db.add_watchlist_tickers(int(chat_id), list(DEFAULT_WATCHLIST))


# ----------------------------------------------------------------------------
# Ticker validation + watchlist commands (called by bot.py)
# ----------------------------------------------------------------------------

def normalize_ticker(ticker):
    """Canonicalize user input ('$aapl ' -> 'AAPL'). Returns None on garbage,
    so callers can use `if not ticker: ...` to reject bad input."""
    ticker = ticker.upper().strip()                    # tickers are upper-cased by convention
    # users often type "$AAPL" — drop the cashtag
    ticker = ticker.replace("$", "")
    # Allow letters, dot (BRK.B), caret (^VIX), equals (FX pairs like EURUSD=X).
    if not re.match(r"^[A-Z.^=]{1,10}$", ticker):
        return None
    return ticker


# NEW: regex matching anything that separates ticker arguments. Lets users
# type any of these and have them all parse the same way:
#   /add NVDA TSLA AMD           (plain whitespace)
#   /add NVDA, TSLA, AMD         (comma + spaces)
#   /add NVDA,TSLA,AMD           (commas, no spaces)
#   /add NVDA  ,, TSLA           (sloppy mix — also fine)
_TICKER_DELIM_RE = re.compile(r"[,\s]+")


def _split_ticker_args(raw_args):
    """NEW: take the post-command tokens (a list like ['NVDA,', 'TSLA,', 'AMD'])
    and return a flat list of clean ticker strings with empties dropped.

    Trick: we re-join with a space and then re-split on commas OR whitespace,
    which collapses every possible separator combination into one normalized
    list. Saves us from juggling multiple split rules in the caller."""
    joined = " ".join(raw_args)
    return [t for t in _TICKER_DELIM_RE.split(joined) if t]


def _format_change_report(action, ok, skipped, invalid, skip_reason):
    """NEW: build a multi-line summary of a bulk add/remove operation.

    Three buckets so the user knows exactly what happened to each input:
      `ok`       — tickers that succeeded (e.g. "Added: NVDA, TSLA")
      `skipped`  — valid tickers that couldn't be applied (e.g. already listed)
      `invalid`  — raw inputs that failed normalize_ticker (e.g. "Invalid: hello")

    Empty buckets are omitted, so a fully clean operation just shows one
    "Added: ..." line instead of three lines with two "(none)" fillers."""
    lines = []
    if ok:
        lines.append(f"{action}: {', '.join(ok)}")
    if skipped:
        lines.append(f"Skipped ({skip_reason}): {', '.join(skipped)}")
    if invalid:
        lines.append(f"Invalid: {', '.join(invalid)}")
    return "\n".join(lines) if lines else "Nothing changed."


def add_tickers(chat_id, raw_args):
    """Add one or many tickers to `chat_id`'s watchlist. Accepts the same
    flexible separator forms as before (comma, whitespace, mix).

    Validation happens here (normalize_ticker, dedupe of invalid input);
    the actual INSERT-vs-skip distinction happens in db.add_watchlist_tickers,
    where SQLite's per-row PK conflict handling lives. This split keeps the
    "what counts as a valid ticker" rule out of the DB layer."""
    inputs = _split_ticker_args(raw_args)
    if not inputs:
        return "No tickers provided."

    valid, invalid = [], []
    for raw in inputs:
        norm = normalize_ticker(raw)
        if norm:
            valid.append(norm)
        else:
            # keep original so user sees what failed
            invalid.append(raw)

    added, duplicates = db.add_watchlist_tickers(int(chat_id), valid)
    return _format_change_report(
        "Added", added, duplicates, invalid,
        skip_reason="already in watchlist",
    )


def remove_tickers(chat_id, raw_args):
    """Remove one or many tickers from `chat_id`'s watchlist. Same multi-input
    shape as add_tickers — see that docstring for the design reasoning."""
    inputs = _split_ticker_args(raw_args)
    if not inputs:
        return "No tickers provided."

    valid, invalid = [], []
    for raw in inputs:
        norm = normalize_ticker(raw)
        if norm:
            valid.append(norm)
        else:
            invalid.append(raw)

    removed, missing = db.remove_watchlist_tickers(int(chat_id), valid)
    return _format_change_report(
        "Removed", removed, missing, invalid,
        skip_reason="not in watchlist",
    )


def show_watchlist(chat_id):
    """Return a human-readable listing of the chat's watchlist tickers, or a
    friendly 'empty' message when nothing is being tracked yet."""
    watchlist = load_watchlist(chat_id)
    if not watchlist:
        return "Your watchlist is empty."
    return "Watchlist:\n" + "\n".join(watchlist)


# ----------------------------------------------------------------------------
# Telegram I/O
# ----------------------------------------------------------------------------

def escape_md(text):
    """Escape Markdown reserved chars so user-supplied text (news titles,
    Reddit posts, etc.) can't break Telegram's parser. r"\\\\\\1" inserts
    a backslash before whichever character matched the group."""
    return _MD_RE.sub(r"\\\1", text)


# Telegram's hard per-message limit is 4096 chars. We stay under it with
# a buffer because: (a) Markdown escape sequences expand the byte count,
# (b) emoji are counted as multiple chars, (c) some clients add CRLF
# instead of LF. 3900 leaves headroom without leaving readable space on
# the table.
MAX_TELEGRAM_LENGTH = 3900


# Regex matching a `[[SPLIT]]` marker line on its own — tolerates
# surrounding whitespace and the blank lines SKILL.md asks Claude to put
# around it. Compiled multiline so `^`/`$` match line boundaries.
_SPLIT_MARKER_RE = re.compile(r"^\s*\[\[SPLIT\]\]\s*$", re.MULTILINE)


def _split_on_markers(text):
    """Split `text` on `[[SPLIT]]` marker lines. Strips the markers and
    the blank lines surrounding them so the resulting chunks read cleanly.

    Returns a single-element list when no markers are present, so callers
    can blindly iterate. Empty fragments (e.g. consecutive markers) are
    dropped.

    Why this exists: SKILL.md instructs Claude to insert `[[SPLIT]]` to
    indicate Telegram message boundaries, with the promise that the
    sender strips them. This is the strip-and-respect side of that promise.
    """
    if not _SPLIT_MARKER_RE.search(text):
        return [text]
    parts = _SPLIT_MARKER_RE.split(text)
    return [p.strip("\n") for p in parts if p.strip()]


def _split_message(text, max_length=MAX_TELEGRAM_LENGTH):
    """NEW: split `text` into chunks <= max_length, breaking on paragraph
    boundaries ("\\n\\n") so each chunk still reads as a complete thought.

    Why split on paragraphs and not on raw character count: a brute cut
    at 3900 chars could land inside an unclosed `*bold*` or a Markdown
    link, and Telegram would silently reject the whole message. Splitting
    on blank-line boundaries keeps each chunk syntactically valid.

    Edge case: if a single paragraph is itself larger than max_length,
    it's emitted as its own oversized chunk (we don't try to slice inside
    it for the same Markdown-safety reason). For our brief this never
    triggers — the largest single section is the ticker table at ~800
    chars. Worth knowing if you grow the watchlist to ~50+ tickers.
    """
    if len(text) <= max_length:                          # fast path: most messages fit
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        # +2 accounts for the "\n\n" we'll re-insert when joining paragraphs
        if len(current) + len(paragraph) + 2 <= max_length:
            current += paragraph + "\n\n"
        else:
            if current:
                chunks.append(current.strip())
            current = paragraph + "\n\n"
    if current:
        chunks.append(current.strip())
    return chunks


def _html_to_plain(text):
    """Strip the small, fixed set of HTML we ever inject (<b>…</b>) and undo
    the entity escaping, recovering readable plain text. Used as the safety
    fallback when Telegram rejects an HTML message (see _send_one): the user
    still gets the content, just without bold. Unescape '&amp;' LAST so a
    literal '&lt;' in the source doesn't get double-decoded."""
    text = text.replace("<b>", "").replace("</b>", "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _post_message(message, parse_mode, chat_id):
    """Single Telegram sendMessage POST. Returns the requests.Response, or
    None on a transport-level failure (connection/timeout/DNS)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id if chat_id is not None else CHAT_ID,
        # defense in depth: splitter should already cap
        "text": message[:4096],
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        return requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:               # connection/timeout/DNS failures
        print(f"Telegram request failed: {type(e).__name__}: {e}")
        return None


def _send_one(message, parse_mode="Markdown", chat_id=None):
    """Internal: send a single, already-sized chunk.

    `chat_id` defaults to the env CHAT_ID — backward compatible with every
    existing call site. Broadcast loops in market_brief.py pass an explicit
    chat_id to target each subscriber in turn.

    `parse_mode` controls Telegram-side formatting:
      - "Markdown" (default): apply legacy V1 Markdown — *bold*, _italic_, etc.
      - "HTML":               apply HTML formatting — <b>…</b> etc. The daily
                              brief uses this (bold headers/tickers/levels).
      - None:                 send as PLAIN TEXT, no parsing. Use this when
                              forwarding content you don't control (like
                              an externally-written briefing file) where
                              stray '*' or '_' would otherwise reject the
                              whole message with a 400.

    HTML safety net: if an HTML send is rejected (malformed tag/entity in the
    rendered chunk), retry the SAME chunk once as plain text so the subscriber
    never silently misses the brief over a formatting glitch. The retry is
    per-chunk, so a good chunk is never re-sent (no duplicates)."""
    resp = _post_message(message, parse_mode, chat_id)
    if resp is None:
        return False
    if resp.ok:
        return True
    print(f"Telegram send failed: {resp.status_code} {resp.text}")
    # Bad HTML → fall back to plain text for this chunk only.
    if parse_mode == "HTML":
        print("  retrying chunk as plain text (HTML rejected).")
        retry = _post_message(_html_to_plain(message), None, chat_id)
        if retry is not None and retry.ok:
            return True
        if retry is not None:
            print(f"  plain-text retry also failed: {retry.status_code} {retry.text}")
    return False


def send_telegram(message, parse_mode="Markdown", chat_id=None):
    """Send `message` to a chat. Splits transparently across multiple
    Telegram messages if the input exceeds MAX_TELEGRAM_LENGTH.

    `chat_id` defaults to the env CHAT_ID — backward compatible. The
    broadcast loop in market_brief.main() passes one explicit chat_id
    per subscriber; bot.py's command replies leave it default to reply
    to whoever called the command (bot.py reads the from-chat off the
    incoming update and passes it through).

    Returns True ONLY if every chunk succeeded — a partial failure (e.g.
    chunk 2 of 3 fails) returns False so the caller can decide whether
    to retry.

    Splitting is two-pass: first honor any `[[SPLIT]]` markers Claude
    inserted (these are AUTHORITATIVE section boundaries), then run each
    marker-bounded chunk through the paragraph-based splitter as a safety
    net in case Claude produced a section that's still over the Telegram
    limit. Messages without markers go through the splitter directly, so
    the auto-generated daily message in market_brief.main() is unaffected.
    """
    overall_ok = True
    for marker_chunk in _split_on_markers(message):
        for chunk in _split_message(marker_chunk):
            if not _send_one(chunk, parse_mode, chat_id):
                overall_ok = False
    return overall_ok


# ----------------------------------------------------------------------------
# Language preferences + translation
# ----------------------------------------------------------------------------
# Adding a language is a one-line change to SUPPORTED_LANGUAGES — no other
# code needs to learn about it. `display` is shown back to the user by
# /language; `translate_code` is whatever the translation API expects in
# its target-language parameter (ISO-639-1 for Google Translate).
SUPPORTED_LANGUAGES = {
    "en": {"display": "English",  "translate_code": "en"},
    "ru": {"display": "Русский",  "translate_code": "ru"},
    # Google uses 'iw' for Hebrew
    "he": {"display": "עברית",    "translate_code": "iw"},
}

# Fallback when a subscriber's stored language code isn't in the registry
# (e.g. a language was removed) or when no preference has been recorded yet.
DEFAULT_LANGUAGE = "en"


def is_supported_language(code):
    """True if `code` is a key in SUPPORTED_LANGUAGES. Case-sensitive on
    purpose — the codes are short ISO strings, and bot.py lowercases user
    input before calling this."""
    return code in SUPPORTED_LANGUAGES


# ----------------------------------------------------------------------------
# Brief-length (format) preferences
# ----------------------------------------------------------------------------
# Mirrors the SUPPORTED_LANGUAGES pattern: adding a length is a one-line change
# here. `display` is shown back to the user by the /format command. The actual
# section-trimming for 'short' lives downstream in bot.py (it drops the heavier
# briefing sections); db.py stores the raw preference string per subscriber.
SUPPORTED_FORMATS = {
    "full":  {"display": "Full (all sections)"},
    "short": {"display": "Short (exec, macro, watchlist, trade ideas)"},
}

# Fallback when a subscriber's stored format isn't in the registry or when no
# preference has been recorded yet. 'full' preserves the original behavior.
DEFAULT_FORMAT = "full"


def is_supported_format(value):
    """True if `value` is a key in SUPPORTED_FORMATS. Case-sensitive; bot.py
    lowercases user input before calling this."""
    return value in SUPPORTED_FORMATS


# Google's unofficial web translate endpoint. No API key required; used by
# the Chrome translate extension and a hundred other tools. The dt=t param
# asks for the translation segments (vs detected language, alternatives, etc.).
# Free, but it's an undocumented endpoint — could change shape any time.
_GOOGLE_TRANSLATE_URL = (
    "https://translate.googleapis.com/translate_a/single"
    "?client=gtx&sl=en&tl={tl}&dt=t&q={q}"
)

# Google's endpoint truncates very long q params. Translate in chunks well
# under that limit so we don't have to think about the exact cutoff. The
# briefing is already paragraph-structured; we split on "\n\n" and translate
# each chunk independently, then re-join.
_TRANSLATE_CHUNK_CHARS = 2000


def _translate_chunk(text, target_code):
    """Translate one ≤_TRANSLATE_CHUNK_CHARS chunk via Google's web endpoint.
    Returns the translated string on success, raises on any failure (the
    caller decides whether to fall back or surface the error)."""
    url = _GOOGLE_TRANSLATE_URL.format(tl=target_code, q=quote(text))
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    # The response is a nested JSON array: [[[translated, original, ...], ...], ...].
    # First-level [0] is the list of segments, each [0] is the translated text.
    data = json.loads(resp.text)
    segments = data[0] or []
    return "".join((seg[0] or "") for seg in segments if seg)


def _split_for_translation(text, max_chars=_TRANSLATE_CHUNK_CHARS):
    """Break `text` into translate-API-sized chunks at paragraph boundaries.
    Mirrors core.py's _split_message logic but with a smaller cap — the
    translate endpoint is stricter than Telegram's 4096-char limit."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= max_chars:
            current += paragraph + "\n\n"
        else:
            if current:
                chunks.append(current.rstrip("\n"))
            current = paragraph + "\n\n"
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks


def translate_text(text, target_lang):
    """Translate English `text` into `target_lang` using a free public API.

    Returns (translated_text, None) on success and (None, error_reason) on
    failure. `target_lang` is a SUPPORTED_LANGUAGES key, NOT the API code —
    this function looks up the API code internally.

    Special case: target_lang == 'en' returns the input unchanged with no
    network call, so callers can blindly route every subscriber through
    this function regardless of language.
    """
    if target_lang == "en":
        return (text, None)
    lang = SUPPORTED_LANGUAGES.get(target_lang)
    if lang is None:
        return (None, f"unsupported language '{target_lang}'")
    api_code = lang["translate_code"]

    try:
        chunks = _split_for_translation(text)
        translated = []
        for chunk in chunks:
            translated.append(_translate_chunk(chunk, api_code))
        return ("\n\n".join(translated), None)
    except requests.RequestException as e:
        return (None, f"{type(e).__name__}: {e}")
    except (json.JSONDecodeError, IndexError, TypeError) as e:
        # Malformed response from the unofficial endpoint — treat as fail
        # so the caller can apply the English-fallback notice.
        return (None, f"unexpected response shape ({type(e).__name__})")
