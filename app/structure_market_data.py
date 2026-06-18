"""
structure_market_data.py
========================
Normalization layer between `market_brief.py collect` (which writes the messy
market_data.md blob) and `generate_briefing.py` (which calls the LLM).

It parses market_data.md's 12 numbered sections into a clean, source-attributed
JSON document (market_data_structured.json) and, best-effort, fetches the linked
news articles so the model can analyze real article text instead of bare links.

Design goals (per the refactor request): small helper functions, deterministic
behavior, minimal dependencies (requests + BeautifulSoup), graceful failure — a
single unfetchable article must never break the run. Missing values are null,
never guessed; weak/absent sections are recorded in `data_gaps`.

CLI:
  python structure_market_data.py                 # market_data.md -> market_data_structured.json
  python structure_market_data.py in.md out.json  # explicit paths
"""

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib import robotparser

import requests

# parse_key_levels is the single source of truth for the section-11 table; reuse
# it rather than re-implementing the column parsing.
from validate_briefing import parse_key_levels

APP_DIR = Path(__file__).parent
DEFAULT_MARKET_DATA = APP_DIR / "market_data.md"
DEFAULT_STRUCTURED = APP_DIR / "market_data_structured.json"
CACHE_DIR = APP_DIR.parent / "cache" / "articles"

# --- Fetch limits (approved amendment #2) -----------------------------------
ARTICLE_TIMEOUT_SEC = 10
MAX_ARTICLES_PER_RUN = 25
ARTICLE_CACHE_TTL_HOURS = 24
MAX_ARTICLE_TEXT_CHARS = 6000
_SUMMARY_CHARS = 900
_MIN_TEXT_CHARS = 200          # below this we treat extraction as a paywall/empty
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Ticker token shape (mirrors validate_briefing._TICKER_TOKEN_RE).
_TICKER_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9.\^=]{0,9}\b")
# Markdown link [text](url) and bare URLs.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<!\()\bhttps?://[^\s)\]]+")

# Uppercase tokens that look like tickers but are common acronyms in headlines —
# kept out of non_watchlist_mentions to cut obvious false positives.
_TICKER_STOPWORDS = {
    "AI", "CEO", "CFO", "COO", "IPO", "ETF", "ETFS", "US", "USA", "UK", "EU",
    "PT", "PCR", "GDP", "CPI", "PCE", "FOMC", "FED", "SEC", "NHS", "CNBC", "WSJ",
    "Q1", "Q2", "Q3", "Q4", "M&A", "AI.", "OK", "AM", "PM", "ET", "AND", "THE",
    "AMD", "WWDC", "ARMEC", "RSS", "VOO", "SPY",
}


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

def _split_sections(text):
    """Return {section_number(int): body(str)} for each `## N. Title` block."""
    out = {}
    matches = list(re.finditer(r"^##\s*(\d+)\.\s*(.+)$", text, re.MULTILINE))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[num] = text[start:end].strip()
    return out


def _fenced_block(body):
    """Return the lines inside the first ``` fenced block of `body`, or all
    non-blank lines if there is no fence."""
    fence = re.search(r"```(.*?)```", body, re.DOTALL)
    raw = fence.group(1) if fence else body
    return [ln.rstrip() for ln in raw.splitlines() if ln.strip()]


def _to_float(token):
    """Best-effort float from a token like '$291.13', '+1.6%', '7,435.00'."""
    if token is None:
        return None
    cleaned = token.replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_session(text):
    m = re.search(r"^Session:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return {"raw": None, "status": None}
    raw = m.group(1).strip()
    status = raw.split("·")[0].strip() if raw else None
    return {"raw": raw, "status": status}


def _parse_live_ticker_dashboard(body):
    """Section 1 fixed-width table -> list of ticker objects + watchlist."""
    rows = []
    for line in _fenced_block(body):
        parts = line.split()
        if not parts or parts[0] == "TICKER":
            continue
        t = parts[0]
        if not _TICKER_TOKEN_RE.fullmatch(t):
            continue
        cols = parts[1:]

        def at(i):
            return cols[i] if i < len(cols) else None
        rows.append({
            "ticker": t,
            "price": _to_float(at(0)),
            "price_raw": at(0),
            "volume": at(1),
            "premarket": at(2),
            "change_1d": at(3),
            "change_5d": at(4),
            "off_52wk_high": at(5),
            "off_52wk_low": at(6),
            "catalyst_tag": at(7),
        })
    return rows


def _parse_macro_dashboard(body):
    """Section 2 -> {indicator: {level, change_1d}}. Indicator names can contain
    spaces, and the value may be an error string, so parse leniently."""
    out = {}
    for line in _fenced_block(body):
        if line.split() and line.split()[0] == "INDICATOR":
            continue
        # Trailing change token is the last whitespace-group that looks numeric.
        m = re.match(r"^(.*?)\s{2,}(.+)$", line)
        if not m:
            continue
        name = m.group(1).strip()
        rest = m.group(2).split()
        if len(rest) >= 2 and (rest[-1].endswith("%") or _to_float(rest[-1]) is not None):
            level = " ".join(rest[:-1])
            change = rest[-1]
        else:
            level = " ".join(rest)
            change = None
        out[name] = {"level": level, "change_1d": change}
    return out


def _parse_per_ticker_catalysts(body):
    """Section 3 -> {ticker: [{catalyst_type, date, summary, source, url}]}."""
    out = {}
    current = None
    for line in body.splitlines():
        s = line.strip()
        hdr = re.match(r"^\*([A-Z][A-Z0-9.\^=]{0,9})\*$", s)
        if hdr:
            current = hdr.group(1)
            out[current] = []
            continue
        if current is None or not s.startswith("-"):
            continue
        m = re.match(r"^-\s*`([^`]+)`\s*([\d-]+)?:?\s*(.*)$", s)
        if not m:
            continue
        source = m.group(1).strip()
        date = m.group(2)
        rest = m.group(3).strip()
        link = _MD_LINK_RE.search(rest)
        if link:
            summary, url = link.group(1).strip(), link.group(2).strip()
        else:
            summary, url = rest, None
        out[current].append({
            "catalyst_type": source,
            "source": source,
            "date": date,
            "summary": summary,
            "url": url,
        })
    return out


def _parse_options_positioning(body):
    """Section 5 -> {market_wide:[...], per_ticker:{...}, unusual_volume:[...]}."""
    market_wide = []
    per_ticker = {}
    unusual = []
    for raw in body.splitlines():
        s = raw.strip()
        if s.startswith("SPY PCR") or s.startswith("VIX term"):
            market_wide.append(s.replace("*", ""))
        if s.startswith("- *"):
            mu = re.match(r"^-\s*\*([^*]+)\*:\s*(.+)$", s)
            if mu:
                unusual.append({"ticker": mu.group(1).strip(),
                                "detail": mu.group(2).strip()})
    for line in _fenced_block(body):
        parts = line.split()
        if not parts or parts[0] == "TICKER":
            continue
        if not _TICKER_TOKEN_RE.fullmatch(parts[0]):
            continue
        c = parts[1:]
        per_ticker[parts[0]] = {
            "vol_pcr": _to_float(c[0]) if len(c) > 0 else None,
            "oi_pcr": _to_float(c[1]) if len(c) > 1 else None,
            "total_volume": c[2] if len(c) > 2 else None,
            "atm_iv": c[3] if len(c) > 3 else None,
        }
    return {"market_wide": market_wide, "per_ticker": per_ticker,
            "unusual_volume": unusual}


def _domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except ValueError:
        return None


def _collect_links(sections, watchlist):
    """Walk the link-bearing sections (3, 8, 9, 10) and return a deduped list of
    raw story dicts {url, source, title, published_at, related_tickers}. §3 gives
    us a ticker + date for free; §8/9/10 titles are scanned for watchlist names."""
    seen = {}

    def add(url, title, related, published=None):
        url = url.strip()
        if not url or url in seen:
            if url in seen and related:
                for t in related:
                    if t not in seen[url]["related_tickers"]:
                        seen[url]["related_tickers"].append(t)
            return
        seen[url] = {
            "url": url,
            "source": _domain(url),
            "title": title.strip() if title else None,
            "published_at": published,
            "related_tickers": list(related),
        }

    # §3 — per-ticker catalysts (ticker + date known)
    cat = _parse_per_ticker_catalysts(sections.get(3, ""))
    for ticker, items in cat.items():
        for it in items:
            if it.get("url"):
                add(it["url"], it["summary"], [ticker], it.get("date"))

    # §8/§9/§10 — headline lists; infer tickers from the title text.
    for num in (8, 9, 10):
        body = sections.get(num, "")
        for m in _MD_LINK_RE.finditer(body):
            title, url = m.group(1), m.group(2)
            related = [t for t in _TICKER_TOKEN_RE.findall(title.upper())
                       if t in watchlist]
            add(url, title, related)
    return list(seen.values())


def _non_watchlist_mentions(sections, watchlist):
    """Tickers surfacing in headline/Reddit titles that are NOT in the price
    table — structured support for 'Potential New Watchlist Candidates'
    (amendment #7). Conservative: requires ticker-shaped uppercase tokens not in
    a small acronym stoplist."""
    counts = {}
    for num in (3, 8, 9, 10):
        for m in _MD_LINK_RE.finditer(sections.get(num, "")):
            title = m.group(1)
            for tok in _TICKER_TOKEN_RE.findall(title):
                if (len(tok) >= 2 and tok not in watchlist
                        and tok not in _TICKER_STOPWORDS and not tok.isdigit()):
                    entry = counts.setdefault(tok, {"ticker": tok, "mentions": 0,
                                                    "sample_titles": []})
                    entry["mentions"] += 1
                    if len(entry["sample_titles"]) < 2:
                        entry["sample_titles"].append(title.strip())
    # Only surface names mentioned at least twice — single hits are mostly noise.
    return [v for v in counts.values() if v["mentions"] >= 2]


# ---------------------------------------------------------------------------
# Article fetching (best-effort, cached, graceful)
# ---------------------------------------------------------------------------

def _cache_path(url):
    return CACHE_DIR / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")


def _cache_get(url):
    p = _cache_path(url)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cached_at = data.get("_cached_at")
    if cached_at:
        try:
            age_h = (time.time() - float(cached_at)) / 3600.0
            if age_h > ARTICLE_CACHE_TTL_HOURS:
                return None
        except (TypeError, ValueError):
            return None
    return data.get("article")


def _cache_put(url, article):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(
            json.dumps({"_cached_at": time.time(), "article": article}),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[structure] cache write failed for {url}: {type(e).__name__}: {e}")


_robots_cache = {}


def _robots_allows(url):
    """Best-effort robots.txt check. If robots can't be read, assume allowed."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    rp = _robots_cache.get(base)
    if rp is None:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:  # pylint: disable=broad-exception-caught
            rp = False  # could not read -> treat as allowed
        _robots_cache[base] = rp
    if rp is False:
        return True
    try:
        return rp.can_fetch(_USER_AGENT, url)
    except Exception:  # pylint: disable=broad-exception-caught
        return True


def _deterministic_summary(text):
    """Lead + sentences that carry numbers/$/%, capped at _SUMMARY_CHARS. No LLM."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    picked, seen = [], set()
    for s in sentences[:3]:
        if s and s not in seen:
            picked.append(s)
            seen.add(s)
    for s in sentences:
        if len(" ".join(picked)) >= _SUMMARY_CHARS:
            break
        if s and s not in seen and re.search(r"[\d$%]", s):
            picked.append(s)
            seen.add(s)
    return " ".join(picked)[:_SUMMARY_CHARS].strip()


def _extract_article(html):
    """Pull title/published/author/text from HTML using BeautifulSoup. Returns a
    dict of the four fields (any may be None/empty)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "aside", "header", "footer",
                     "form", "noscript", "figure", "iframe"]):
        tag.decompose()

    def meta(*queries):
        for attr, val in queries:
            el = soup.find("meta", attrs={attr: val})
            if el and el.get("content"):
                return el["content"].strip()
        return None

    title = meta(("property", "og:title")) or (
        soup.title.string.strip() if soup.title and soup.title.string else None)
    published = meta(("property", "article:published_time"),
                     ("name", "article:published_time"),
                     ("name", "pubdate"), ("name", "date"))
    author = meta(("name", "author"), ("property", "article:author"))

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = re.sub(r"\s+", " ", " ".join(p for p in paragraphs if p)).strip()
    return {"title": title, "published_at": published, "author": author,
            "text": text}


def fetch_article(url):
    """Fetch + extract one article. Always returns a story dict, never raises.
    On any block/paywall/timeout/non-HTML, fetch_status='failed' with a reason."""
    story = {
        "url": url, "source": _domain(url), "title": None, "published_at": None,
        "author": None, "text": None, "summary": None,
        "fetch_status": "failed", "error": None,
    }
    if not _robots_allows(url):
        story["error"] = "blocked by robots.txt"
        return story
    try:
        resp = requests.get(url, timeout=ARTICLE_TIMEOUT_SEC,
                            headers={"User-Agent": _USER_AGENT})
    except requests.exceptions.Timeout:
        story["error"] = f"timeout after {ARTICLE_TIMEOUT_SEC}s"
        return story
    except requests.exceptions.RequestException as e:
        story["error"] = f"request failed: {type(e).__name__}"
        return story

    if resp.status_code in (401, 402, 403):
        story["error"] = f"blocked/paywalled (HTTP {resp.status_code})"
        return story
    if not resp.ok:
        story["error"] = f"HTTP {resp.status_code}"
        return story
    if "html" not in resp.headers.get("Content-Type", "").lower():
        story["error"] = "unsupported content type"
        return story

    try:
        extracted = _extract_article(resp.text)
    except Exception as e:  # pylint: disable=broad-exception-caught
        story["error"] = f"parse failed: {type(e).__name__}"
        return story

    text = extracted["text"] or ""
    if len(text) < _MIN_TEXT_CHARS:
        story.update(title=extracted["title"])
        story["error"] = "no extractable text (likely paywall)"
        return story

    truncated = text[:MAX_ARTICLE_TEXT_CHARS]
    story.update(
        title=extracted["title"],
        published_at=extracted["published_at"],
        author=extracted["author"],
        text=truncated,
        summary=_deterministic_summary(text),
        fetch_status="success",
        error=None,
    )
    return story


def _fetch_stories(raw_stories, fetch):
    """Fill each raw story with article content. Caps live fetches at
    MAX_ARTICLES_PER_RUN; the remainder are marked 'skipped' (a warning, not a
    failure). Cache hits don't count against the cap."""
    out = []
    fetched = 0
    for raw in raw_stories:
        url = raw["url"]
        story = dict(raw)
        story.setdefault("author", None)
        story.setdefault("text", None)
        story.setdefault("summary", None)
        if not fetch:
            story["fetch_status"] = "skipped"
            story["error"] = "fetching disabled"
            out.append(story)
            continue
        cached = _cache_get(url)
        if cached is not None:
            merged = dict(cached)
            # keep the richer related_tickers we inferred upstream
            merged["related_tickers"] = raw.get("related_tickers", [])
            out.append(merged)
            continue
        if fetched >= MAX_ARTICLES_PER_RUN:
            story["fetch_status"] = "skipped"
            story["error"] = "max articles per run reached"
            out.append(story)
            continue
        result = fetch_article(url)
        result["related_tickers"] = raw.get("related_tickers", [])
        # carry over the headline-list title if extraction didn't find one
        if not result.get("title"):
            result["title"] = raw.get("title")
        if not result.get("published_at"):
            result["published_at"] = raw.get("published_at")
        _cache_put(url, result)
        fetched += 1
        out.append(result)
    return out


# ---------------------------------------------------------------------------
# Top-level build + validation
# ---------------------------------------------------------------------------

def build_structured_data(market_data_text, fetch=True):
    """Parse market_data.md into the structured schema, fetching linked articles
    best-effort. Always returns a dict; never raises on a single bad section or
    article (those land in data_gaps / per-story errors)."""
    sections = _split_sections(market_data_text)

    live = _parse_live_ticker_dashboard(sections.get(1, ""))
    watchlist = [r["ticker"] for r in live]
    watchset = set(watchlist)
    key_levels = parse_key_levels(market_data_text)

    raw_stories = _collect_links(sections, watchset)
    news_stories = _fetch_stories(raw_stories, fetch)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session": _parse_session(market_data_text),
        "macro_dashboard": _parse_macro_dashboard(sections.get(2, "")),
        "live_ticker_dashboard": live,
        "watchlist": watchlist,
        "per_ticker_catalysts": _parse_per_ticker_catalysts(sections.get(3, "")),
        "options_positioning": _parse_options_positioning(sections.get(5, "")),
        "key_levels": key_levels,
        "news_stories": news_stories,
        "non_watchlist_mentions": _non_watchlist_mentions(sections, watchset),
        "potential_new_watchlist_candidates": [],
        "data_gaps": [],
    }
    data["data_gaps"] = _compute_data_gaps(data, sections)
    return data


def _compute_data_gaps(data, sections):
    gaps = []
    if not data["live_ticker_dashboard"]:
        gaps.append("live_ticker_dashboard empty or unparsed")
    if not data["macro_dashboard"]:
        gaps.append("macro_dashboard empty or unparsed")
    if not data["key_levels"]:
        gaps.append("key_levels (section 11) missing")
    else:
        missing = [t for t in data["watchlist"] if t not in data["key_levels"]]
        if missing:
            gaps.append("no key_levels row for: " + ", ".join(missing))
    if not data["news_stories"]:
        gaps.append("no news stories / links found")
    failed = sum(1 for s in data["news_stories"]
                 if s.get("fetch_status") != "success")
    if data["news_stories"] and failed == len(data["news_stories"]):
        gaps.append("no article bodies could be fetched (all paywalled/blocked)")
    if "no covered names reporting" in sections.get(6, "").lower():
        gaps.append("no earnings among covered names today")
    return gaps


def validate_structured(data):
    """Pre-LLM sanity check (amendment-aware). Returns a list of error strings;
    empty means OK. The caller falls back to the raw market_data flow on errors."""
    errors = []
    if not isinstance(data, dict):
        return ["structured data is not a dict"]
    if data.get("live_ticker_dashboard") and not data.get("watchlist"):
        errors.append("watchlist empty despite a live ticker dashboard")
    if not isinstance(data.get("news_stories"), list):
        errors.append("news_stories is not a list")
    else:
        for i, s in enumerate(data["news_stories"]):
            if not s.get("url"):
                errors.append(f"news_stories[{i}] missing url")
            if not s.get("fetch_status"):
                errors.append(f"news_stories[{i}] missing fetch_status")
    # tradeable tickers should have key levels where the table exists
    if data.get("key_levels"):
        no_levels = [t for t in data.get("watchlist", [])
                     if t not in data["key_levels"]]
        if no_levels and "data_gaps" in data and not any(
                "key_levels" in g for g in data["data_gaps"]):
            errors.append("tickers without key_levels not recorded in data_gaps")
    if "{{" in json.dumps(data):
        errors.append("unsubstituted {{...}} placeholder present in structured data")
    return errors


def write_structured_data(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MARKET_DATA
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_STRUCTURED
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[structure] cannot read {src}: {e}")
        return 2
    data = build_structured_data(text, fetch=True)
    write_structured_data(dst, data)
    errs = validate_structured(data)
    n_ok = sum(1 for s in data["news_stories"] if s.get("fetch_status") == "success")
    print(f"[structure] wrote {dst}")
    print(f"[structure] tickers={len(data['watchlist'])} "
          f"stories={len(data['news_stories'])} (fetched_ok={n_ok}) "
          f"non_watchlist_mentions={len(data['non_watchlist_mentions'])} "
          f"data_gaps={len(data['data_gaps'])}")
    if errs:
        print("[structure] validation warnings:")
        for e in errs:
            print(f"  - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
