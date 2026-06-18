"""
validate_briefing.py
=====================
Post-generation compliance checker for the daily brief. Run it after
generate_briefing.py to catch the two failure modes the prompt is designed
to avoid:

  1. HALLUCINATED TRADE LEVELS — every Entry/Stop/Tgt in the 🎯 TRADE IDEAS
     section must be a value that exists in that ticker's row of the
     "Key Levels" table (section 11 of market_data.md), or be derivable as
     a level ± a multiple of that ticker's ATR. Anything else is the model
     inventing a price, which is exactly what the grounding rules forbid.

  2. MARKDOWN NOISE — the briefing is delivered as PLAIN TEXT, so any
     leftover markdown (*, #, ```, ---, "* " bullets) shows up as literal
     characters to the user. These are reported as warnings.

Usage:
  python validate_briefing.py
  python validate_briefing.py <briefing.md> <market_data.md>

Exit codes:
  0 — no hallucinated levels (markdown warnings, if any, are non-fatal)
  1 — at least one trade level could not be grounded in the Key Levels table
  2 — could not read an input file
"""

import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent
DEFAULT_BRIEFING = APP_DIR / "final_briefing.md"
DEFAULT_MARKET_DATA = APP_DIR / "market_data.md"

# Column order of the Key Levels table emitted by market_brief.get_key_levels.
# Must stay in sync with that table's header (incl. the option-implied columns)
# so a trade grounded on MAXPAIN/HOC/HOP/GWALL isn't falsely flagged as invented.
_LEVEL_COLS = ("price", "sma20", "sma50", "atr", "s20", "r20", "s55", "r55",
               "maxpain", "hoc", "hop", "gwall")

# A ticker token: uppercase start, then letters/digits/.^= (matches
# core.normalize_ticker's shape). Caps length so prose words don't match.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\^=]{0,9}$")
_TICKER_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9.\^=]{0,9}\b")

# Entry/Stop/Tgt(arget) followed by a number (optional $, commas, decimals).
_LEVEL_LINE_RE = re.compile(
    r"(entry|stop|tgt|target)\b\s*[:=]?\s*\$?([0-9][0-9,]*\.?[0-9]*)",
    re.IGNORECASE,
)

# ATR multiples the prompt is allowed to use for derived stops/targets.
_ATR_MULTIPLES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

# Trade-card header parsing (the "🟢 TICKER · LONG · swing 3-5d · HIGH" line).
_DIRECTION_EMOJI = {"🟢": "long", "🔴": "short", "⚪": "avoid"}
_DIRECTION_WORD_RE = re.compile(r"\b(long|short|avoid)\b", re.IGNORECASE)
_CONVICTION_RE = re.compile(r"\b(high|medium|med|low)\b", re.IGNORECASE)
_TIMEFRAME_RE = re.compile(
    r"(intraday|swing[^\n·|]*|\b\d+\s*-\s*\d+\s*d\b|\b\d+\s*d\b)", re.IGNORECASE
)
_WHY_RE = re.compile(r"^\s*why\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_key_levels(market_data):
    """Parse section 11's table into {ticker: {col: float|None}}. Returns {}
    if the section is missing."""
    head = re.search(r"^##\s*11\.\s*Key Levels.*$", market_data, re.MULTILINE)
    if not head:
        return {}
    rest = market_data[head.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    block = rest[:nxt.start()] if nxt else rest

    out = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("```") or line.startswith("TICKER"):
            continue
        parts = line.split()
        ticker = parts[0]
        if not _TICKER_RE.match(ticker):
            continue
        row = {}
        for col, raw in zip(_LEVEL_COLS, parts[1:1 + len(_LEVEL_COLS)]):
            try:
                row[col] = float(raw.replace(",", ""))
            except ValueError:
                row[col] = None
        out[ticker] = row
    return out


def extract_trade_section(briefing):
    """Return the text of the 🎯 TRADE IDEAS section (up to the next top-level
    section header), or '' when it's absent."""
    head = re.search(r"^🎯.*$", briefing, re.MULTILINE)
    if not head:
        return ""
    rest = briefing[head.end():]
    nxt = re.search(r"^(📰|📈|⚠️).*$", rest, re.MULTILINE)
    section = rest[:nxt.start()] if nxt else rest
    return section.replace("[[SPLIT]]", "")


def parse_trade_cards(briefing, level_tickers=None):
    """Parse the 🎯 TRADE IDEAS section into structured card dicts:
        {ticker, direction, conviction, timeframe, thesis,
         entry, stop, target, in_watchlist}

    Shared by the validator (grounding check) and scorecard.ingest (DB logging)
    so both agree on card structure. A block counts as a card if it carries a
    direction cue (🟢/🔴/⚪ or the word long/short/avoid) OR an Entry/Stop/Tgt
    line — which skips the eligibility prose and disclaimer. `level_tickers`
    (the set of tickers in the Key Levels table) resolves the card's ticker and
    sets in_watchlist; out-of-watchlist ideas are still captured by falling back
    to the first ticker-shaped token. Missing fields come back as None."""
    section = extract_trade_section(briefing)
    cards = []
    if not section.strip():
        return cards
    level_tickers = level_tickers or set()

    for block in re.split(r"\n\s*\n", section):
        block = block.strip()
        if not block:
            continue
        has_dir = bool(_DIRECTION_WORD_RE.search(block)) or any(
            emo in block for emo in _DIRECTION_EMOJI
        )
        if not has_dir and not _LEVEL_LINE_RE.search(block):
            continue  # prose / disclaimer, not a trade card

        tokens = _TICKER_TOKEN_RE.findall(block)
        ticker = next((t for t in tokens if t in level_tickers), None)
        if ticker is None:
            ticker = tokens[0] if tokens else None
        if ticker is None:
            continue

        header = block.splitlines()[0]

        direction = next(
            (d for emo, d in _DIRECTION_EMOJI.items() if emo in header), None
        )
        if direction is None:
            wm = _DIRECTION_WORD_RE.search(block)
            direction = wm.group(1).lower() if wm else None

        cm = _CONVICTION_RE.search(header) or _CONVICTION_RE.search(block)
        conviction = None
        if cm:
            conviction = cm.group(1).lower()
            conviction = "medium" if conviction == "med" else conviction

        tm = _TIMEFRAME_RE.search(header)
        timeframe = tm.group(1).strip() if tm else None

        wh = _WHY_RE.search(block)
        thesis = wh.group(1).strip() if wh else None

        entry = stop = target = None
        for label, raw in _LEVEL_LINE_RE.findall(block):
            value = float(raw.replace(",", ""))
            lab = label.lower()
            if lab == "entry":
                entry = value
            elif lab == "stop":
                stop = value
            elif lab in ("tgt", "target"):
                target = value

        cards.append({
            "ticker": ticker,
            "direction": direction,
            "conviction": conviction,
            "timeframe": timeframe,
            "thesis": thesis,
            "entry": entry,
            "stop": stop,
            "target": target,
            "in_watchlist": (
                ticker in level_tickers and "not in watchlist" not in block.lower()
            ),
        })
    return cards


# ---------------------------------------------------------------------------
# Level matching
# ---------------------------------------------------------------------------

def _close(a, b, rel=0.004, absolute=0.02):
    """True if a ~= b within a small relative/absolute tolerance (allows the
    model to round 297.46 to 297.5, etc.)."""
    return abs(a - b) <= max(absolute, rel * abs(b))


def match_level(value, row):
    """Return a human label for what `value` matches in a Key Levels row
    ('SMA20', 'S20', 'PRICE-1.0ATR', …), or None if it matches nothing —
    i.e. the model invented it.

    Collects ALL levels within tolerance and returns the closest one, so a
    value sitting near two adjacent levels gets the accurate label. Raw named
    levels are preferred over ATR-derived ones on a tie (rank 0 vs 1)."""
    candidates = []  # (distance, rank, label)
    named = {k: v for k, v in row.items() if k != "atr" and v is not None}
    for name, v in named.items():
        if _close(value, v):
            candidates.append((abs(value - v), 0, name.upper()))
    atr = row.get("atr")
    if atr:
        for base_name, base in named.items():
            for mult in _ATR_MULTIPLES:
                for sign, sym in ((1, "+"), (-1, "-")):
                    target = base + sign * mult * atr
                    if _close(value, target):
                        candidates.append(
                            (abs(value - target), 1, f"{base_name.upper()}{sym}{mult}ATR")
                        )
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def validate_trades(briefing, levels):
    """Grade each trade card's Entry/Stop/Tgt against the Key Levels table.
    Returns per-card dicts {ticker, status, checks} where status is 'ok' / 'n/a'
    and checks is [(label, value, matched_label_or_None), ...]. Cards with no
    numeric levels (the allowed "n/a (no level data)" case) come back 'n/a'."""
    results = []
    for card in parse_trade_cards(briefing, set(levels)):
        present = [
            (label, card[key])
            for label, key in (("entry", "entry"), ("stop", "stop"), ("tgt", "target"))
            if card[key] is not None
        ]
        if not present:
            results.append({"ticker": card["ticker"], "status": "n/a", "checks": []})
            continue
        row = levels.get(card["ticker"], {})
        checks = [(label, value, match_level(value, row)) for label, value in present]
        results.append({"ticker": card["ticker"], "status": "ok", "checks": checks})
    return results


# ---------------------------------------------------------------------------
# Markdown-noise check
# ---------------------------------------------------------------------------

def markdown_warnings(briefing):
    """Return [(line_no, reason), ...] for leftover markdown that would show
    as literal characters in the plain-text Telegram channel."""
    warns = []
    for i, line in enumerate(briefing.splitlines(), 1):
        s = line.strip()
        if re.match(r"#{1,6}\s", s):
            warns.append((i, "markdown header '#'"))
        if "```" in line:
            warns.append((i, "code fence '```'"))
        if re.match(r"(\*\*\*|---|___)\s*$", s):
            warns.append((i, "horizontal rule"))
        if re.search(r"\*[^*\n]+\*", line):
            warns.append((i, "bold/italic '*...*'"))
        if re.match(r"\*\s", s):
            warns.append((i, "literal '* ' bullet (use •)"))
    return warns


# ---------------------------------------------------------------------------
# Output-structure validation (the contract the LLM must satisfy)
# ---------------------------------------------------------------------------

# The 8 required top-level section headers, in order. The bot keys routing off
# the leading emoji, so presence of the emoji is what we check.
REQUIRED_SECTIONS = (
    ("🧭", "BOTTOM LINE"),
    ("🔥", "EXECUTIVE MARKET READ"),
    ("📊", "MACRO DASHBOARD"),
    ("👀", "WATCHLIST DASHBOARD"),
    ("🎯", "TRADE IDEAS"),
    ("📰", "HIGHEST RELEVANCE STORIES"),
    ("📈", "POTENTIAL NEW WATCHLIST CANDIDATES"),
    ("⚠️", "KEY RISKS"),
)
_SECTION_EMOJIS = tuple(emo for emo, _ in REQUIRED_SECTIONS)

_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
_THINK_RE = re.compile(r"</?think\b|</?reasoning\b", re.IGNORECASE)
# A [[SPLIT]] marker line (mirrors core._split_marker handling; kept local so
# this module imports without pulling core's env-loading side effects).
_SPLIT_MARKER_RE = re.compile(r"^\s*\[\[SPLIT\]\]\s*$", re.MULTILINE)
# A well-formed watchlist entry line: bullet + ticker, nothing else.
_WATCHLIST_OK_RE = re.compile(r"^•\s*[A-Z][A-Z0-9.\^=]{0,9}$")

_MAX_SEGMENT = 4096      # Telegram hard limit -> fatal if exceeded
_WARN_SEGMENT = 3500     # target ceiling -> warning if exceeded


def check_required_sections(briefing):
    return [f"missing required section: {emo} {name}"
            for emo, name in REQUIRED_SECTIONS if emo not in briefing]


def check_no_placeholders(briefing):
    found = sorted(set(_PLACEHOLDER_RE.findall(briefing)))
    return [f"unsubstituted placeholder token: {tok}" for tok in found]


def check_think_leakage(briefing):
    return (["<think>/reasoning tags leaked into the briefing"]
            if _THINK_RE.search(briefing) else [])


def _section_body(briefing, emoji):
    """Text of the section that starts with `emoji`, up to the next top-level
    section header (or end). Excludes the header line itself."""
    head = re.search(rf"^{re.escape(emoji)}.*$", briefing, re.MULTILINE)
    if not head:
        return ""
    rest = briefing[head.end():]
    others = [e for e in _SECTION_EMOJIS if e != emoji]
    nxt = re.search("^(" + "|".join(re.escape(e) for e in others) + ").*$",
                    rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def _watchlist_section(briefing):
    return _section_body(briefing, "👀")


def check_watchlist_ticker_lines(briefing):
    """Every bulleted watchlist line must be exactly '• TICKER'."""
    errors = []
    for line in _watchlist_section(briefing).splitlines():
        s = line.rstrip()
        if s.startswith("•") and not _WATCHLIST_OK_RE.match(s):
            errors.append(f"malformed watchlist ticker line: {s!r} "
                          f"(must be '• TICKER' only)")
    return errors


def check_watchlist_content(briefing, watchlist_tickers=None):
    """The 👀 WATCHLIST DASHBOARD must contain ticker cards: at least one line
    that is exactly '• TICKER'. Reject '* TICKER' / '1. TICKER' bullets and
    (when a structured watchlist is known) tickers not in that universe."""
    errors = []
    watchlist_tickers = set(watchlist_tickers or [])
    section = _watchlist_section(briefing)
    if not section.strip():
        return ["WATCHLIST DASHBOARD section is empty (no ticker cards)"]

    valid = []
    for line in section.splitlines():
        s = line.rstrip()
        m = _WATCHLIST_OK_RE.match(s)
        if m:
            valid.append(s.lstrip("• ").strip())
        elif re.match(r"^\*\s+[A-Z]", s):
            errors.append(f"watchlist uses a markdown '* ' bullet: {s!r} (use '• TICKER')")
        elif re.match(r"^\d+\.\s+[A-Z]", s):
            errors.append(f"watchlist uses a numbered bullet: {s!r} (use '• TICKER')")

    if not valid:
        errors.append("WATCHLIST DASHBOARD has no valid '• TICKER' cards")
        return errors

    if watchlist_tickers:
        invented = [t for t in valid if t not in watchlist_tickers]
        if invented:
            errors.append("WATCHLIST DASHBOARD lists ticker(s) not in the "
                          f"structured watchlist: {', '.join(sorted(set(invented)))}")
    return errors


def check_split_segments(briefing):
    """Return (fatal, warnings) for [[SPLIT]] sizing/placement."""
    fatal, warnings = [], []
    segments = [p.strip("\n") for p in _SPLIT_MARKER_RE.split(briefing) if p.strip()]
    for i, seg in enumerate(segments, 1):
        n = len(seg)
        if n > _MAX_SEGMENT:
            fatal.append(f"segment {i} is {n} chars (> {_MAX_SEGMENT} Telegram limit)")
        elif n > _WARN_SEGMENT:
            warnings.append(f"segment {i} is {n} chars (> {_WARN_SEGMENT} target)")
    # A marker should be followed by a top-level section header.
    for m in _SPLIT_MARKER_RE.finditer(briefing):
        after = briefing[m.end():].lstrip("\n")
        nxt = after.splitlines()[0] if after.splitlines() else ""
        if nxt and not nxt.startswith(_SECTION_EMOJIS):
            warnings.append("a [[SPLIT]] marker is not at a top-level section "
                            f"boundary (before: {nxt[:40]!r})")
    return fatal, warnings


def check_trade_grounding(briefing, levels):
    """Fatal error per Entry/Stop/Tgt that isn't grounded in Key Levels."""
    errors = []
    for card in validate_trades(briefing, levels):
        for label, value, matched in card["checks"]:
            if matched is None:
                errors.append(f"{card['ticker']}: {label} {value:g} not grounded "
                              f"in Key Levels (invented price)")
    return errors


def check_trade_directions(briefing, levels):
    """LONG -> stop < entry < target; SHORT -> stop > entry > target."""
    errors = []
    for card in parse_trade_cards(briefing, set(levels)):
        d, e, s, t = (card["direction"], card["entry"],
                      card["stop"], card["target"])
        if d == "long" and None not in (e, s, t):
            if not (s < e < t):
                errors.append(f"{card['ticker']} LONG: expected stop<entry<target, "
                              f"got stop={s:g} entry={e:g} target={t:g}")
        elif d == "short" and None not in (e, s, t):
            if not (s > e > t):
                errors.append(f"{card['ticker']} SHORT: expected stop>entry>target, "
                              f"got stop={s:g} entry={e:g} target={t:g}")
    return errors


# ---------------------------------------------------------------------------
# Quality contract (the SKILL.md format/grounding rules, enforced strictly)
# ---------------------------------------------------------------------------

NO_SETUP_LINE = "No high-conviction setups in today's data."

_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(\s[^>]*)?>")

# Required macro items (SKILL.md) -> keyword alternatives used to detect both
# whether the structured data carries the item and whether the briefing mentions
# it. Word-ish substrings, matched case-insensitively within the macro section.
_MACRO_REQUIRED = (
    ("S&P futures", ("s&p", "es ", "spx", "e-mini")),
    ("Nasdaq futures", ("nasdaq", "nq ")),
    ("10Y Treasury yield", ("10y", "10-year", "10 year", "tnx", "ten-year")),
    ("VIX", ("vix",)),
    ("Dollar", ("dollar", "dxy", "greenback")),
    ("Oil", ("oil", "wti", "brent", "crude")),
    ("Bitcoin", ("bitcoin", "btc")),
)
# Off-contract macro lines that signal the model replaced the dashboard with
# generic macro it invented. Fatal only when absent from the supplied data.
_MACRO_OFF_CONTRACT = ("fed funds", "leading indicators", "inflation")

# Generic/technical terms the model tends to hallucinate. Fatal unless the exact
# term is present in the structured data (it never is for the invented ones).
_UNSUPPORTED_TERMS = (
    "macd", "rsi", "overbought", "oversold", "divergence", "call spread",
    "put spread", "options play", "institutional support",
    "earnings beat expectations",
)


def check_markdown_fatal(briefing):
    """Plain-text Telegram channel: ANY markdown/HTML markup is fatal for local
    models. Deduped to one error per kind so the repair list stays readable."""
    errors, seen = [], set()

    def add(reason):
        if reason not in seen:
            seen.add(reason)
            errors.append(reason)

    for line in briefing.splitlines():
        s = line.strip()
        if "```" in line:
            add("markdown code fence ``` present (plain text only)")
        if re.match(r"#{1,6}\s", s):
            add("markdown header '#' present (plain text only)")
        if re.match(r"(\*\*\*|---|___)\s*$", s):
            add("markdown horizontal rule present")
        if re.search(r"\*\*[^*\n]+\*\*", line) or re.search(r"\*[^*\n]+\*", line):
            add("markdown bold/italic '*' present (use ALL-CAPS, not markdown)")
        if re.match(r"\*\s", s):
            add("markdown '* ' bullet present (use '•')")
        if s.count("|") >= 2:
            add("markdown table pipes '|' present (use card layout, not tables)")
        if _HTML_TAG_RE.search(line):
            add("HTML tag present (plain text only)")
    return errors


def check_macro_dashboard(briefing, structured_data=None):
    """MACRO DASHBOARD must include the required SKILL.md items that ARE present
    in the supplied macro data, and must not substitute off-contract items."""
    section = _section_body(briefing, "📊")
    if not section.strip():
        return ["MACRO DASHBOARD section is empty"]
    low = section.lower()

    require_all = not isinstance(structured_data, dict)
    macro_keys = ""
    if isinstance(structured_data, dict):
        macro_keys = " ".join((structured_data.get("macro_dashboard") or {})
                              .keys()).lower()
    blob = (json.dumps(structured_data, ensure_ascii=False).lower()
            if structured_data else "")

    errors = []
    for concept, kws in _MACRO_REQUIRED:
        available = require_all or any(k in macro_keys for k in kws)
        if available and not any(k in low for k in kws):
            errors.append(f"MACRO DASHBOARD missing required item: {concept}")
    for phrase in _MACRO_OFF_CONTRACT:
        if phrase in low and phrase not in blob:
            errors.append("MACRO DASHBOARD contains off-contract item not in "
                          f"supplied data: {phrase!r}")
    return errors


def check_trade_ideas_format(briefing):
    """TRADE IDEAS must be EITHER the exact no-setup line OR one+ valid cards
    (direction-emoji header + Entry/Stop/Tgt, or the explicit n/a line)."""
    section = extract_trade_section(briefing)
    if not section.strip():
        return ["TRADE IDEAS section is empty"]
    if NO_SETUP_LINE in section:
        return []  # Case A

    errors = []
    if any(re.match(r"^\s*\d+\.\s+\S", ln) for ln in section.splitlines()):
        errors.append("TRADE IDEAS uses numbered-list format; use trade cards "
                      f"or the exact line: {NO_SETUP_LINE!r}")

    valid_cards = 0
    for block in re.split(r"\n\s*\n", section):
        b = block.strip()
        if not b:
            continue
        header = b.splitlines()[0]
        has_emoji = any(emo in header for emo in _DIRECTION_EMOJI)
        has_levels = bool(_LEVEL_LINE_RE.search(b))
        has_na = "n/a (no level data)" in b.lower()
        looks_like_idea = (has_emoji or has_levels
                           or _DIRECTION_WORD_RE.search(b))
        if not looks_like_idea:
            continue  # eligibility prose / disclaimer
        if not has_emoji:
            errors.append("trade idea not in card format (missing 🟢/🔴/⚪ "
                          f"header): {header[:60]!r}")
            continue
        labels = {lab.lower() for lab, _ in _LEVEL_LINE_RE.findall(b)}
        has_triple = ({"entry", "stop"} <= labels
                      and ("tgt" in labels or "target" in labels))
        if not (has_triple or has_na):
            errors.append("trade idea missing 'Entry <p>  Stop <p>  Tgt <p>' "
                          f"(or 'Entry/Stop/Tgt: n/a (no level data)'): {header[:60]!r}")
            continue
        valid_cards += 1

    if valid_cards == 0 and not errors:
        errors.append("TRADE IDEAS has neither valid trade cards nor the exact "
                      f"line: {NO_SETUP_LINE!r}")
    return errors


def check_unsupported_language(briefing, structured_blob=""):
    """Reject hallucinated technical/generic terms not present in the data."""
    low = briefing.lower()
    blob = structured_blob or ""
    errors = []
    for term in _UNSUPPORTED_TERMS:
        if re.search(r"\b" + re.escape(term) + r"\b", low) and term not in blob:
            errors.append(f"unsupported/invented term not backed by data: {term!r}")
    return errors


def check_bottom_line(briefing):
    """BOTTOM LINE must be 2-3 short lines incl. a 'Top idea:' line whose
    ticker/prices appear in TRADE IDEAS (or 'Top idea: none …' when no setup)."""
    section = _section_body(briefing, "🧭")
    body = [ln.strip() for ln in section.splitlines() if ln.strip()]
    errors = []
    if not 2 <= len(body) <= 3:
        errors.append(f"BOTTOM LINE must be 2-3 short lines, found {len(body)}")

    top = next((ln for ln in body if "top idea" in ln.lower()), None)
    if not top:
        errors.append("BOTTOM LINE missing a 'Top idea:' line")
        return errors

    trade_section = extract_trade_section(briefing)
    no_setup = NO_SETUP_LINE in trade_section
    is_none = bool(re.search(r"top idea:\s*none", top, re.IGNORECASE))

    if no_setup and not is_none:
        errors.append("BOTTOM LINE must say 'Top idea: none — no high-conviction "
                      "setup today.' when TRADE IDEAS has no setups")
    if is_none:
        return errors

    for tok in _TICKER_TOKEN_RE.findall(top):
        if tok in ("TICKER", "LONG", "SHORT") or len(tok) < 2:
            continue
        if tok not in trade_section:
            errors.append(f"BOTTOM LINE top idea references {tok}, absent from "
                          "TRADE IDEAS")
    for num in re.findall(r"\d+\.\d+", top):
        if num not in trade_section:
            errors.append(f"BOTTOM LINE top idea price {num} not present in "
                          "TRADE IDEAS")
    return errors


def validate_quality_contract(briefing, structured_data=None):
    """Strict SKILL.md quality gate. Returns a list of FATAL error strings:
    markdown markup, missing/empty watchlist cards, wrong MACRO items, non-card
    or level-less trade ideas, hallucinated indicators, and BOTTOM LINE format.
    `structured_data` (the parsed market_data_structured.json dict) tightens the
    watchlist/macro/language checks; None makes them maximally strict."""
    watchlist = (structured_data or {}).get("watchlist", []) \
        if isinstance(structured_data, dict) else []
    blob = (json.dumps(structured_data, ensure_ascii=False).lower()
            if structured_data else "")

    errors = []
    errors += check_markdown_fatal(briefing)
    errors += check_watchlist_content(briefing, watchlist)
    errors += check_macro_dashboard(briefing, structured_data)
    errors += check_trade_ideas_format(briefing)
    errors += check_unsupported_language(briefing, blob)
    errors += check_bottom_line(briefing)
    return errors


def validate_all(briefing, market_data, structured_data=None):
    """Run every output check and split results into (fatal_errors, warnings).

    FATAL (triggers the repair pass): empty output, <think> leakage, missing
    required sections, leftover {{...}} placeholders, malformed/empty WATCHLIST
    cards, ANY markdown/HTML markup, wrong MACRO items, non-card or level-less
    TRADE IDEAS, hallucinated indicators, bad BOTTOM LINE, ungrounded/invented
    trade levels, impossible LONG/SHORT ordering, and any Telegram segment over
    the 4096 hard limit.

    WARNINGS (logged, do NOT block delivery): segments over the 3500 target but
    under 4096, and off-boundary [[SPLIT]] markers."""
    fatal, warnings = [], []
    if not briefing or not briefing.strip():
        return ["briefing is empty"], warnings

    levels = parse_key_levels(market_data) if market_data else {}

    fatal += check_think_leakage(briefing)
    fatal += check_required_sections(briefing)
    fatal += check_no_placeholders(briefing)
    fatal += check_watchlist_ticker_lines(briefing)
    fatal += check_trade_grounding(briefing, levels)
    fatal += check_trade_directions(briefing, levels)
    fatal += validate_quality_contract(briefing, structured_data)

    seg_fatal, seg_warn = check_split_segments(briefing)
    fatal += seg_fatal
    warnings += seg_warn

    fatal = list(dict.fromkeys(fatal))  # de-dupe, preserve order
    return fatal, warnings


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main():
    briefing_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BRIEFING
    market_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MARKET_DATA

    try:
        briefing = briefing_path.read_text(encoding="utf-8")
        market_data = market_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[validate] cannot read input: {e}")
        return 2

    levels = parse_key_levels(market_data)
    print(f"=== Briefing validation: {briefing_path.name} ===")
    print(f"Key Levels parsed for {len(levels)} ticker(s).")

    # 1. Trade-level grounding
    print("\n-- Trade-level grounding --")
    results = validate_trades(briefing, levels)
    flagged = 0
    grounded = 0
    if not results:
        print("  (no 🎯 TRADE IDEAS cards found — nothing to ground)")
    for card in results:
        if card["status"] == "n/a":
            print(f"  {card['ticker']}: n/a (no level data) — OK")
            continue
        bad = [c for c in card["checks"] if c[2] is None]
        rendered = ", ".join(
            f"{lbl} {val:g} [{matched or 'UNMATCHED'}]"
            for lbl, val, matched in card["checks"]
        )
        if bad:
            flagged += 1
            print(f"  {card['ticker']}: {rendered}  -> FLAG")
        else:
            grounded += 1
            print(f"  {card['ticker']}: {rendered}  -> OK")

    # 2. Markdown noise
    print("\n-- Markdown-noise check (plain-text channel) --")
    warns = markdown_warnings(briefing)
    if not warns:
        print("  clean — no literal markdown found")
    else:
        for line_no, reason in warns:
            print(f"  [warn] line {line_no}: {reason}")

    # 3. Output-structure + quality check (fatal vs warning split)
    print("\n-- Output-structure + quality check --")
    structured = None
    structured_path = APP_DIR / "market_data_structured.json"
    if structured_path.exists():
        try:
            structured = json.loads(structured_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            structured = None
    fatal, warnings = validate_all(briefing, market_data, structured)
    if not fatal:
        print("  no fatal structural errors")
    for e in fatal:
        print(f"  [FATAL] {e}")
    for w in warnings:
        print(f"  [warn]  {w}")

    print("\n-- Summary --")
    print(f"  trade cards: {grounded} grounded, {flagged} flagged, "
          f"{sum(1 for c in results if c['status'] == 'n/a')} n/a")
    print(f"  fatal errors: {len(fatal)}, warnings: {len(warnings)}")

    if fatal:
        print(f"  RESULT: FAIL ({len(fatal)} fatal error(s))")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
