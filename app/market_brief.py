"""
market_brief.py
================
ONE-SHOT script. Builds a daily market brief and sends it to Telegram, then exits.

This is what your scheduler (Task Scheduler / cron) runs once per day.
It is NOT the bot — the always-on Telegram listener lives in bot.py.

Run modes:
  python market_brief.py            # build brief and send it (default daily run)
  python market_brief.py collect    # write the brief to market_data.md instead
  python market_brief.py send FILE  # send an arbitrary file's contents to Telegram

Three-file architecture (see core.py for the shared plumbing):
  - core.py          shared helpers (env, watchlist, send_telegram, escape_md)
  - market_brief.py  THIS file: data fetching + the daily brief entry point
  - bot.py           always-on Telegram listener + briefing forwarder
"""

# NEW: regex for $TICKER extraction in WSB titles
import re
import sys
import time                                  # throttle multi-expiration Tradier calls
import json                                  # options_history.json cache
# NEW: pulls the 2Y yield from FRED's CSV endpoint
import requests
# market data source for the ticker dashboard
import yfinance as yf
# RSS parser for Yahoo Finance + Reddit scans
import feedparser
from pathlib import Path                     # cache file path
from datetime import datetime, timezone, timedelta  # timestamps + catalyst windows
# America/New_York for pre-market cutoffs (Py3.9+)
from zoneinfo import ZoneInfo

# Helpers shared with bot.py live in core.py — see that file for the
# watchlist persistence, env-var checks, escape_md, and send_telegram.
from core import (
    CHAT_ID,
    # powers earnings + per-ticker catalysts
    FMP_API_KEY,
    # powers economic calendar (release dates)
    FRED_API_KEY,
    REDDIT_USER_AGENT,
    # powers per-watchlist put/call ratios (optional)
    TRADIER_TOKEN,
    check_required_env,
    escape_md,
    load_watchlist,
    send_telegram,
)

# Subscriber list + chat watchlists live in db.py.
import db


# ============================================================================
# NEW: small formatting helpers used by the data-fetching sections below.
# Both are private (leading underscore) — they're implementation details of
# this file and aren't meant to be imported elsewhere.
# ============================================================================

def _format_volume(v):
    """Compact volume formatting: 1_234_567 -> '1.2M'.

    Why bother: a raw integer like 'Volume: 1234567' is noise; a glance-able
    'Vol: 1.2M' is what an analyst actually scans for."""
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(int(v))


# Cashtag pattern: literal '$' followed by 1-5 uppercase letters, then a
# word boundary. Cashtags are the WSB convention for naming a specific
# stock ('$NVDA', '$TSLA'). Requiring the leading '$' rules out false
# positives like "THE", "GO", "ALL" that a naked uppercase regex would catch.
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")


def _extract_tickers(text):
    """Find $TICKER cashtags in a string. Returns a deduped list, in order
    of first appearance. Used to surface WHICH names a WSB post is hyping
    rather than just leaving the title as opaque sentiment."""
    seen = []
    for m in _CASHTAG_RE.finditer(text):
        sym = m.group(1)
        if sym not in seen:                            # cheap dedupe, preserves order
            seen.append(sym)
    return seen


# ============================================================================
# Pre-market helper — used by both the PRE column in the watchlist
# dashboard AND by the (rewritten) pre-market movers section. Factored out
# so the same ticker doesn't trigger two near-identical yfinance fetches.
# ============================================================================

# Cache pre-market quotes for the lifetime of one script run. Both call
# sites query the same watchlist, so the second caller hits this dict
# instead of yfinance. Also reused across subscribers in the personalized
# broadcast loop — overlapping watchlists trigger one yfinance call per
# unique ticker regardless of how many subscribers want it.
_PREMARKET_CACHE = {}

# Sibling per-run caches for the other two expensive per-ticker fetches
# (catalysts via FMP + Yahoo, options chain via Tradier/yfinance). Wrapped
# around _fetch_catalysts_for and _fetch_chain so the cache logic lives
# in one place instead of bleeding into the section builders. A None
# value means "we tried and failed" — distinguishes 'no chain' from
# 'cache miss' so we don't retry failing fetches inside one run.
_CATALYST_CACHE = {}
_CHAIN_CACHE = {}
# Per-run cache for get_key_levels' 1y-bar fetch + derived support/resistance.
_LEVELS_CACHE = {}


def reset_run_caches():
    """Clear the per-run caches. Called by main() at the start of a
    broadcast so a long-lived process (currently only bot.py, but a future
    daemonized brief would benefit too) doesn't serve stale data from
    yesterday's run."""
    _PREMARKET_CACHE.clear()
    _CATALYST_CACHE.clear()
    _CHAIN_CACHE.clear()
    _LEVELS_CACHE.clear()


# US market opens at 9:30 ET. Anything before that timestamp on the latest
# trading day is "pre-market" for our purposes (regular hours start at 9:30).
_NY = ZoneInfo("America/New_York")


def _premarket_pct(ticker, prev_close=None):
    """Return (pre_market_price, pct_vs_prev_close) for `ticker`, or
    (None, None) if no pre-market bar is available (weekends, mid-session,
    market closed for holiday, or yfinance just doesn't have the data).

    Uses one yfinance minute-bar fetch with prepost=True over 2 days and
    picks the latest bar with a timestamp before 9:30 ET on the most recent
    session. `prev_close` is the previous regular-session close; if None,
    we derive it from a `period='5d'` daily fetch as a fallback (extra HTTP
    call, so callers should pass it in when they already have it).
    """
    if ticker in _PREMARKET_CACHE:
        return _PREMARKET_CACHE[ticker]

    try:
        bars = yf.Ticker(ticker).history(
            period="2d", interval="1m", prepost=True
        )
        if bars.empty:
            _PREMARKET_CACHE[ticker] = (None, None)
            return (None, None)

        # yfinance returns a tz-aware index when prepost=True. Convert to
        # ET so the 9:30 cutoff is unambiguous across DST transitions.
        idx_ny = bars.index.tz_convert(_NY)

        # Find the most recent session date present. "Session date" = the
        # date portion of the latest timestamp, since pre-market bars are
        # part of that day's session.
        latest_date = idx_ny[-1].date()
        cutoff = datetime.combine(
            latest_date, datetime.min.time(), tzinfo=_NY
        ).replace(hour=9, minute=30)

        # Pre-market bars: same session date, timestamp strictly before 9:30 ET.
        mask = (idx_ny.date == latest_date) & (idx_ny < cutoff)
        pre_bars = bars[mask]
        if pre_bars.empty:
            _PREMARKET_CACHE[ticker] = (None, None)
            return (None, None)

        pre_price = float(pre_bars["Close"].iloc[-1])

        if prev_close is None:
            daily = yf.Ticker(ticker).history(period="5d")
            if len(daily) < 1:
                _PREMARKET_CACHE[ticker] = (None, None)
                return (None, None)
            # If the latest daily bar IS the current session, use the
            # second-to-last close as the prior close; else the last.
            if daily.index[-1].date() == latest_date and len(daily) >= 2:
                prev_close = float(daily["Close"].iloc[-2])
            else:
                prev_close = float(daily["Close"].iloc[-1])

        if not prev_close:                              # defensive: avoid /0
            _PREMARKET_CACHE[ticker] = (None, None)
            return (None, None)

        pct = (pre_price / prev_close - 1) * 100
        result = (pre_price, pct)
        _PREMARKET_CACHE[ticker] = result
        return result
    except Exception:
        _PREMARKET_CACHE[ticker] = (None, None)
        return (None, None)


# ============================================================================
# Per-ticker catalysts — pulls analyst grades, 8-Ks, press releases (FMP)
# with a Yahoo per-ticker news fallback. Produces TWO outputs in one pass:
#   1. A tag map {ticker: short_tag} consumed by get_tickers() so the
#      dashboard can show a CAT column without re-fetching anything.
#   2. A grouped markdown block listing the actual catalysts so the
#      analyst prompt has the underlying headlines + links to reason over.
# ============================================================================

# A catalyst is "fresh" if dated within this many calendar days. 4 days
# covers the weekend gap (a Friday-evening upgrade still drives Monday's
# open). Tighter than that misses Friday catalysts on a Monday brief;
# looser than that starts including stale news that doesn't explain
# today's move.
_CATALYST_WINDOW_DAYS = 4

# Tag priority — used both to pick a single dashboard tag when a ticker
# has multiple catalyst types AND to sort sources in the section output.
# Order matters: an upgrade is the strongest signal we surface, a
# generic news headline is the weakest. "—" is the fallback for tickers
# with no fresh catalysts (so the CAT column is never empty).
_TAG_PRIORITY = ("UPG", "DOWN", "8-K", "PR", "NEWS")


def _within_catalyst_window(date_str):
    """True if a YYYY-MM-DD (or ISO datetime prefix) is within the window.
    FMP returns dates in several shapes — 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS',
    or full ISO with 'T' — so we slice the first 10 chars and parse that.
    Returns False on missing/malformed input rather than raising."""
    if not date_str:
        return False
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now().date() - d).days <= _CATALYST_WINDOW_DAYS


def _fmp_get(path):
    """Single-source error handling for FMP fetches. Returns [] on any
    failure (no key, network error, non-2xx, malformed JSON) so callers
    can treat 'no data' and 'fetch failed' identically — the section
    degrades to 'no catalysts' rather than crashing the brief."""
    if not FMP_API_KEY:
        return []
    sep = "&" if "?" in path else "?"
    url = f"https://financialmodelingprep.com/{path}{sep}apikey={FMP_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _classify_grade(item):
    """Map an FMP grade entry to one of 'UPG' / 'DOWN' / 'PR' (neutral
    action like 'reiterate' falls back to PR-equivalent priority).

    FMP populates these fields inconsistently across vendors — sometimes
    `action` says 'upgrade'/'downgrade'; sometimes only `newGrade` and
    `previousGrade` are populated. We try `action` first (most reliable),
    fall back to a coarse string-match on the grade pair."""
    action = (item.get("action") or "").lower()
    if "upgrade" in action:
        return "UPG"
    if "downgrade" in action:
        return "DOWN"

    new_g = (item.get("newGrade") or "").lower()
    prev_g = (item.get("previousGrade") or "").lower()
    bullish_terms = ("buy", "outperform", "overweight",
                     "positive", "accumulate")
    bearish_terms = ("sell", "underperform",
                     "underweight", "negative", "reduce")
    new_bull = any(t in new_g for t in bullish_terms)
    new_bear = any(t in new_g for t in bearish_terms)
    prev_bull = any(t in prev_g for t in bullish_terms)
    prev_bear = any(t in prev_g for t in bearish_terms)

    if new_bull and not prev_bull:
        return "UPG"
    if new_bear and not prev_bear:
        return "DOWN"
    # Reiteration / initiation / unparseable — treat as a non-directional
    # rating signal, lower priority than 8-K but higher than nothing.
    return "PR"


def _fetch_catalysts_for(ticker):
    """Cache-backed wrapper around the actual fetch. First caller for a
    ticker pays the network cost; subsequent callers (later subscribers
    in the broadcast loop) hit _CATALYST_CACHE."""
    if ticker in _CATALYST_CACHE:
        return _CATALYST_CACHE[ticker]
    out = _fetch_catalysts_for_uncached(ticker)
    _CATALYST_CACHE[ticker] = out
    return out


def _fetch_catalysts_for_uncached(ticker):
    """Actual implementation. Returns a list of catalyst dicts, each shaped:
        {'tag': 'UPG'|'DOWN'|'8-K'|'PR'|'NEWS',
         'date': 'YYYY-MM-DD',
         'source': 'GRADE'|'8-K'|'PR'|'NEWS',
         'headline': '...',
         'link': '...'}

    Order matches FMP-priority sources first (grades, then 8-Ks, then
    press releases). Yahoo Finance per-ticker news is ONLY used when all
    three FMP sources came back empty — it's a noisier source and we
    don't want to drown the real catalysts in random Yahoo headlines."""
    out = []

    # 1. Analyst grades — highest signal.
    for it in _fmp_get(f"stable/grades?symbol={ticker}"):
        if not _within_catalyst_window(it.get("date")):
            continue
        tag = _classify_grade(it)
        firm = it.get("gradingCompany") or "?"
        prev_g = (it.get("previousGrade") or "").strip() or "—"
        new_g = (it.get("newGrade") or "").strip() or "—"
        out.append({
            "tag": tag,
            "date": str(it.get("date", ""))[:10],
            "source": "GRADE",
            "headline": f"{firm}: {prev_g} → {new_g}",
            "link": "",                                  # FMP grades have no link
        })

    # 2. SEC 8-K filings — material corporate events (guidance updates,
    # M&A, executive changes, etc.). The /stable/sec-filings-search/symbol
    # endpoint REQUIRES from/to date params (otherwise returns 400 "Invalid
    # or missing query parameter - from"). We send a window slightly wider
    # than _CATALYST_WINDOW_DAYS so timezone slop / weekend filings don't
    # fall just outside the API request range.
    today_iso = datetime.now().strftime("%Y-%m-%d")
    since_iso = (
        datetime.now() - timedelta(days=_CATALYST_WINDOW_DAYS + 2)
    ).strftime("%Y-%m-%d")
    for it in _fmp_get(
        f"stable/sec-filings-search/symbol?symbol={ticker}"
        f"&from={since_iso}&to={today_iso}&limit=10"
    ):
        ftype = (it.get("type") or it.get("formType") or "").upper()
        if "8-K" not in ftype:
            continue
        filed_at = it.get("filingDate") or it.get(
            "fillingDate") or it.get("date")
        if not _within_catalyst_window(filed_at):
            continue
        link = it.get("finalLink") or it.get("link") or ""
        out.append({
            "tag": "8-K",
            "date": str(filed_at)[:10],
            "source": "8-K",
            "headline": "8-K filing",
            "link": link,
        })

    # NOTE: press releases (stable/news/press-releases) and stock news
    # (stable/news/stock) are paid-tier-only on FMP and return 402 on the
    # free plan. Per-ticker news for free-tier users comes from the Yahoo
    # fallback below instead.

    # 3. Yahoo per-ticker news — ONLY as a fallback when no FMP catalyst
    # was found. yfinance's `.news` schema has shifted across versions;
    # probe a few key names defensively. (Always running Yahoo would
    # drown the strong FMP signals in headline noise.)
    if not out:
        try:
            news = yf.Ticker(ticker).news or []
        except Exception:
            news = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for n in news[:5]:
            # yfinance 0.2.40+ nests under .content
            content = n.get("content") or n
            ts = (
                content.get("providerPublishTime")
                or content.get("pubDate")
                or n.get("providerPublishTime")
            )
            try:
                if isinstance(ts, (int, float)):
                    when = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    when = datetime.fromisoformat(
                        str(ts).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if when < cutoff:
                continue
            title = (
                content.get("title")
                or n.get("title")
                or ""
            ).strip() or "(no title)"
            link = (
                (content.get("canonicalUrl") or {}).get("url")
                or content.get("link")
                or n.get("link")
                or ""
            )
            out.append({
                "tag": "NEWS",
                "date": when.date().isoformat(),
                "source": "NEWS",
                "headline": title,
                "link": link,
            })

    return out


def _best_tag(catalysts):
    """Pick the highest-priority tag from a ticker's catalyst list for
    the dashboard column. Empty list → em-dash placeholder."""
    if not catalysts:
        return "—"
    tags = {c["tag"] for c in catalysts}
    for t in _TAG_PRIORITY:
        if t in tags:
            return t
    return "—"


def get_per_ticker_catalysts(watchlist):
    """Build the per-ticker catalysts section AND the tag map in one pass
    over `watchlist`.

    Returns a tuple `(section_markdown, tag_map)`:
      - section_markdown: grouped per-ticker bullet list, with one
        sub-block per ticker that has fresh catalysts. Tickers with
        nothing fresh are omitted from the markdown (don't waste lines)
        but still appear in tag_map with the '—' placeholder.
      - tag_map: {ticker: short_tag} for the dashboard CAT column.

    The caller (main/collect) MUST call this BEFORE get_tickers() so the
    tag map is available to fill the dashboard column.

    Per-ticker fetches go through _fetch_catalysts_for which is now
    cache-backed by _CATALYST_CACHE — calling this function for two
    subscribers with overlapping watchlists triggers ONE network round
    trip per unique ticker."""
    if not FMP_API_KEY:
        # Yahoo-only fallback path — still useful, just less coverage.
        # Tag map will mostly be NEWS/— rather than UPG/DOWN.
        pass

    chunks = []
    tag_map = {}
    # Sort-key lookup — built once here rather than per iteration, which also
    # keeps the sort lambda below from closing over a loop-local variable.
    priority_index = {t: i for i, t in enumerate(_TAG_PRIORITY)}
    for ticker in watchlist:
        try:
            catalysts = _fetch_catalysts_for(ticker)
        except Exception as e:
            chunks.append(f"*{ticker}* — _error: {type(e).__name__}_")
            tag_map[ticker] = "—"
            continue

        tag_map[ticker] = _best_tag(catalysts)

        if not catalysts:
            continue                                     # skip in markdown, '—' in tag_map

        # Sort: tag priority first, then date descending. So the
        # strongest signal per ticker appears first.
        catalysts.sort(key=lambda c: (
            priority_index.get(c["tag"], len(_TAG_PRIORITY)),
            -datetime.strptime(c["date"], "%Y-%m-%d").toordinal()
            if c.get("date") else 0,
        ))

        lines = [f"*{ticker}*"]
        # cap per ticker; 4 is enough context
        for c in catalysts[:4]:
            headline = escape_md(c["headline"])
            if c["link"]:
                lines.append(
                    f"- `{c['source']}` {c['date']}: [{headline}]({c['link']})"
                )
            else:
                lines.append(
                    f"- `{c['source']}` {c['date']}: {headline}"
                )
        chunks.append("\n".join(lines))

    if not chunks:
        return ("_no fresh catalysts across watchlist_", tag_map)
    return ("\n\n".join(chunks), tag_map)


# ============================================================================
# Data-fetching sections (one per numbered part of the brief)
# ============================================================================

def get_tickers(watchlist, catalyst_tags=None):
    """Build the dashboard table for `watchlist`.

    Now derives EIGHT columns per ticker:

      PRICE  - latest regular-session close
      VOL    - latest day's volume (formatted 12.3M / 1.2B)
      PRE    - pre-market % move vs prev close (real intraday data)
      1D     - day-over-day percent change         (immediate momentum)
      5D     - 5-trading-day percent change        (weekly trend)
      52HI   - distance from 52-week high in %     (crowding signal)
      52LO   - distance from 52-week low in %      (drawdown signal)
      CAT    - catalyst tag (UPG/DOWN/8-K/PR/NEWS/—) from
               get_per_ticker_catalysts(); passed in via `catalyst_tags`
               so this function doesn't double-fetch the catalyst data.

    `catalyst_tags` is the second element returned by
    get_per_ticker_catalysts(). When omitted, the CAT column shows '—'
    for every row — useful for standalone smoke tests of this function.

    Output is a fixed-width table wrapped in triple-backticks. Widths
    are tuned to fit Telegram mobile monospace (~66 chars per row) so
    columns don't wrap on a phone.
    """
    catalyst_tags = catalyst_tags or {}

    # Column widths chosen so the row totals ~66 chars (fits Telegram
    # mobile monospace). Percent columns dropped to 1 decimal — at-a-
    # glance reading is what matters here, not precision. The 52HI/LO
    # columns are 8 chars wide because outliers like MU and INTC have
    # +700%+ 52-low values that would otherwise crowd the next column.
    header = (
        f"{'TICKER':<6}"            # 6: e.g. 'AAPL  '
        f"{'PRICE':>9}"             # 9: e.g. ' $234.50'
        f"{'VOL':>7}"               # 7: e.g. '  12.3M'
        f"{'PRE':>7}"               # 7: e.g. '  +1.2%'
        f"{'1D':>7}"
        f"{'5D':>7}"
        f"{'52HI':>8}"              # 8: fits '+999.9%' without crowding
        f"{'52LO':>8}"
        f"{'CAT':>5}"               # 5: tag is <=4 chars (DOWN)
    )
    rows = [header]

    for ticker in watchlist:
        try:
            # ONE HTTP call per ticker, returning ~252 trading days of bars.
            # All daily-bar metrics below come out of this single DataFrame.
            data = yf.Ticker(ticker).history(period="1y")
            if len(data) < 6:                          # need >=6 rows for a 5-day change
                continue

            closes = data["Close"]                     # Series indexed by date
            volumes = data["Volume"]

            last = closes.iloc[-1]                     # latest close
            # previous close (1d denominator)
            prev = closes.iloc[-2]
            # 5 trading days back (5d denominator)
            five_ago = closes.iloc[-6]
            year_high = closes.max()                   # 52-week high (window-wide)
            year_low = closes.min()                    # 52-week low
            volume = volumes.iloc[-1]                  # latest day's volume

            pct_1d = ((last - prev) / prev) * 100
            pct_5d = ((last - five_ago) / five_ago) * 100
            from_hi = ((last - year_high) / year_high) * \
                100  # 0 at high, negative below
            from_lo = ((last - year_low) / year_low) * \
                100    # 0 at low, positive above

            # Pre-market: separate intraday fetch via _premarket_pct. Cached
            # for the script run so the movers section reuses this work.
            # `last` is the prev-session close from the daily fetch above,
            # passed in to avoid a second daily-bars round-trip.
            _, pre_pct = _premarket_pct(ticker, prev_close=last)
            pre_str = f"{pre_pct:+.1f}%" if pre_pct is not None else "—"

            price_str = f"${last:.2f}"
            cat_tag = catalyst_tags.get(ticker, "—")

            rows.append(
                f"{ticker:<6}"
                f"{price_str:>9}"
                f"{_format_volume(volume):>7}"
                f"{pre_str:>7}"
                # '+' forces sign even on positive; 1 dp keeps column tight.
                # 1D/5D get a 6-wide number (max +/-99.9% in practice);
                # 52HI/LO get 7-wide (some watchlist names sit +500% above
                # their 52w low and the column needs room for it).
                f"{pct_1d:>+6.1f}%"
                f"{pct_5d:>+6.1f}%"
                f"{from_hi:>+7.1f}%"
                f"{from_lo:>+7.1f}%"
                f"{cat_tag:>5}"
            )
        except Exception as e:
            # One bad ticker shouldn't break the dashboard — record the
            # failure inline and keep going so the rest of the table renders.
            rows.append(f"{ticker:<6} error ({type(e).__name__})")

    if len(rows) == 1:                                # only header — nothing fetched
        return "_no ticker data_"

    # Wrap in triple-backticks for Telegram's monospace code-block rendering.
    return "```\n" + "\n".join(rows) + "\n```"


# ============================================================================
# Key Levels — per-ticker price reference points that GROUND the trade ideas
# the analyst prompt produces. SKILL.md instructs the model to draw every
# numeric Entry/Stop/Target from this section rather than inventing prices it
# doesn't have. All values come from one year of daily bars (same source as
# get_tickers), so no new data dependency is introduced.
# ============================================================================

def _sma(series, n):
    """Simple moving average of the last `n` values, or None if the series
    is shorter than `n` (don't fabricate an average from too few bars)."""
    if len(series) < n:
        return None
    return float(series.tail(n).mean())


def _atr(data, n=14):
    """Average True Range over the last `n` bars — a volatility measure used
    for stop sizing. True Range uses the prior close, so we need n+1 rows;
    returns None below that. Computed with a small loop to avoid pandas
    multi-Series max gymnastics."""
    if len(data) < n + 1:
        return None
    highs = data["High"].tolist()
    lows = data["Low"].tolist()
    closes = data["Close"].tolist()
    trs = []
    for i in range(len(data) - n, len(data)):
        prev_close = closes[i - 1]
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        ))
    return sum(trs) / len(trs)


def _compute_key_levels(ticker):
    """One yfinance fetch → the level dict for `ticker`. Returns None when
    there aren't enough bars to be meaningful (matches get_tickers' >=6 rule).
    S/R = recent swing low/high (support/resistance); falls back to the full
    window when fewer than 55 bars are available."""
    data = yf.Ticker(ticker).history(period="1y")
    if len(data) < 6:
        return None
    closes = data["Close"]
    highs = data["High"]
    lows = data["Low"]
    has_55 = len(data) >= 55
    return {
        "price": float(closes.iloc[-1]),
        "sma20": _sma(closes, 20),
        "sma50": _sma(closes, 50),
        "atr":   _atr(data, 14),
        "s20":   float(lows.tail(20).min()),
        "r20":   float(highs.tail(20).max()),
        "s55":   float(lows.tail(55).min()) if has_55 else float(lows.min()),
        "r55":   float(highs.tail(55).max()) if has_55 else float(highs.max()),
    }


def _key_levels_for(ticker):
    """Cache-backed wrapper around _compute_key_levels. First caller pays the
    network cost; later callers in the same run hit _LEVELS_CACHE."""
    if ticker in _LEVELS_CACHE:
        return _LEVELS_CACHE[ticker]
    out = _compute_key_levels(ticker)
    _LEVELS_CACHE[ticker] = out
    return out


def _fmt_level(v):
    """Two-decimal price, or em-dash when the level couldn't be computed."""
    return f"{v:.2f}" if v is not None else "—"


def get_key_levels(watchlist):
    """Fixed-width per-ticker level table for `watchlist`:

      PRICE  - latest close
      SMA20  - 20-day simple moving average  (near-term dynamic S/R)
      SMA50  - 50-day simple moving average  (trend / invalidation line)
      ATR    - 14-day Average True Range      (stop-distance sizing)
      S20/R20- 20-day swing support / resistance
      S55/R55- 55-day swing support / resistance
      MAXPAIN- options max-pain (pin) strike
      HOC/HOP- highest open-interest call / put strike (magnet R / S)
      GWALL  - gamma wall: strike with the most dealer gamma (strong pin)

    These are the ONLY prices the analyst prompt is allowed to use for
    Entry/Stop/Target, so a name that can't be priced is simply omitted
    (the model then can't invent a level for it). The MAXPAIN/HOC/HOP columns
    reuse the per-ticker chain already fetched (and cached) by the Options
    Positioning section, so they add no network calls in the daily run."""
    header = (
        f"{'TICKER':<6}"
        f"{'PRICE':>8}"
        f"{'SMA20':>8}"
        f"{'SMA50':>8}"
        f"{'ATR':>8}"
        f"{'S20':>8}"
        f"{'R20':>8}"
        f"{'S55':>8}"
        f"{'R55':>8}"
        f"{'MAXPAIN':>9}"
        f"{'HOC':>8}"
        f"{'HOP':>8}"
        f"{'GWALL':>8}"
    )
    rows = [header]
    for ticker in watchlist:
        try:
            levels = _key_levels_for(ticker)
        except Exception as e:
            rows.append(f"{ticker:<6} error ({type(e).__name__})")
            continue
        if levels is None:
            continue
        # Option-implied levels reuse the cached chain from section 5 (no new
        # network in the daily run); None when no chain is available.
        chain = _fetch_chain(ticker)
        max_pain = chain.get("max_pain") if chain else None
        hi_oi_call = chain.get("hi_oi_call") if chain else None
        hi_oi_put = chain.get("hi_oi_put") if chain else None
        gamma_wall = chain.get("gamma_wall") if chain else None
        rows.append(
            f"{ticker:<6}"
            f"{_fmt_level(levels['price']):>8}"
            f"{_fmt_level(levels['sma20']):>8}"
            f"{_fmt_level(levels['sma50']):>8}"
            f"{_fmt_level(levels['atr']):>8}"
            f"{_fmt_level(levels['s20']):>8}"
            f"{_fmt_level(levels['r20']):>8}"
            f"{_fmt_level(levels['s55']):>8}"
            f"{_fmt_level(levels['r55']):>8}"
            f"{_fmt_level(max_pain):>9}"
            f"{_fmt_level(hi_oi_call):>8}"
            f"{_fmt_level(hi_oi_put):>8}"
            f"{_fmt_level(gamma_wall):>8}"
        )

    if len(rows) == 1:                                # only header — nothing priced
        return "_no key-level data_"
    return "```\n" + "\n".join(rows) + "\n```"


# ============================================================================
# Macro Dashboard — fixed lineup, NOT driven by the watchlist.
# ============================================================================

# Each entry: (display label, source, symbol, level formatter).
# `source` is 'yf' (yfinance, intraday) or 'fred' (FRED CSV, end-of-day).
# Why hard-coded instead of watchlist-driven: the macro lineup is the
# same backdrop every day regardless of which single names you're
# tracking, so it shouldn't shift when the watchlist changes.
# Why FRED for the 2Y: Yahoo publishes ^TNX (10Y) and ^TYX (30Y) but no
# 2Y yield series; FRED's DGS2 fills the gap with no API key needed.
_MACRO = [
    ("ES Futures", "yf",   "ES=F", lambda v: f"{v:,.2f}"),
    ("NQ Futures", "yf",   "NQ=F", lambda v: f"{v:,.2f}"),
    ("10Y Yield",  "yf",   "^TNX", lambda v: f"{v:.2f}%"),
    ("2Y Yield",   "fred", "DGS2", lambda v: f"{v:.2f}%"),
    ("VIX",        "yf",   "^VIX", lambda v: f"{v:.2f}"),
    ("DXY",        "yf",   "DX-Y.NYB", lambda v: f"{v:,.2f}"),
    ("WTI Oil",    "yf",   "CL=F", lambda v: f"${v:.2f}"),
    ("Brent Oil",  "yf",   "BZ=F", lambda v: f"${v:.2f}"),
    ("BTC",        "yf",   "BTC-USD", lambda v: f"${v:,.0f}"),
]


def _fetch_yf_macro(symbol):
    """Return (latest, previous) closes for a yfinance symbol, or None
    if the response is too short. period='5d' is enough to derive a 1D
    change without paying for the 1y backfill that get_tickers() needs
    for its 52-week distance columns."""
    data = yf.Ticker(symbol).history(period="5d")
    if len(data) < 2:
        return None
    closes = data["Close"]
    return float(closes.iloc[-1]), float(closes.iloc[-2])


def _fetch_fred_macro(series_id):
    """Return (latest, previous) observations from a FRED daily series.

    Uses FRED's public CSV download — no API key, no extra dependency.
    FRED publishes once per day with a 1-trading-day lag, so the 'latest'
    value is typically yesterday's close. That's acceptable for a daily
    brief (the rest of the dashboard is also a once-a-day snapshot).
    Skips '.' values, which is FRED's missing-data marker (holidays, etc.).
    """
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    values = []
    # [1:] skips the header row
    for line in resp.text.strip().splitlines()[1:]:
        _, val = line.split(",", 1)
        val = val.strip()
        if val and val != ".":
            values.append(float(val))
    if len(values) < 2:
        return None
    return values[-1], values[-2]


def get_macro_dashboard():
    """Build a fixed-width macro table: futures, yields, vol, dollar,
    oil, BTC. Three columns (NAME / LEVEL / 1D %) — narrower than the
    watchlist table because most macro series don't have a meaningful
    'volume' or 52-week range to read off (yields and DXY in particular).

    Each row is fetched independently so one bad symbol degrades to a
    single 'no data' line instead of breaking the whole section."""
    header = f"{'INDICATOR':<13}{'LEVEL':>12}{'1D':>8}"
    rows = [header]

    for label, source, symbol, fmt in _MACRO:
        try:
            if source == "yf":
                pair = _fetch_yf_macro(symbol)
            else:                                       # 'fred' is the only other source today
                pair = _fetch_fred_macro(symbol)

            if pair is None:
                rows.append(f"{label:<13}{'no data':>12}{'':>8}")
                continue

            last, prev = pair
            pct = ((last - prev) / prev) * 100
            rows.append(f"{label:<13}{fmt(last):>12}{pct:>+7.2f}%")
        except Exception as e:
            # Same per-row isolation philosophy as get_tickers(): record
            # the failure inline and keep the rest of the table rendering.
            rows.append(f"{label:<13} error ({type(e).__name__})")

    return "```\n" + "\n".join(rows) + "\n```"


# ============================================================================
# Pre-Market Movers (watchlist-filtered), Earnings, Economic Calendar.
#
# Was: scraped Nasdaq's PREMARKET endpoint for a market-wide top-10 list,
# which routinely returned penny-stock garbage (PROK, AGRX, sub-$2 names)
# the SKILL.md analyst prompt is explicitly told to dismiss. Net signal
# was negative — the section just trained the analyst to skip it.
#
# Now: rank YOUR watchlist by pre-market % move. The universe is small
# enough to be all signal, and these are the names the dashboard actually
# tracks downstream. Movers section answers "which of my names is gapping
# this morning" rather than "which untradeable shell is squeezing today".
# ============================================================================

# Movers must clear this absolute % move to make the list. Below 2% is
# noise (within typical overnight drift); 2%+ on a watchlist name is worth
# flagging as a potential gap-and-go / gap-and-fade setup.
_PREMARKET_MOVER_THRESHOLD_PCT = 2.0


def get_premarket_movers(watchlist):
    """Pre-market movers, filtered to `watchlist`.

    For each ticker, query the cached pre-market % move (filled
    earlier by get_tickers() — same _PREMARKET_CACHE), filter to those
    moving >= _PREMARKET_MOVER_THRESHOLD_PCT in either direction, and
    rank by absolute move. Top 10 by magnitude.

    If get_tickers() hasn't run yet in this script run (e.g. when this
    function is called standalone for smoke-testing), _premarket_pct
    populates the cache lazily on first hit — works either way."""
    movers = []
    for ticker in watchlist:
        try:
            pre_price, pct = _premarket_pct(ticker)
        except Exception:
            continue
        if pct is None or abs(pct) < _PREMARKET_MOVER_THRESHOLD_PCT:
            continue
        movers.append({
            "symbol": ticker,
            "price":  f"${pre_price:.2f}" if pre_price is not None else "—",
            "pct":    pct,
        })

    if not movers:
        return (
            "_no watchlist names moving "
            f">{_PREMARKET_MOVER_THRESHOLD_PCT:.0f}% in pre-market_"
        )

    # Largest absolute move first — caller wants the biggest gaps surfaced.
    movers.sort(key=lambda m: abs(m["pct"]), reverse=True)
    movers = movers[:10]

    header = f"{'TICKER':<7}{'PRE':>10}{'CHG':>10}"
    lines = [header]
    for m in movers:
        lines.append(
            f"{m['symbol']:<7}{m['price']:>10}{m['pct']:>+9.2f}%"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# ============================================================================
# Options positioning — three sub-blocks:
#   (a) MARKET-WIDE: SPY chain PCR + VIX9D/VIX term ratio
#   (b) PER-WATCHLIST: volume + OI put/call ratios per ticker (Tradier
#       preferred, yfinance fallback)
#   (c) UNUSUAL VOLUME: per-ticker today's total options volume vs cached
#       5-day rolling average; flags spikes >2x average
#
# Why this section exists: the SKILL.md prompt explicitly tells the
# analyst to identify "moves driven by positioning rather than
# fundamentals" — but had ZERO positioning data to work from. Every
# positioning call was read off price action + Reddit sentiment.
# ============================================================================

# Rolling history file. Anchored next to source like watchlist.json so the
# daily brief and any standalone smoke test read the same file regardless
# of cwd. Cache survives across runs — that's the whole point of the
# unusual-volume flag.
_OPTIONS_HISTORY_FILE = Path(__file__).parent / "options_history.json"

# How many days of history to keep per ticker. 10 lets us compute a 5-day
# rolling average with a few days of slack for missing entries.
_OPTIONS_HISTORY_CAP = 10

# Today's vol must be at least this multiple of the trailing average to
# get the UNUSUAL flag. 2.0 is a conventional "doubled volume" threshold —
# loose enough to fire occasionally, tight enough to mean something.
_UNUSUAL_VOLUME_MULTIPLE = 2.0

# Need at least this many historical days before we trust the average
# enough to flag anything. Below this, the section prints "building
# baseline" rather than fabricating signal off too few data points.
_UNUSUAL_BASELINE_MIN_DAYS = 3


def _load_options_history():
    """Read the JSON cache. Returns {} on missing/corrupt file (fresh
    start) rather than raising — first-run case is normal, not an error."""
    if not _OPTIONS_HISTORY_FILE.exists():
        return {}
    try:
        with open(_OPTIONS_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_options_history(history):
    """Persist the cache. Caps each ticker's entries at _OPTIONS_HISTORY_CAP
    before writing so the file doesn't grow unbounded over months."""
    trimmed = {
        ticker: entries[-_OPTIONS_HISTORY_CAP:]
        for ticker, entries in history.items()
    }
    try:
        with open(_OPTIONS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2)
    except OSError:
        # cache is best-effort; don't crash the brief
        pass


# ---------------------------------------------------------------------------
# Per-ticker chain fetchers
# ---------------------------------------------------------------------------

# Tradier sandbox base URL. The production endpoint is api.tradier.com,
# which requires a funded brokerage account; sandbox is free and serves
# the same market data with the same schema.
_TRADIER_BASE = "https://sandbox.tradier.com/v1"


def _tradier_headers():
    return {
        "Authorization": f"Bearer {TRADIER_TOKEN}",
        "Accept": "application/json",
    }


def _option_magnet_levels(call_oi, put_oi):
    """From {strike: open_interest} maps for calls and puts, derive three
    option-implied price levels:

      max_pain   - the listed strike that MINIMIZES total option-holder value
                   (Σ callOI·max(0,P−K) + Σ putOI·max(0,K−P)); the classic 'pin'.
      hi_oi_call - strike with the most call OI (overhead resistance / magnet).
      hi_oi_put  - strike with the most put OI (underlying support / magnet).

    Strikes with zero OI are ignored. Returns (max_pain, hi_oi_call, hi_oi_put);
    any element is None when that side has no open interest."""
    call_oi = {k: v for k, v in call_oi.items() if v > 0}
    put_oi = {k: v for k, v in put_oi.items() if v > 0}
    strikes = sorted(set(call_oi) | set(put_oi))
    if not strikes:
        return (None, None, None)

    max_pain, best_pain = None, None
    for price in strikes:
        pain = sum(oi * (price - k) for k, oi in call_oi.items() if price > k)
        pain += sum(oi * (k - price) for k, oi in put_oi.items() if price < k)
        if best_pain is None or pain < best_pain:
            best_pain, max_pain = pain, price

    hi_oi_call = max(call_oi, key=call_oi.get) if call_oi else None
    hi_oi_put = max(put_oi, key=put_oi.get) if put_oi else None
    return (max_pain, hi_oi_call, hi_oi_put)


def _oi_by_strike(df):
    """Build a {strike: open_interest} map from a yfinance calls/puts
    DataFrame (columns 'strike' and 'openInterest'). NaN OI counts as 0."""
    out = {}
    for strike, oi in zip(df["strike"], df["openInterest"].fillna(0)):
        try:
            out[float(strike)] = out.get(float(strike), 0) + int(oi)
        except (ValueError, TypeError):
            continue
    return out


def _tradier_expirations(ticker):
    """Return Tradier's list of available expiration dates (nearest first),
    or [] on any failure."""
    resp = requests.get(
        f"{_TRADIER_BASE}/markets/options/expirations",
        headers=_tradier_headers(),
        params={"symbol": ticker},
        timeout=10,
    )
    resp.raise_for_status()
    dates = (resp.json().get("expirations") or {}).get("date") or []
    if isinstance(dates, str):                          # single-date, unwrapped
        dates = [dates]
    return dates


def _tradier_chain_options(ticker, expiration, greeks=True):
    """Return the raw list of option dicts for one expiration, with greeks
    requested by default. Normalizes Tradier's single-option (dict) shape to
    a list."""
    resp = requests.get(
        f"{_TRADIER_BASE}/markets/options/chains",
        headers=_tradier_headers(),
        params={
            "symbol": ticker,
            "expiration": expiration,
            "greeks": "true" if greeks else "false",
        },
        timeout=10,
    )
    resp.raise_for_status()
    opts = (resp.json().get("options") or {}).get("option") or []
    if isinstance(opts, dict):                          # single-option case
        opts = [opts]
    return opts


def _gamma_wall(opts):
    """The strike carrying the most dealer gamma = Σ(gamma · open_interest)
    across BOTH calls and puts. Acts as a magnet/pin price tends to gravitate
    toward. Returns None when no option carries usable greeks (e.g. greeks
    were disabled, or Tradier hasn't computed them yet)."""
    by_strike = {}
    for o in opts:
        g = (o.get("greeks") or {}).get("gamma")
        oi = o.get("open_interest")
        strike = o.get("strike")
        if g is None or not oi or strike is None:
            continue
        try:
            by_strike[float(strike)] = by_strike.get(float(strike), 0.0) + abs(float(g)) * int(oi)
        except (ValueError, TypeError):
            continue
    by_strike = {k: v for k, v in by_strike.items() if v > 0}
    return max(by_strike, key=by_strike.get) if by_strike else None


def _atm_iv(opts):
    """At-the-money implied vol, as a percentage. ATM is found via delta (no
    spot price needed): the call nearest delta +0.50 and the put nearest
    delta −0.50. Averages whichever sides are available. Returns None when no
    option carries a usable mid_iv."""
    best = {"call": (None, None), "put": (None, None)}   # side -> (|Δ−target|, mid_iv)
    targets = {"call": 0.5, "put": -0.5}
    for o in opts:
        side = o.get("option_type")
        if side not in targets:
            continue
        gk = o.get("greeks") or {}
        delta, iv = gk.get("delta"), gk.get("mid_iv")
        if delta is None or iv is None:
            continue
        try:
            dist = abs(float(delta) - targets[side])
            iv = float(iv)
        except (ValueError, TypeError):
            continue
        if best[side][0] is None or dist < best[side][0]:
            best[side] = (dist, iv)
    ivs = [v for _, v in best.values() if v is not None]
    return (sum(ivs) / len(ivs)) * 100 if ivs else None


def _fetch_chain_tradier(ticker):
    """Pull the nearest expiration's full chain via Tradier (greeks ON) and
    return aggregate stats: volumes/OI, the option-implied magnet levels
    (max_pain, hi_oi_call, hi_oi_put), plus greeks-derived gamma_wall and
    atm_iv.

    Returns None on any failure so the caller can fall through to the
    yfinance path. Tradier returns {} for empty fields, NOT None, so we
    always cast through int() with a fallback."""
    if not TRADIER_TOKEN:
        return None
    try:
        dates = _tradier_expirations(ticker)
        if not dates:
            return None
        nearest = dates[0]
        opts = _tradier_chain_options(ticker, nearest, greeks=True)

        call_vol = call_oi = put_vol = put_oi = 0
        call_oi_strikes, put_oi_strikes = {}, {}
        for o in opts:
            vol = int(o.get("volume") or 0)
            oi = int(o.get("open_interest") or 0)
            strike = o.get("strike")
            if o.get("option_type") == "call":
                call_vol += vol
                call_oi += oi
                if strike is not None:
                    call_oi_strikes[float(strike)] = call_oi_strikes.get(float(strike), 0) + oi
            elif o.get("option_type") == "put":
                put_vol += vol
                put_oi += oi
                if strike is not None:
                    put_oi_strikes[float(strike)] = put_oi_strikes.get(float(strike), 0) + oi
        max_pain, hi_oi_call, hi_oi_put = _option_magnet_levels(
            call_oi_strikes, put_oi_strikes
        )
        return {
            "call_vol": call_vol,
            "put_vol":  put_vol,
            "call_oi":  call_oi,
            "put_oi":   put_oi,
            "expiration": nearest,
            "max_pain": max_pain,
            "hi_oi_call": hi_oi_call,
            "hi_oi_put": hi_oi_put,
            "gamma_wall": _gamma_wall(opts),
            "atm_iv": _atm_iv(opts),
        }
    except Exception:
        return None


def _fetch_chain_yfinance(ticker):
    """Fallback chain fetcher when Tradier is unavailable / unconfigured.
    Same return shape as _fetch_chain_tradier so callers don't branch.

    yfinance returns NaN for many .volume entries on thinly-traded strikes
    — .fillna(0) handles that. Slower than Tradier (yfinance scrapes the
    underlying Yahoo HTML) but works without a key."""
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        nearest = exps[0]
        chain = t.option_chain(nearest)
        max_pain, hi_oi_call, hi_oi_put = _option_magnet_levels(
            _oi_by_strike(chain.calls), _oi_by_strike(chain.puts)
        )
        return {
            "call_vol": int(chain.calls["volume"].fillna(0).sum()),
            "put_vol":  int(chain.puts["volume"].fillna(0).sum()),
            "call_oi":  int(chain.calls["openInterest"].fillna(0).sum()),
            "put_oi":   int(chain.puts["openInterest"].fillna(0).sum()),
            "expiration": nearest,
            "max_pain": max_pain,
            "hi_oi_call": hi_oi_call,
            "hi_oi_put": hi_oi_put,
            # yfinance gives no greeks — keep the dict shape stable so callers
            # don't branch; the table renders these as "—".
            "gamma_wall": None,
            "atm_iv": None,
        }
    except Exception:
        return None


def _fetch_chain(ticker):
    """Cache-backed wrapper. First caller pays the network cost; later
    subscribers in the broadcast loop hit _CHAIN_CACHE. A cached None
    means "we tried both Tradier and yfinance and got nothing" — DO NOT
    retry within the same run."""
    if ticker in _CHAIN_CACHE:
        return _CHAIN_CACHE[ticker]
    out = _fetch_chain_tradier(ticker) or _fetch_chain_yfinance(ticker)
    _CHAIN_CACHE[ticker] = out
    return out


def _safe_pcr(puts, calls):
    """Volume/OI ratio with a guard against divide-by-zero. Returns None
    when there's no call activity (which usually means the chain itself
    was empty — distinct from 'lots of puts, no calls' which is impossible
    in practice for liquid names)."""
    if not calls:
        return None
    return puts / calls


def _term_structure(symbol="SPY", n=3):
    """Put/call VOLUME ratio across the nearest `n` expirations for `symbol`.
    A rising PCR with tenor means downside protection is being built out the
    curve (structural caution); a falling PCR means hedging is front-loaded
    (near-term event risk). Tradier-only — returns None without a token or on
    any failure. Lightly throttled to stay friendly to the sandbox rate limit.

    SPY is the default because it's the deepest options market, so its term
    structure is the cleanest read on broad hedging posture."""
    if not TRADIER_TOKEN:
        return None
    try:
        dates = _tradier_expirations(symbol)[:n]
        if len(dates) < 2:
            return None
        points = []
        for i, exp in enumerate(dates):
            if i:
                time.sleep(0.2)                          # gentle on the sandbox
            opts = _tradier_chain_options(symbol, exp, greeks=False)
            cv = sum(int(o.get("volume") or 0) for o in opts if o.get("option_type") == "call")
            pv = sum(int(o.get("volume") or 0) for o in opts if o.get("option_type") == "put")
            pcr = _safe_pcr(pv, cv)
            if pcr is not None:
                points.append((exp, pcr))
        if len(points) < 2:
            return None
        chain = " → ".join(f"{exp} {pcr:.2f}" for exp, pcr in points)
        slope = points[-1][1] - points[0][1]
        note = ("downside hedging builds out the curve" if slope > 0.1
                else "front-loaded hedging (near-term risk)" if slope < -0.1
                else "flat across tenors")
        return f"{symbol} PCR term: {chain} — {note}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Market-wide block (SPY chain + VIX term)
# ---------------------------------------------------------------------------

def _market_wide_block():
    """Build the macro positioning sub-block. Two lines:
      - SPY put/call volume + OI ratios (broad equity positioning proxy)
      - VIX9D / VIX ratio (short-vol vs longer-vol; >1 = backwardation/stress)

    SPY is used as the proxy because it's the deepest, most liquid equity
    options market in the world — its PCR is a reasonable stand-in for
    'where is broad-market hedging today'. Using a real CBOE total-equity
    PCR would be slightly more correct but their public endpoints have
    moved repeatedly; SPY is stable across yfinance versions.
    """
    lines = []

    spy_chain = _fetch_chain("SPY")
    if spy_chain:
        vol_pcr = _safe_pcr(spy_chain["put_vol"], spy_chain["call_vol"])
        oi_pcr = _safe_pcr(spy_chain["put_oi"], spy_chain["call_oi"])
        vol_str = f"{vol_pcr:.2f}" if vol_pcr is not None else "—"
        oi_str = f"{oi_pcr:.2f}" if oi_pcr is not None else "—"
        lines.append(
            f"SPY PCR (exp {spy_chain['expiration']}): "
            f"vol *{vol_str}*, OI *{oi_str}*"
        )
    else:
        lines.append("SPY PCR: _no chain data_")

    term = _term_structure("SPY")
    if term:
        lines.append(term)

    try:
        vix9_hist = yf.Ticker("^VIX9D").history(period="5d")
        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if not vix9_hist.empty and not vix_hist.empty:
            vix9 = float(vix9_hist["Close"].iloc[-1])
            vix = float(vix_hist["Close"].iloc[-1])
            ratio = vix9 / vix if vix else None
            regime = "backwardation (stress)" if ratio and ratio > 1 else "contango (calm)"
            lines.append(
                f"VIX term: VIX9D *{vix9:.2f}* / VIX *{vix:.2f}* = "
                f"*{ratio:.2f}* — {regime}"
            )
        else:
            lines.append("VIX term: _no data_")
    except Exception as e:
        lines.append(f"VIX term: _error ({type(e).__name__})_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-watchlist block (PCRs + unusual-volume flag in one table)
# ---------------------------------------------------------------------------

def _per_watchlist_block(chains):
    """Render the per-ticker PCR table from a {ticker: chain_dict} map.
    Empty chains map → 'no chains fetched' line.

    The CAT-style column at the end is a per-ticker unusual-volume flag,
    populated by _unusual_volume_block via a side-effect dict so the
    history-based comparison happens in one place."""
    if not chains:
        return "_no per-watchlist chain data — set TRADIER_TOKEN to enable_"

    header = (
        f"{'TICKER':<7}"
        f"{'VOL_PCR':>9}"
        f"{'OI_PCR':>9}"
        f"{'TOT_VOL':>10}"
        f"{'ATM_IV':>9}"
    )
    lines = [header]
    for ticker in sorted(chains):
        c = chains[ticker]
        vol_pcr = _safe_pcr(c["put_vol"], c["call_vol"])
        oi_pcr = _safe_pcr(c["put_oi"], c["call_oi"])
        tot_vol = c["call_vol"] + c["put_vol"]
        iv = c.get("atm_iv")
        lines.append(
            f"{ticker:<7}"
            f"{(f'{vol_pcr:.2f}' if vol_pcr is not None else '—'):>9}"
            f"{(f'{oi_pcr:.2f}' if oi_pcr is not None else '—'):>9}"
            f"{_format_volume(tot_vol):>10}"
            f"{(f'{iv:.0f}%' if iv is not None else '—'):>9}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# ---------------------------------------------------------------------------
# Unusual-volume block (uses options_history.json cache)
# ---------------------------------------------------------------------------

def _unusual_volume_block(chains, history):
    """For each ticker in `chains`, compare today's total options volume
    to the trailing 5-day average from `history`. Flag any ticker whose
    today's volume exceeds the average by _UNUSUAL_VOLUME_MULTIPLE.

    Mutates `history` in place: appends today's reading per ticker.
    Caller is responsible for persisting `history` to disk after this
    returns.

    If too few baseline days exist, returns a "building baseline" notice
    rather than firing false positives on a 1-day sample."""
    today = datetime.now().date().isoformat()
    flagged = []
    baseline_short = []

    for ticker, c in chains.items():
        today_vol = c["call_vol"] + c["put_vol"]
        ticker_hist = history.setdefault(ticker, [])

        # Filter out any prior entry from today (re-runs same day shouldn't
        # double-count) before computing the rolling average.
        prior = [e for e in ticker_hist if e.get("date") != today]
        recent = prior[-5:]                             # last 5 trading days

        if len(recent) >= _UNUSUAL_BASELINE_MIN_DAYS:
            avg = sum(e["vol"] for e in recent) / len(recent)
            if avg and today_vol >= avg * _UNUSUAL_VOLUME_MULTIPLE:
                mult = today_vol / avg
                flagged.append(f"*{ticker}*: {mult:.1f}× avg "
                               f"({_format_volume(today_vol)} vs "
                               f"{_format_volume(int(avg))})")
        else:
            baseline_short.append(ticker)

        # Append today's reading (replacing any earlier same-day entry).
        history[ticker] = prior + [{"date": today, "vol": today_vol}]

    parts = []
    if flagged:
        parts.append("UNUSUAL volume vs 5-day avg:\n" + "\n".join(
            f"- {f}" for f in flagged
        ))
    else:
        parts.append("_no unusual options volume across watchlist_")
    if baseline_short:
        parts.append(
            f"_(building baseline for {len(baseline_short)} ticker(s); "
            f"need {_UNUSUAL_BASELINE_MIN_DAYS}+ runs to flag)_"
        )
    return "\n".join(parts)


def get_options_positioning(watchlist):
    """Compose the three sub-blocks into one section for `watchlist`.
    Each sub-block is independent — if Tradier/yfinance can't fetch a
    per-ticker chain, the market-wide block still renders; if VIX is
    down, the SPY line still renders; etc.

    Per-ticker chain fetches go through _fetch_chain which is now
    cache-backed by _CHAIN_CACHE — calling this function for two
    subscribers with overlapping watchlists triggers ONE network round
    trip per unique ticker, regardless of which subscriber asks first.

    Unusual-volume history is keyed by ticker globally (not per chat),
    so the cache file stays small and the baseline is robust."""
    chunks = []

    # (a) Market-wide first — cheap, always wants to render.
    chunks.append("_Market-wide_\n" + _market_wide_block())

    # Fetch every ticker's chain ONCE; both sub-blocks (b) and (c) read it.
    chains = {}
    for ticker in watchlist:
        c = _fetch_chain(ticker)
        if c is not None:
            chains[ticker] = c

    # (b) Per-watchlist PCRs.
    chunks.append("_Per-watchlist PCRs_\n" + _per_watchlist_block(chains))

    # (c) Unusual-volume flag, using the cache. Mutate-then-save pattern
    # so the cache is updated for tomorrow's run even when nothing is
    # flagged today.
    history = _load_options_history()
    chunks.append("_Unusual volume_\n" +
                  _unusual_volume_block(chains, history))
    _save_options_history(history)

    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Market session header + quotes/liquidity (Tradier clock/calendar + quotes)
# ---------------------------------------------------------------------------

def _tradier_holiday_note(now_et):
    """Holiday / half-day note for today from Tradier's market calendar, or ''
    when today is a normal full session (or the call fails)."""
    try:
        resp = requests.get(
            f"{_TRADIER_BASE}/markets/calendar",
            headers=_tradier_headers(),
            params={"month": now_et.month, "year": now_et.year},
            timeout=10,
        )
        resp.raise_for_status()
        days = (((resp.json().get("calendar") or {}).get("days") or {}).get("day") or [])
        if isinstance(days, dict):
            days = [days]
        today = now_et.strftime("%Y-%m-%d")
        for d in days:
            if d.get("date") != today:
                continue
            if (d.get("status") or "").lower() == "closed":
                desc = (d.get("description") or "").strip()
                return f"Holiday — market closed ({desc})" if desc else "Holiday — market closed"
            end = (d.get("open") or {}).get("end") or ""
            if end and end < "16:00":                    # regular close is 16:00 ET
                return f"Half-day — early close {end} ET"
            return ""
        return ""
    except Exception:
        return ""


def get_market_session():
    """One-line session header: where we are in the trading day plus any
    holiday/half-day note. Tradier /markets/clock is authoritative; falls back
    to a simple ET wall-clock heuristic (no holiday awareness) when no token is
    set or the call fails, so the brief always carries a session line."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if TRADIER_TOKEN:
        try:
            resp = requests.get(
                f"{_TRADIER_BASE}/markets/clock",
                headers=_tradier_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            c = resp.json().get("clock") or {}
            state = (c.get("state") or "").lower()
            label = {
                "premarket": "PRE-MARKET",
                "open": "OPEN",
                "postmarket": "AFTER-HOURS",
                "closed": "CLOSED",
            }.get(state, state.upper() or "UNKNOWN")
            # The clock's own description is generic noise ("Market is closed");
            # the calendar note carries the useful signal (holiday name / half-day).
            holiday = _tradier_holiday_note(now_et)
            return f"Session: {label}" + (f" · {holiday}" if holiday else "")
        except Exception:
            pass
    # Fallback: ET wall clock, regular-hours assumption.
    hm = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        label = "CLOSED (weekend)"
    elif hm < 9 * 60 + 30:
        label = "PRE-MARKET (opens 9:30 ET)"
    elif hm < 16 * 60:
        label = "OPEN"
    else:
        label = "AFTER-HOURS"
    return f"Session: {label} · {now_et:%Y-%m-%d %H:%M ET}"


def _fetch_quotes_tradier(symbols):
    """Batch real-time quotes for `symbols` in ONE Tradier call. Returns
    {symbol: quote_dict}, or {} on failure / no token."""
    if not TRADIER_TOKEN or not symbols:
        return {}
    try:
        resp = requests.get(
            f"{_TRADIER_BASE}/markets/quotes",
            headers=_tradier_headers(),
            params={"symbols": ",".join(symbols)},
            timeout=10,
        )
        resp.raise_for_status()
        q = (resp.json().get("quotes") or {}).get("quote") or []
        if isinstance(q, dict):                          # single-symbol case
            q = [q]
        return {item.get("symbol"): item for item in q if item.get("symbol")}
    except Exception:
        return {}


def get_quotes_liquidity(watchlist):
    """Per-ticker liquidity snapshot: last price, bid/ask SPREAD (tradeability),
    and average volume — one batched Tradier /markets/quotes call. The spread is
    the genuinely new datum: a wide spread warns a name is costly to get in and
    out of, which the price chart alone can't show."""
    if not TRADIER_TOKEN:
        return "_TRADIER_TOKEN not set — quotes/liquidity skipped_"
    quotes = _fetch_quotes_tradier(watchlist)
    if not quotes:
        return "_no quote data_"

    header = f"{'TICKER':<7}{'LAST':>10}{'SPREAD%':>9}{'AVG_VOL':>10}"
    lines = [header]
    for ticker in watchlist:
        q = quotes.get(ticker)
        if not q:
            lines.append(f"{ticker:<7}{'—':>10}{'—':>9}{'—':>10}")
            continue
        last, bid, ask = q.get("last"), q.get("bid"), q.get("ask")
        avgvol = q.get("average_volume")
        spread = None
        try:
            bid_f, ask_f = float(bid), float(ask)
            mid = (bid_f + ask_f) / 2
            if mid > 0:
                spread = (ask_f - bid_f) / mid * 100
        except (ValueError, TypeError):
            spread = None
        lines.append(
            f"{ticker:<7}"
            f"{(f'{float(last):.2f}' if last is not None else '—'):>10}"
            f"{(f'{spread:.2f}' if spread is not None else '—'):>9}"
            f"{(_format_volume(int(avgvol)) if avgvol else '—'):>10}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


def get_earnings_today():
    """Today's earnings calendar from FMP, top 15 by revenue estimate.

    Filter rationale (option 'b' from the design conversation): top-N by
    revenue is a zero-cost proxy for 'the names that matter today' — drops
    microcaps without needing a market-cap lookup per ticker. Names with
    no revenue estimate are dropped (typically uncovered shells).

    The 'time' field maps to BMO ('time-pre-market') / AMC ('time-after-hours'):
    if FMP omits it, the column reads '—' rather than fabricating a slot."""
    if not FMP_API_KEY:
        return "_FMP_API_KEY not set — section skipped_"

    today = datetime.now().strftime("%Y-%m-%d")
    url = (
        "https://financialmodelingprep.com/stable/earnings-calendar"
        f"?from={today}&to={today}&apikey={FMP_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json() or []
    except Exception as e:
        return f"_earnings calendar error: {type(e).__name__}_"

    with_revenue = [it for it in items if it.get("revenueEstimated")]
    with_revenue.sort(key=lambda x: x["revenueEstimated"], reverse=True)
    top = with_revenue[:15]

    if not top:
        return "_no covered names reporting today_"

    header = f"{'TICKER':<7}{'EPS EST':>10}{'REV EST':>12}{'TIME':>7}"
    lines = [header]
    for e in top:
        eps = e.get("epsEstimated")
        eps_str = f"${eps:.2f}" if eps is not None else "—"
        rev_str = _format_volume(e.get("revenueEstimated", 0))
        # FMP's time field varies between endpoints; accept the common shapes.
        raw = (e.get("time") or "").lower()
        time_str = "BMO" if "pre" in raw or raw == "bmo" else (
            "AMC" if "after" in raw or raw == "amc" else "—"
        )
        lines.append(
            f"{e.get('symbol', '?'):<7}{eps_str:>10}{rev_str:>12}{time_str:>7}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# Substring patterns identifying market-moving US releases on FRED. Names
# vary slightly across releases (e.g. "Advance Monthly Sales for Retail and
# Food Services" vs "Retail Trade"), so we use case-insensitive substring
# match rather than release-ID allowlist — more resilient to renames.
_MARKET_MOVING_RELEASES = (
    "Consumer Price Index",
    "Producer Price Index",
    "Personal Consumption Expenditures",
    "Personal Income",
    "Employment Situation",
    "Unemployment Insurance",          # weekly jobless claims
    "Job Openings",                    # JOLTS
    "Gross Domestic Product",
    "Retail",                          # catches retail sales releases
    "Industrial Production",
    "Housing Starts",
    "New Residential",
    "Existing Home",
    "Durable Goods",
    "Trade Balance",
    "Consumer Confidence",
    "Consumer Sentiment",
    "FOMC",
    "Federal Open Market",
)


def get_economic_calendar():
    """Today's US economic releases from FRED's release-dates API, filtered
    to a curated list of market-movers (CPI, NFP, FOMC, etc.).

    FRED gives release DATES, not intraday times or consensus/previous —
    that data is paywalled across the major aggregators since FMP and
    TradingEconomics killed their free tiers. We surface what releases hit
    today so the analyst prompt knows what to expect; the actual prints
    can be checked on the Fed's release pages or elsewhere."""
    if not FRED_API_KEY:
        return "_FRED_API_KEY not set — section skipped_"

    today = datetime.now().strftime("%Y-%m-%d")
    url = (
        "https://api.stlouisfed.org/fred/releases/dates"
        f"?api_key={FRED_API_KEY}"
        f"&realtime_start={today}&realtime_end={today}"
        "&include_release_dates_with_no_data=false"
        "&file_type=json"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        releases = resp.json().get("release_dates", [])
    except Exception as e:
        return f"_economic calendar error: {type(e).__name__}_"

    if not releases:
        return "_no US releases today_"

    seen_names = set()
    lines = []
    for r in releases:
        name = r.get("release_name", "")
        if not any(m.lower() in name.lower() for m in _MARKET_MOVING_RELEASES):
            continue
        # FRED occasionally lists the same release twice on the same day
        # (different release_ids for components). De-dupe by name.
        if name in seen_names:
            continue
        seen_names.add(name)
        lines.append(f"- {escape_md(name)}")
        if len(lines) >= 15:
            break

    if not lines:
        return "_no market-moving US releases today_"
    return "\n".join(lines)


def get_yahoo_news():
    """Pull the latest financial headlines from Yahoo Finance's RSS feed.

    UPGRADED: each item is now a tappable Markdown link `[title](url)`.
    - The TITLE is escape_md()'d because headlines often contain reserved
      Markdown chars (commonly '_' or '*') that would break Telegram's parser.
    - The URL is left RAW. Telegram's parser doesn't apply Markdown inside
      the URL portion of a link, so escaping it would just insert literal
      backslashes that break the URL.
    """
    try:
        feed = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        # cap at 8 to keep brief readable
        items = feed.entries[:8]
        if not items:
            return "_no headlines_"
        return "\n".join(
            f"- [{escape_md(item.title)}]({item.link})" for item in items
        )
    except Exception as e:
        return f"_yahoo news error: {type(e).__name__}_"


# Four WSJ RSS feeds covering the angles a market brief cares about. Article
# links are paywalled (WSJ subscription required), but headlines themselves
# are a strong signal — often the move-the-tape line of the day.
#
# Endpoint note: WSJ's legacy feeds.a.dj.com/rss/* host froze in Jan 2025
# (still responds, returns 474+-day-old entries). Live feeds are at the
# feeds.content.dowjones.io/public/rss/* host — same feed names, no .xml
# suffix.
_WSJ_FEEDS = (
    ("Markets",  "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("Business", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"),
    ("World",    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
    ("Opinion",  "https://feeds.content.dowjones.io/public/rss/RSSOpinion"),
)


def _fetch_wsj_feed(url, limit=4):
    """Return formatted headline lines from a single WSJ RSS feed.

    Matches get_yahoo_news()'s line shape: `- [title](url)`. No timestamp —
    WSJ feeds are kept fresh by editorial flow, so the headline order itself
    is the recency signal.
    """
    try:
        feed = feedparser.parse(url)
        items = feed.entries[:limit]
        if not items:
            return "_no headlines_"
        return "\n".join(
            f"- [{escape_md(item.title)}]({item.link})" for item in items
        )
    except Exception as e:
        return f"_error: {type(e).__name__}_"


def get_wsj_news():
    """Aggregate top headlines from four WSJ RSS feeds (Markets, Business,
    World, Opinion). Four headlines per feed, italicized sub-header per feed
    — same layout shape as get_reddit_scan() so the brief reads consistently.
    """
    chunks = [
        f"_{label}_\n{_fetch_wsj_feed(url)}" for label, url in _WSJ_FEEDS]
    return "\n\n".join(chunks)


def _format_age(published_parsed):
    """Compact relative-time formatting for Reddit RSS entries.

    published_parsed is a time.struct_time in UTC (feedparser's normalized form).
    Returns 'Xm ago' / 'Xh ago' / 'Xd ago'. Falls back to '?' on missing input —
    feedparser populates this field for every Reddit RSS entry in practice, but
    a defensive fallback keeps the brief from crashing on a malformed feed.
    """
    if not published_parsed:
        return "?"
    published = datetime(*published_parsed[:6], tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - published
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _fetch_subreddit_rss(subreddit, limit=5):
    """Fetch top-of-day posts from a subreddit via Reddit's public RSS feed.

    Reddit blocks the default Python urllib user-agent but accepts identified
    clients, so we pass REDDIT_USER_AGENT explicitly to feedparser. The 'top'
    sort with t=day surfaces what's blowing up in the last 24h — a better
    signal than 'hot' which can include slow-burning megathreads.

    RSS doesn't expose upvote scores, so we substitute the post's age. Cashtag
    extraction is unchanged from the old PRAW path.
    """
    url = f"https://www.reddit.com/r/{subreddit}/top/.rss?t=day"
    try:
        feed = feedparser.parse(url, agent=REDDIT_USER_AGENT)
        entries = feed.entries[:limit]
        if not entries:
            # bozo flag indicates a parse error; surface the exception type so
            # the user can tell "feed empty" from "Reddit returned 403/HTML".
            if getattr(feed, "bozo", False) and getattr(feed, "bozo_exception", None):
                return f"_r/{subreddit} error: {type(feed.bozo_exception).__name__}_"
            return f"_no posts in r/{subreddit}_"

        lines = []
        for entry in entries:
            tickers = _extract_tickers(entry.title)
            ticker_tag = f" — {', '.join(tickers)}" if tickers else ""
            age = _format_age(getattr(entry, "published_parsed", None))
            lines.append(
                f"- [{escape_md(entry.title)}]({entry.link}) | {age}{ticker_tag}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"_r/{subreddit} error: {type(e).__name__}_"


def get_reddit_scan():
    """Aggregate top-of-day posts from three trading-focused subreddits.

    One Telegram message, three italicized sub-headers. WSB is the loudest
    so it leads; r/options and r/stocks add directional and fundamentals-y
    counterweight so the section isn't pure meme sentiment.
    """
    subreddits = ["wallstreetbets", "options", "stocks"]
    chunks = [f"_r/{sub}_\n{_fetch_subreddit_rss(sub)}" for sub in subreddits]
    return "\n\n".join(chunks)


# ============================================================================
# Alternative output modes (kept for manual use; not part of the daily run)
# ============================================================================

def collect():
    """Write the brief to market_data.md instead of sending it.

    Uses the DEDUPED UNION of every active subscriber's watchlist so the
    single market_data.md file (and the downstream final_briefing.md that
    Claude writes from it) covers every ticker any user cares about.
    bot.py then filters the briefing per subscriber at send time.

    Falls back to the admin's own watchlist when there are no active
    subscribers — preserves single-user dev mode and the first-run case
    where no /start has been sent yet.

    Catalysts are fetched FIRST so the dashboard's CAT column reflects
    real data — same ordering rule as main()."""
    db.init()
    watchlist = db.union_active_watchlists()
    if not watchlist:
        # No active subscribers (or none with tickers) — fall back to the
        # admin chat so a fresh deployment / dev box still produces output.
        chat_id = int(CHAT_ID) if CHAT_ID else None
        if chat_id is None:
            print("No active subscribers and TELEGRAM_CHAT_ID not set —"
                  " collect() has no watchlist to work from.")
            return
        watchlist = load_watchlist(chat_id)
    data = build_market_data(watchlist)
    with open("market_data.md", "w", encoding="utf-8") as f:
        f.write(data)
    print("market_data.md created successfully")


def build_market_data(watchlist):
    """Build the full market_data.md content for `watchlist` and return it as
    a string (no file I/O). This is the in-memory core that collect() writes
    to disk and that bot.py's /refresh reuses to generate a private, per-user
    brief without touching the shared market_data.md / final_briefing.md.

    Resets the per-run caches FIRST: collect() runs as a one-shot process so
    its caches start empty, but bot.py is long-lived — without a reset the
    first /refresh of the day would pin its prices into every later refresh.

    Catalysts are fetched FIRST so get_tickers() can fill the dashboard's CAT
    column from the same data (no double-fetch)."""
    reset_run_caches()
    catalyst_text, catalyst_tags = get_per_ticker_catalysts(watchlist)
    return f"""# Daily Market Data
{get_market_session()}

## 1. Live Ticker Dashboard
{get_tickers(watchlist, catalyst_tags)}

## 2. Macro Dashboard
{get_macro_dashboard()}

## 3. Per-Ticker Catalysts
{catalyst_text}

## 4. Pre-Market Movers (Watchlist)
{get_premarket_movers(watchlist)}

## 5. Options Positioning
{get_options_positioning(watchlist)}

## 6. Earnings Today
{get_earnings_today()}

## 7. Economic Calendar
{get_economic_calendar()}

## 8. Yahoo Finance Headlines
{get_yahoo_news()}

## 9. WSJ Headlines
{get_wsj_news()}

## 10. Reddit Scan
{get_reddit_scan()}

## 11. Key Levels (Entry/Stop/Target must be drawn from this table)
{get_key_levels(watchlist)}

## 12. Quotes & Liquidity
{get_quotes_liquidity(watchlist)}
"""


def send_file(path):
    """Read a file's contents and send them to Telegram as one message."""
    with open(path, "r", encoding="utf-8") as f:
        message = f.read()
    send_telegram(message)
    print(f"Sent {path} to Telegram")


# ============================================================================
# The daily-brief entry point
# ============================================================================

_ANALYST_PROMPT = (
    "*11. Analyst Prompt for Claude*\n"
    "Approach the data like a skeptical buy-side analyst.\n"
    "Prioritize signal over novelty.\n"
    "Identify:\n"
    "- what institutions are likely paying attention to\n"
    "- what narratives are strengthening or breaking\n"
    "- what looks crowded or consensus-driven\n"
    "- what is likely retail/meme noise\n"
    "- which moves appear driven by positioning rather than fundamentals\n"
    "- which tickers are dangerous to chase despite strong momentum\n"
    "When per-ticker catalysts exist (section 3), use them to explain WHY a "
    "name moved instead of guessing 'flow vs fundamental'.\n"
    "Weight the options-positioning data (section 5) when scoring "
    "institutional vs retail conviction: lopsided PCRs and unusual-volume "
    "flags are positioning evidence the price chart alone can't give you.\n"
    "Explicitly dismiss low-quality narratives, penny-stock squeezes, and low-liquidity hype.\n"
    "Do not treat all headlines or movers as equally important.\n"
    "If signals conflict, explain which signal likely matters more and why."
)


def _build_personalized_sections(date_str, watchlist):
    """Build the per-chat portion of the brief. Returns an ordered dict
    of section_name -> rendered_markdown. The caller merges these with
    the run-shared sections (macro, news, etc.) before sending."""
    catalyst_text, catalyst_tags = get_per_ticker_catalysts(watchlist)
    return {
        "header":    f"*Daily Market Brief — {date_str}*",
        "tickers":   f"*1. Live Ticker Dashboard*\n{get_tickers(watchlist, catalyst_tags)}",
        "catalysts": f"*3. Per-Ticker Catalysts*\n{catalyst_text}",
        "movers":    f"*4. Pre-Market Movers (Watchlist)*\n{get_premarket_movers(watchlist)}",
        "options":   f"*5. Options Positioning*\n{get_options_positioning(watchlist)}",
    }


def main():
    """Daily-run entry point: validate env, then build and broadcast the
    brief to every recipient (env CHAT_ID + active DB subscribers). Returns
    early without sending if any required environment variable is missing."""
    # Use the shared check from core so the same rule applies in both
    # market_brief.py and bot.py: fail fast with the missing variable names.
    missing = check_required_env()
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        return

    db.init()
    reset_run_caches()

    date_str = datetime.now().strftime('%b %d, %Y')

    # Build the recipients list: env CHAT_ID + every active DB subscriber,
    # deduped (the env chat is always also a DB subscriber after init()'s
    # bootstrap migration, but dedup defensively in case someone removes
    # it manually from the DB).
    env_chat = int(CHAT_ID) if CHAT_ID else None
    recipients = list(dict.fromkeys(                  # ordered dedup
        ([env_chat] if env_chat is not None else [])
        + db.list_active_chat_ids()
    ))
    if not recipients:
        print("No recipients — nothing to send.")
        return
    print(f"Broadcasting to {len(recipients)} chat(s): {recipients}")

    # Compute shared sections ONCE — these are identical across every
    # recipient regardless of their personal watchlist. Building them
    # before the broadcast loop keeps the loop cheap and ensures every
    # subscriber sees the same macro/news context.
    shared = {
        "macro":    f"*2. Macro Dashboard*\n{get_macro_dashboard()}",
        "earnings": f"*6. Earnings Today*\n{get_earnings_today()}",
        "econ":     f"*7. Economic Calendar*\n{get_economic_calendar()}",
        "news":     f"*8. Yahoo Finance Headlines*\n{get_yahoo_news()}",
        "wsj":      f"*9. WSJ Headlines*\n{get_wsj_news()}",
        "reddit":   f"*10. Reddit Scan*\n{get_reddit_scan()}",
        "prompt":   _ANALYST_PROMPT,
    }

    for chat_id in recipients:
        watchlist = load_watchlist(chat_id)
        if not watchlist:
            # A subscriber with an empty watchlist still gets the shared
            # sections — better than silently skipping them entirely.
            print(
                f"  chat {chat_id}: empty watchlist, sending shared sections only")

        personalized = _build_personalized_sections(date_str, watchlist)

        # Send order: header, tickers, macro, catalysts, movers, options,
        # earnings, econ, news, wsj, reddit, prompt. dict preserves
        # insertion order so we build the merged dict in the right order
        # explicitly here rather than relying on personalized + shared
        # to interleave the way we want.
        ordered = {
            "header":    personalized["header"],
            "tickers":   personalized["tickers"],
            "macro":     shared["macro"],
            "catalysts": personalized["catalysts"],
            "movers":    personalized["movers"],
            "options":   personalized["options"],
            "earnings":  shared["earnings"],
            "econ":      shared["econ"],
            "news":      shared["news"],
            "wsj":       shared["wsj"],
            "reddit":    shared["reddit"],
            "prompt":    shared["prompt"],
        }

        for section_text in ordered.values():
            send_telegram(section_text, chat_id=chat_id)


if __name__ == "__main__":
    # The "bot" subcommand was removed when the file was split — bot logic
    # now lives in bot.py and is run as a separate, long-lived process.
    # If you used to run `python market_brief.py bot`, run `python bot.py` instead.
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "collect":
            collect()
        elif command == "send":
            if len(sys.argv) < 3:
                print("Usage: python market_brief.py send <file>")
            else:
                send_file(sys.argv[2])
        else:
            print(f"Unknown command: {command}")
    else:
        main()
