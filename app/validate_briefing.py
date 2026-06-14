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

    print("\n-- Summary --")
    print(f"  trade cards: {grounded} grounded, {flagged} flagged, "
          f"{sum(1 for c in results if c['status'] == 'n/a')} n/a")
    print(f"  markdown warnings: {len(warns)}")

    if flagged:
        print("  RESULT: FAIL (hallucinated trade level(s))")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
