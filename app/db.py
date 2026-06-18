"""
db.py
======
SQLite persistence layer for subscribers and their per-chat watchlists.

Replaces the old single-file `watchlist.json`. SQLite is in the stdlib, so
there's no new requirements entry — the only cost is one new file under
`secrets/`.

Two tables:
  subscribers — one row per Telegram chat that has /start'd the bot.
                Soft-deletable via `active=0` (so /stop preserves the
                history + watchlist for re-subscription).
  watchlists  — composite (chat_id, ticker) rows. Each chat has its own
                watchlist; no cross-chat sharing.

Connections are opened per call and closed via `with` so we never juggle
shared connection state across threads (bot.py is single-threaded today,
but market_brief.py and bot.py share this module without coordination).

Why no ORM: the surface area is six functions over two tables. An ORM
would be more code than the queries themselves, and SQLite's stdlib API
already handles parameter escaping correctly via `?` placeholders.
"""

import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


# DB lives alongside .env in secrets/ — already gitignored by the
# `secrets/` rule. Anchored to this file's path so behavior doesn't
# depend on the script's cwd.
DB_PATH = Path(__file__).resolve().parent.parent / "secrets" / "bot.db"

# JSON file we migrate FROM on first init(). After migration the JSON is
# left in place for one release as a backup; load_watchlist() never reads
# it again.
LEGACY_WATCHLIST_JSON = Path(__file__).resolve().parent / "watchlist.json"


def _connect():
    """Open a new connection with foreign-key enforcement turned ON.
    SQLite defaults FK enforcement to OFF, which would silently let an
    orphan watchlist row survive after its subscriber is deleted —
    not what the schema intends."""
    # Ensure parent dir exists. secrets/ already exists on this system,
    # but new deployments wouldn't have it before .env is created.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso():
    """UTC ISO-8601 timestamp string. SQLite has no native datetime
    type — TEXT is the recommended storage format and ISO-8601 sorts
    correctly as a string."""
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Schema + first-run migration
# ----------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
  chat_id    INTEGER PRIMARY KEY,
  username   TEXT,
  first_name TEXT,
  created_at TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  language   TEXT NOT NULL DEFAULT 'en',
  format     TEXT NOT NULL DEFAULT 'full'
);

CREATE TABLE IF NOT EXISTS watchlists (
  chat_id  INTEGER NOT NULL,
  ticker   TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, ticker),
  FOREIGN KEY (chat_id) REFERENCES subscribers(chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_watchlists_chat ON watchlists(chat_id);

CREATE TABLE IF NOT EXISTS access (
  chat_id      INTEGER PRIMARY KEY,
  status       TEXT NOT NULL,          -- 'pending' | 'approved' | 'denied'
  username     TEXT,
  first_name   TEXT,
  requested_at TEXT NOT NULL,
  decided_at   TEXT
);
"""


# Default language for subscribers who pre-date the language column or who
# never call /language. Kept here (not in core.py) so db.py stays import-
# free of higher layers — core.py also exports DEFAULT_LANGUAGE under the
# same name and the two must agree.
_DEFAULT_LANGUAGE = "en"

# Default brief length for subscribers who pre-date the `format` column or who
# never call /format. 'full' preserves the original behavior (whole briefing).
# core.py also exports DEFAULT_FORMAT under the same name and the two must agree.
_DEFAULT_FORMAT = "full"


def _migrate_add_language_column(conn):
    """Add `subscribers.language` if a pre-language DB is being opened.

    SQLite's ALTER TABLE doesn't support 'IF NOT EXISTS' on columns, so
    we probe via PRAGMA table_info first. Idempotent: safe to call on
    every init()."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(subscribers)")}
    if "language" not in cols:
        conn.execute(
            "ALTER TABLE subscribers ADD COLUMN language TEXT "
            f"NOT NULL DEFAULT '{_DEFAULT_LANGUAGE}'"
        )


def _migrate_add_format_column(conn):
    """Add `subscribers.format` if a pre-format DB is being opened. Same
    PRAGMA-probe approach as _migrate_add_language_column (SQLite can't do
    'ADD COLUMN IF NOT EXISTS'). Idempotent — safe on every init()."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(subscribers)")}
    if "format" not in cols:
        conn.execute(
            "ALTER TABLE subscribers ADD COLUMN format TEXT "
            f"NOT NULL DEFAULT '{_DEFAULT_FORMAT}'"
        )


def init():
    """Create tables (idempotent) and run the one-shot migration from
    watchlist.json. Safe to call on every module import; the migration
    no-ops if there's already data.

    Migration rules:
      1. If `subscribers` is empty AND TELEGRAM_CHAT_ID env is set,
         seed that chat as the first active subscriber. This preserves
         the existing single-user behavior — the admin keeps receiving
         the brief without needing to manually /start.
      2. If `watchlists` is empty AND the legacy watchlist.json exists,
         import every ticker into the env CHAT_ID's watchlist. Without
         this the admin's 18-ticker watchlist would silently vanish.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)

        # Step 0: migrate pre-language / pre-format DBs by adding the
        # columns. Runs before any reads/writes so the rest of init() can
        # rely on the columns existing.
        _migrate_add_language_column(conn)
        _migrate_add_format_column(conn)

        # Step 1: seed env CHAT_ID if subscribers is empty.
        cur = conn.execute("SELECT COUNT(*) FROM subscribers")
        if cur.fetchone()[0] == 0:
            env_chat = os.getenv("TELEGRAM_CHAT_ID")
            if env_chat:
                try:
                    env_chat_int = int(env_chat)
                except ValueError:
                    env_chat_int = None
                if env_chat_int is not None:
                    conn.execute(
                        "INSERT INTO subscribers"
                        " (chat_id, username, first_name, created_at, active)"
                        " VALUES (?, ?, ?, ?, 1)",
                        (env_chat_int, None, "admin (env bootstrap)", _now_iso()),
                    )

        # Step 2: import watchlist.json if watchlists table is empty.
        cur = conn.execute("SELECT COUNT(*) FROM watchlists")
        if cur.fetchone()[0] == 0 and LEGACY_WATCHLIST_JSON.exists():
            env_chat = os.getenv("TELEGRAM_CHAT_ID")
            try:
                env_chat_int = int(env_chat) if env_chat else None
            except ValueError:
                env_chat_int = None

            if env_chat_int is not None:
                try:
                    with open(LEGACY_WATCHLIST_JSON, "r") as f:
                        legacy = json.load(f)
                except (json.JSONDecodeError, OSError):
                    legacy = []
                if isinstance(legacy, list) and legacy:
                    now = _now_iso()
                    conn.executemany(
                        "INSERT OR IGNORE INTO watchlists"
                        " (chat_id, ticker, added_at) VALUES (?, ?, ?)",
                        [(env_chat_int, t, now) for t in legacy],
                    )


# ----------------------------------------------------------------------------
# Subscriber operations
# ----------------------------------------------------------------------------

def add_subscriber(chat_id, username=None, first_name=None):
    """Insert a new subscriber or reactivate an existing one. Returns True
    if this was a newly-added or newly-reactivated subscriber (i.e. the
    caller should send the welcome message + seed default watchlist),
    False if they were already active.

    The UPSERT keeps username/first_name fresh on every /start — useful
    if the user later changes their Telegram username, but only updates
    them when /start is sent."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)
        )
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO subscribers"
                " (chat_id, username, first_name, created_at, active)"
                " VALUES (?, ?, ?, ?, 1)",
                (chat_id, username, first_name, _now_iso()),
            )
            return True                                  # brand-new — seed default watchlist
        was_active = bool(row[0])
        conn.execute(
            "UPDATE subscribers SET username = ?, first_name = ?, active = 1"
            " WHERE chat_id = ?",
            (username, first_name, chat_id),
        )
        # Reactivation (was inactive) is treated like a new signup for the
        # welcome-message purpose, but DO NOT re-seed the watchlist — their
        # prior tickers survived in the watchlists table.
        return not was_active


def deactivate_subscriber(chat_id):
    """Soft delete via active=0. Watchlist rows are preserved (because the
    FK is ON DELETE CASCADE only triggers on actual row delete, not on a
    flag update). Returns True if a row actually changed state."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE subscribers SET active = 0 WHERE chat_id = ? AND active = 1",
            (chat_id,),
        )
        return cur.rowcount > 0


def is_subscriber(chat_id):
    """Gate for the non-/start commands. Inactive subscribers count as
    not-subscribed — they need to /start again to re-enable themselves."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM subscribers WHERE chat_id = ? AND active = 1",
            (chat_id,),
        )
        return cur.fetchone() is not None


def list_active_chat_ids():
    """Broadcast recipients. Returned as a list of ints, sorted ascending
    for deterministic send order (helps debugging when a run partially
    fails — you know which chat was next)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT chat_id FROM subscribers WHERE active = 1 ORDER BY chat_id"
        )
        return [row[0] for row in cur.fetchall()]


# ----------------------------------------------------------------------------
# Access control (admin-approval allowlist)
# ----------------------------------------------------------------------------
# The bot is private. `access` records each chat's authorization decision so the
# admin can approve/deny FROM Telegram without editing .env + restarting (the
# env ALLOWED_CHAT_IDS list in core.py still works and is checked first). States:
#   pending  — a non-allowlisted chat asked for access; awaiting the admin
#   approved — admin granted access; is_approved() lets them through the gate
#   denied   — admin rejected; stays silent so they can't re-spam the admin

def record_access_request(chat_id, username=None, first_name=None):
    """Log a /start (or any first message) from a non-allowlisted chat and
    return what should happen next:
      'new'      — first time we've seen this chat → admin should be notified
      'pending'  — already awaiting a decision → DON'T re-notify (anti-spam)
      'approved' — already approved (caller normally won't reach this)
      'denied'   — previously denied → stay silent, DON'T re-notify
    Contact info is refreshed every call so the admin sees the current handle,
    but an existing status is never changed here."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM access WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO access"
                " (chat_id, status, username, first_name, requested_at)"
                " VALUES (?, 'pending', ?, ?, ?)",
                (chat_id, username, first_name, _now_iso()),
            )
            return "new"
        conn.execute(
            "UPDATE access SET username = ?, first_name = ? WHERE chat_id = ?",
            (username, first_name, chat_id),
        )
        return row[0]


def is_approved(chat_id):
    """True if `chat_id` has been explicitly approved by the admin. Consulted by
    core.is_allowed_chat (OR'd with the static env allowlist)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM access WHERE chat_id = ? AND status = 'approved'",
            (chat_id,),
        ).fetchone() is not None


def set_access_status(chat_id, status, username=None, first_name=None):
    """Upsert an admin decision ('approved' / 'denied'). Stamps decided_at.
    Creates the row if the admin acts on a chat_id that never filed a request
    (e.g. adding someone proactively). Always returns True."""
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM access WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO access"
                " (chat_id, status, username, first_name, requested_at, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, status, username, first_name, _now_iso(), _now_iso()),
            )
        else:
            conn.execute(
                "UPDATE access SET status = ?, decided_at = ? WHERE chat_id = ?",
                (status, _now_iso(), chat_id),
            )
        return True


def get_access(chat_id):
    """Return (chat_id, status, username, first_name, requested_at, decided_at)
    for `chat_id`, or None if it has no access row."""
    with _connect() as conn:
        return conn.execute(
            "SELECT chat_id, status, username, first_name, requested_at, decided_at"
            " FROM access WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def list_pending():
    """Chats awaiting an access decision, oldest first. Returns
    [(chat_id, username, first_name, requested_at), ...]."""
    with _connect() as conn:
        return conn.execute(
            "SELECT chat_id, username, first_name, requested_at FROM access"
            " WHERE status = 'pending' ORDER BY requested_at"
        ).fetchall()


# ----------------------------------------------------------------------------
# Language preferences
# ----------------------------------------------------------------------------

def get_language(chat_id):
    """Return the language code for `chat_id`, falling back to the default
    when the chat has no row (e.g. lookups for an env-bootstrap chat that
    hasn't been migrated, or a chat that /stop'd and was hard-deleted)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT language FROM subscribers WHERE chat_id = ?", (chat_id,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else _DEFAULT_LANGUAGE


def set_language(chat_id, code):
    """Set `chat_id`'s language preference. Returns True if the update hit
    a row, False if the chat isn't a subscriber. Caller is responsible for
    validating `code` against the SUPPORTED_LANGUAGES registry in core.py —
    this function just writes whatever string it's given."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE subscribers SET language = ? WHERE chat_id = ?",
            (code, chat_id),
        )
        return cur.rowcount > 0


def list_active_subscribers_with_language():
    """Broadcast helper. Returns [(chat_id, language), ...] sorted by
    chat_id. Used by the bot.py briefing watcher to group recipients by
    language so each translation is computed once per language and reused
    across subscribers."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT chat_id, language FROM subscribers"
            " WHERE active = 1 ORDER BY chat_id"
        )
        return [(row[0], row[1] or _DEFAULT_LANGUAGE) for row in cur.fetchall()]


# ----------------------------------------------------------------------------
# Brief-length (format) preferences
# ----------------------------------------------------------------------------

def get_format(chat_id):
    """Return the brief-length preference ('full' / 'short') for `chat_id`,
    falling back to the default when the chat has no row or a NULL value."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT format FROM subscribers WHERE chat_id = ?", (chat_id,)
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else _DEFAULT_FORMAT


def set_format(chat_id, value):
    """Set `chat_id`'s brief-length preference. Returns True if the update hit
    a row, False if the chat isn't a subscriber. Caller validates `value`
    against SUPPORTED_FORMATS in core.py — this just writes what it's given."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE subscribers SET format = ? WHERE chat_id = ?",
            (value, chat_id),
        )
        return cur.rowcount > 0


def list_active_subscribers_with_prefs():
    """Broadcast helper. Returns [(chat_id, language, format), ...] sorted by
    chat_id. Superset of list_active_subscribers_with_language() — the bot.py
    watcher groups by language for translation caching and uses `format` to
    decide whether to trim the briefing to the short section set per user."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT chat_id, language, format FROM subscribers"
            " WHERE active = 1 ORDER BY chat_id"
        )
        return [
            (row[0], row[1] or _DEFAULT_LANGUAGE, row[2] or _DEFAULT_FORMAT)
            for row in cur.fetchall()
        ]


# ----------------------------------------------------------------------------
# Watchlist operations
# ----------------------------------------------------------------------------

def load_watchlist(chat_id):
    """Return the chat's tickers sorted ascending. Empty list if the chat
    has no rows yet (brand-new subscriber pre-seed, or an admin chat that
    never had anything imported)."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT ticker FROM watchlists WHERE chat_id = ? ORDER BY ticker",
            (chat_id,),
        )
        return [row[0] for row in cur.fetchall()]


def union_active_watchlists():
    """Deduped union of every active subscriber's tickers, sorted ascending
    for deterministic output. Returns [] if no active subscribers exist or
    none of them have any tickers — callers fall back to admin-only behavior.

    Powers market_brief.collect(): one data-collection pass covers every
    ticker any user cares about, and the per-ticker caches in market_brief.py
    keep duplicate fetches free across overlapping watchlists."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT DISTINCT w.ticker FROM watchlists w"
            " JOIN subscribers s ON s.chat_id = w.chat_id"
            " WHERE s.active = 1"
            " ORDER BY w.ticker"
        )
        return [row[0] for row in cur.fetchall()]


def add_watchlist_tickers(chat_id, tickers):
    """Bulk-insert tickers for `chat_id`. Returns (added, duplicates) so
    the caller can format a per-bucket reply ("Added: X, Y / Skipped: Z").

    Tickers are assumed to be already normalized by core.normalize_ticker
    upstream — this function trusts its input and won't re-validate."""
    if not tickers:
        return ([], [])
    now = _now_iso()
    added, duplicates = [], []
    with _connect() as conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT ticker FROM watchlists WHERE chat_id = ?",
                (chat_id,),
            )
        }
        rows_to_insert = []
        for t in tickers:
            if t in existing:
                duplicates.append(t)
            else:
                rows_to_insert.append((chat_id, t, now))
                added.append(t)
                # Track in-memory so duplicates within the same call get
                # reported correctly (caller may pass ['NVDA', 'NVDA']).
                existing.add(t)
        if rows_to_insert:
            conn.executemany(
                "INSERT INTO watchlists (chat_id, ticker, added_at)"
                " VALUES (?, ?, ?)",
                rows_to_insert,
            )
    return (added, duplicates)


def remove_watchlist_tickers(chat_id, tickers):
    """Bulk-delete tickers for `chat_id`. Returns (removed, missing) for
    the same reply-formatting reason."""
    if not tickers:
        return ([], [])
    removed, missing = [], []
    with _connect() as conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT ticker FROM watchlists WHERE chat_id = ?",
                (chat_id,),
            )
        }
        to_delete = []
        for t in tickers:
            if t in existing:
                to_delete.append((chat_id, t))
                removed.append(t)
                # Same in-call tracking as add_watchlist_tickers — if the
                # caller passes ['NVDA', 'NVDA'], second one is "missing".
                existing.discard(t)
            else:
                missing.append(t)
        if to_delete:
            conn.executemany(
                "DELETE FROM watchlists WHERE chat_id = ? AND ticker = ?",
                to_delete,
            )
    return (removed, missing)
