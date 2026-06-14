"""
scorecard.py
============
Closes the loop on the 🎯 TRADE IDEAS section: logs each day's ideas, grades
their outcomes against real price action, and reports a weekly scorecard to the
admin.

Subcommands:
  ingest [BRIEF] [DATA]  — parse final_briefing.md's trade cards and record them
                           in trades_db. LIGHT (no yfinance). Idempotent; safe
                           to run inline after generation.
  score                  — grade every still-open idea against subsequent daily
                           bars (yfinance), update trades_db, and send a weekly
                           summary to the env CHAT_ID admin. Needs no Ollama, so
                           it runs even when the model is down.

Grading model (simplifications, documented honestly):
  - The idea is assumed FILLED at its stated Entry on the briefing day; we grade
    from brief_date forward. (We don't model whether price actually reached the
    entry first.)
  - Daily bars only: if a session's range touches BOTH stop and target we can't
    know which came first, so we resolve conservatively as 'stopped'.
"""

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import trades_db
import validate_briefing
from core import CHAT_ID, send_telegram

APP_DIR = Path(__file__).parent
DEFAULT_BRIEFING = APP_DIR / "final_briefing.md"
DEFAULT_MARKET_DATA = APP_DIR / "market_data.md"

# Holding window (trading days) when a card's timeframe is missing/unparseable.
_DEFAULT_WINDOW_DAYS = 10
# Lookback for the weekly summary.
_SCORECARD_LOOKBACK_DAYS = 7


def _today():
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# ingest (light)
# ---------------------------------------------------------------------------

def ingest(briefing_path=DEFAULT_BRIEFING, market_path=DEFAULT_MARKET_DATA,
           brief_date=None):
    """Parse the briefing's trade cards and record them. Returns the number of
    NEW ideas inserted. Never raises on missing files (returns 0) so the
    generate_briefing hook stays safe."""
    try:
        briefing = Path(briefing_path).read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        market_data = Path(market_path).read_text(encoding="utf-8")
    except OSError:
        market_data = ""

    level_tickers = set(validate_briefing.parse_key_levels(market_data))
    cards = validate_briefing.parse_trade_cards(briefing, level_tickers)
    if not cards:
        return 0

    trades_db.init()
    return trades_db.record_ideas(brief_date or _today().isoformat(), cards)


# ---------------------------------------------------------------------------
# score (heavy — yfinance)
# ---------------------------------------------------------------------------

def _window_days(timeframe):
    """Trading-day holding window from a card's timeframe string ('intraday',
    'swing 3-5d', '5d', …). Uses the upper bound of a range."""
    if not timeframe:
        return _DEFAULT_WINDOW_DAYS
    tf = timeframe.lower()
    if "intraday" in tf:
        return 1
    rng = re.search(r"(\d+)\s*-\s*(\d+)\s*d", tf)
    if rng:
        return int(rng.group(2))
    single = re.search(r"(\d+)\s*d", tf)
    if single:
        return int(single.group(1))
    return _DEFAULT_WINDOW_DAYS


def _return_pct(entry, exit_price, direction):
    """Signed P/L percent for the direction (short profits when price falls)."""
    if not entry:
        return None
    raw = (exit_price - entry) / entry * 100.0
    return raw if direction == "long" else -raw


def grade_idea(idea, bars):
    """Decide an open idea's outcome from its daily `bars` (list of
    (date, high, low, close) from brief_date forward).

    Returns (status, outcome_price, outcome_date, return_pct), or None when
    there aren't enough sessions yet to resolve (idea stays open).
    """
    entry, stop, target = idea["entry"], idea["stop"], idea["target"]
    direction = idea["direction"]
    if not bars or entry is None or stop is None or target is None:
        return None

    window = _window_days(idea["timeframe"])
    horizon = bars[:window + 1]  # brief-day session + the window after it
    for date, high, low, _close in horizon:
        if direction == "long":
            stopped, hit = low <= stop, high >= target
        else:
            stopped, hit = high >= stop, low <= target
        if stopped:  # conservative: stop wins same-session ties
            return ("stopped", stop, date, _return_pct(entry, stop, direction))
        if hit:
            return ("hit", target, date, _return_pct(entry, target, direction))

    # Neither hit nor stopped. Expire only once the full window has elapsed.
    if len(bars) > window:
        date, _high, _low, close = horizon[-1]
        return ("expired", close, date, _return_pct(entry, close, direction))
    return None


def _fetch_bars(yf, ticker, brief_date):
    """Daily (date, high, low, close) tuples from brief_date forward, [] on error."""
    try:
        start = datetime.fromisoformat(brief_date).date().isoformat()
    except ValueError:
        return []
    try:
        data = yf.Ticker(ticker).history(start=start)
    except Exception:
        return []
    bars = []
    for idx, row in data.iterrows():
        try:
            bars.append((
                idx.date().isoformat(),
                float(row["High"]), float(row["Low"]), float(row["Close"]),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return bars


def score():
    """Grade all open ideas, persist outcomes, and ALWAYS send a dated
    scorecard — even on an empty week or a grading failure — so a scheduled
    run is always confirmable in Telegram. Returns the number newly resolved."""
    resolved = 0
    grading_error = None
    try:
        import yfinance as yf  # lazy: keeps the ingest path free of the heavy import
        trades_db.init()
        for idea in trades_db.open_ideas():
            try:
                bars = _fetch_bars(yf, idea["ticker"], idea["brief_date"])
                outcome = grade_idea(idea, bars)
                if outcome is None:
                    continue
                status, price, date, ret = outcome
                trades_db.mark_outcome(idea["id"], status, price, date, ret)
                resolved += 1
            except Exception as e:  # one bad ticker must not abort the whole run
                print(f"[scorecard] {idea['ticker']} grading failed: {type(e).__name__}: {e}")
    except Exception as e:  # whole grading phase failed (import/db) — still report
        grading_error = f"{type(e).__name__}: {e}"
        print(f"[scorecard] grading phase failed: {grading_error}")

    _send_scorecard(resolved=resolved, error=grading_error)
    return resolved


def _format_scorecard(summary, since, resolved=0, error=None):
    """Plain-text weekly summary (admin DM — no markdown). ALWAYS returns a
    dated, self-confirming message: an empty week or a grading error still
    produces a clear "the scheduled job ran" signal you can use to verify
    Task Scheduler is wired up correctly."""
    ran = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📊 WEEKLY TRADE SCORECARD — ran {ran}"]

    if error:
        lines.append(f"⚠️ Ran, but grading hit an error: {error}")

    if not summary or summary["total"] == 0:
        lines.append(f"No tracked ideas since {since} — nothing to grade yet.")
        lines.append("(This message confirms the scheduled job ran.)")
        return "\n".join(lines)

    win = f"{summary['win_rate']:.0f}%" if summary["win_rate"] is not None else "—"
    avg = f"{summary['avg_return']:+.1f}%" if summary["avg_return"] is not None else "—"
    lines.append(
        f"Resolved {summary['resolved']}/{summary['total']} · "
        f"win rate {win} · avg {avg} · {summary['open']} still open "
        f"({resolved} newly graded this run)"
    )
    lines.append("")
    for row in summary["rows"]:
        ret = f"{row['return_pct']:+.1f}%" if row["return_pct"] is not None else "—"
        lines.append(
            f"{row['brief_date']} {row['ticker']} {row['direction'].upper()} "
            f"-> {row['status']} {ret}"
        )
    return "\n".join(lines)


def _safe_print(text):
    """print() that won't crash on a non-UTF-8 console (Windows cp1252). The
    scorecard text leads with a 📊 emoji which would otherwise raise
    UnicodeEncodeError and abort the run AFTER the Telegram send already
    happened — making a working send look like a failure."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _send_scorecard(resolved=0, error=None):
    since = (_today() - timedelta(days=_SCORECARD_LOOKBACK_DAYS)).isoformat()
    try:
        summary = trades_db.summary_since(since)
    except Exception as e:  # pylint: disable=broad-exception-caught
        summary = None
        error = error or f"could not read trades.db: {type(e).__name__}: {e}"
    msg = _format_scorecard(summary, since, resolved=resolved, error=error)
    sent = send_telegram(msg, chat_id=CHAT_ID) if CHAT_ID else None
    _safe_print(msg)
    if not CHAT_ID:
        _safe_print("[scorecard] CHAT_ID not set — printed only, nothing sent.")
    else:
        _safe_print(f"[scorecard] Telegram delivery: {'OK' if sent else 'FAILED'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "score"
    if cmd == "ingest":
        brief = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BRIEFING
        data = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_MARKET_DATA
        print(f"[scorecard] ingested {ingest(brief, data)} new idea(s)")
        return 0
    if cmd == "score":
        print(f"[scorecard] resolved {score()} idea(s)")
        return 0
    print("Usage: python scorecard.py [ingest|score]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
