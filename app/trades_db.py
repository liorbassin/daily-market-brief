"""
trades_db.py
============
SQLite persistence for the trade ideas emitted in the daily 🎯 TRADE IDEAS
section, plus their graded outcomes.

DELIBERATELY a separate module AND a separate database file from db.py: db.py
holds the bot's operational state (subscribers, watchlists) in secrets/bot.db,
while this is an analytics ledger in secrets/trades.db. Keeping them apart means
the tracking experiment can't corrupt the subscriber store, and either can be
wiped independently.

One table, `ideas`:
  - APPEND-ONLY at ingest time. INSERT OR IGNORE keyed on
    UNIQUE(brief_date, ticker, direction) makes re-ingesting the same day a
    no-op, so the daily hook is safe to run more than once.
  - UPDATED IN PLACE by the weekly scorecard as outcomes resolve (open -> hit /
    stopped / expired).

Connections are opened per call and closed via `with`, mirroring db.py — no
shared connection state across threads/processes.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# Lives alongside bot.db under secrets/ (already gitignored). Anchored to this
# file so behavior doesn't depend on cwd, same as db.py.
DB_PATH = Path(__file__).resolve().parent.parent / "secrets" / "trades.db"


def _connect():
    """New connection. secrets/ is created if missing (fresh deployments)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _now_iso():
    """UTC ISO-8601 timestamp — TEXT storage, sorts correctly as a string."""
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  brief_date    TEXT NOT NULL,                 -- YYYY-MM-DD of the briefing
  ticker        TEXT NOT NULL,
  direction     TEXT NOT NULL,                 -- long / short / avoid
  conviction    TEXT,                          -- low / medium / high
  timeframe     TEXT,                          -- intraday / swing Nd
  entry         REAL,
  stop          REAL,
  target        REAL,
  thesis        TEXT,
  in_watchlist  INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'open',  -- open/hit/stopped/expired/skipped
  outcome_price REAL,
  outcome_date  TEXT,
  return_pct    REAL,
  created_at    TEXT NOT NULL,
  evaluated_at  TEXT,
  UNIQUE(brief_date, ticker, direction)
);
CREATE INDEX IF NOT EXISTS ix_ideas_status ON ideas(status);
CREATE INDEX IF NOT EXISTS ix_ideas_date ON ideas(brief_date);
"""


def init():
    """Create the table (idempotent). Safe to call on every import/run."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def record_ideas(brief_date, cards):
    """Insert the day's ideas. `cards` is a list of dicts from
    validate_briefing.parse_trade_cards(). Returns the number of NEW rows.

    Idempotent: UNIQUE(brief_date,ticker,direction) + INSERT OR IGNORE means
    re-running for the same brief_date inserts nothing.

    Status at ingest:
      - 'skipped' for AVOID ideas and for any card missing a full
        entry/stop/target triple (nothing to grade).
      - 'open' otherwise (the scorecard will resolve it later).
    """
    if not cards:
        return 0
    now = _now_iso()
    rows = []
    for c in cards:
        direction = (c.get("direction") or "").lower()
        has_levels = (
            c.get("entry") is not None
            and c.get("stop") is not None
            and c.get("target") is not None
        )
        status = "open" if (direction in ("long", "short") and has_levels) else "skipped"
        rows.append((
            brief_date,
            c.get("ticker"),
            direction or "?",
            c.get("conviction"),
            c.get("timeframe"),
            c.get("entry"),
            c.get("stop"),
            c.get("target"),
            c.get("thesis"),
            1 if c.get("in_watchlist") else 0,
            status,
            now,
        ))
    with _connect() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO ideas"
            " (brief_date, ticker, direction, conviction, timeframe,"
            "  entry, stop, target, thesis, in_watchlist, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return cur.rowcount


def open_ideas():
    """Ideas still awaiting an outcome. Returns a list of dicts (one per row)
    with the fields the scorecard needs to grade them."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, brief_date, ticker, direction, entry, stop, target, timeframe"
            " FROM ideas WHERE status = 'open' ORDER BY brief_date, ticker"
        )
        cols = ("id", "brief_date", "ticker", "direction",
                "entry", "stop", "target", "timeframe")
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_outcome(idea_id, status, outcome_price, outcome_date, return_pct):
    """Resolve one idea: set its terminal status, exit price/date, and the
    realized return percent."""
    with _connect() as conn:
        conn.execute(
            "UPDATE ideas SET status = ?, outcome_price = ?, outcome_date = ?,"
            " return_pct = ?, evaluated_at = ? WHERE id = ?",
            (status, outcome_price, outcome_date, return_pct, _now_iso(), idea_id),
        )


def summary_since(since_date):
    """Aggregate ideas with brief_date >= since_date (excluding 'skipped') for
    the weekly scorecard. Returns counts, win rate over resolved ideas, average
    realized return, and the raw rows for a detail list."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT brief_date, ticker, direction, status, return_pct,"
            " entry, target, stop FROM ideas"
            " WHERE brief_date >= ? AND status != 'skipped'"
            " ORDER BY brief_date, ticker",
            (since_date,),
        )
        cols = ("brief_date", "ticker", "direction", "status", "return_pct",
                "entry", "target", "stop")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    terminal = {"hit", "stopped", "expired"}
    resolved = [r for r in rows if r["status"] in terminal]
    wins = [r for r in resolved if r["status"] == "hit"]
    returns = [r["return_pct"] for r in resolved if r["return_pct"] is not None]
    return {
        "rows": rows,
        "total": len(rows),
        "open": sum(1 for r in rows if r["status"] == "open"),
        "resolved": len(resolved),
        "wins": len(wins),
        "win_rate": (len(wins) / len(resolved) * 100) if resolved else None,
        "avg_return": (sum(returns) / len(returns)) if returns else None,
    }
