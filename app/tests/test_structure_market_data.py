"""Tests for structure_market_data: section parsing, the schema shape, graceful
article fetching (no live network), caching, and structured validation."""
import json

import pytest
import requests

import structure_market_data as s

MARKET_DATA = """# Daily Market Data
Session: CLOSED · Holiday — market closed

## 1. Live Ticker Dashboard
```
TICKER  PRICE    VOL   PRE    1D    5D   52HI  52LO CAT
NVDA  $205.19  112M  +0.1% +0.2% +0.0% -12%  +44% NEWS
AMD   $511.57   31M  -2.3% +4.7% +9.7%  -5%  +340% UPG
```

## 2. Macro Dashboard
```
INDICATOR        LEVEL      1D
ES Futures    7,435.00  +0.53%
10Y Yield        4.49%  +0.54%
```

## 3. Per-Ticker Catalysts
*NVDA*
- `NEWS` 2026-06-14: [SpaceX IPO is here](https://example.com/spacex-ipo)
- `GRADE` 2026-06-12: Citigroup: Neutral → Buy

## 5. Options Positioning
_Market-wide_
SPY PCR (exp 2026-06-15): vol *0.84*, OI *1.45*

_Per-watchlist PCRs_
```
TICKER   VOL_PCR   OI_PCR   TOT_VOL   ATM_IV
NVDA        0.64     0.75    360.6K      26%
```

_Unusual volume_
- *AMD*: 2.3× avg (20.3K vs 8.9K)

## 6. Earnings Today
_no covered names reporting today_

## 8. Yahoo Finance Headlines
- [SpaceX fever tests the chip trade NVDA](https://example.com/spacex-chip)
- [Some unrelated PLTR PLTR story](https://example.com/pltr-story)

## 11. Key Levels
```
TICKER  PRICE   SMA20   SMA50    ATR    S20    R20    S55    R55 MAXPAIN  HOC  HOP GWALL
NVDA   205.19  214.46  206.70   8.33 199.34 232.01 164.08 236.26  207.50 210.00 165.00 210.00
```
"""


class _FakeResp:
    def __init__(self, status=200, text="", ctype="text/html"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = text
        self.headers = {"Content-Type": ctype}


def test_build_structured_no_fetch():
    d = s.build_structured_data(MARKET_DATA, fetch=False)
    assert d["watchlist"] == ["NVDA", "AMD"]
    assert d["session"]["status"] == "CLOSED"
    assert "ES Futures" in d["macro_dashboard"]
    assert d["key_levels"]["NVDA"]["r20"] == 232.01
    assert d["options_positioning"]["per_ticker"]["NVDA"]["vol_pcr"] == 0.64
    assert d["options_positioning"]["unusual_volume"][0]["ticker"] == "AMD"
    # NVDA catalyst with a link becomes a news story; GRADE (no link) does not.
    urls = [n["url"] for n in d["news_stories"]]
    assert "https://example.com/spacex-ipo" in urls
    # fetch disabled -> every story marked skipped
    assert all(n["fetch_status"] == "skipped" for n in d["news_stories"])
    # PLTR appears twice in a headline and isn't in the watchlist.
    assert any(m["ticker"] == "PLTR" for m in d["non_watchlist_mentions"])
    assert "no earnings among covered names today" in d["data_gaps"]


def test_related_tickers_inferred_from_headline():
    d = s.build_structured_data(MARKET_DATA, fetch=False)
    chip = next(n for n in d["news_stories"]
                if n["url"] == "https://example.com/spacex-chip")
    assert "NVDA" in chip["related_tickers"]


def test_fetch_article_success(monkeypatch):
    monkeypatch.setattr(s, "_robots_allows", lambda url: True)
    body = "<html><body>" + "<p>NVDA rose 5% to $205.19 today. </p>" * 20 + \
        "</body></html>"
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(text=body))
    story = s.fetch_article("https://example.com/ok")
    assert story["fetch_status"] == "success"
    assert story["text"] and len(story["text"]) <= s.MAX_ARTICLE_TEXT_CHARS
    assert story["summary"]


def test_fetch_article_timeout_is_failed(monkeypatch):
    monkeypatch.setattr(s, "_robots_allows", lambda url: True)

    def boom(*a, **k):
        raise requests.exceptions.Timeout()
    monkeypatch.setattr(requests, "get", boom)
    story = s.fetch_article("https://example.com/slow")
    assert story["fetch_status"] == "failed"
    assert "timeout" in story["error"].lower()


def test_fetch_article_paywall_is_failed(monkeypatch):
    monkeypatch.setattr(s, "_robots_allows", lambda url: True)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(status=403))
    story = s.fetch_article("https://wsj.com/paywalled")
    assert story["fetch_status"] == "failed"
    assert "403" in story["error"]


def test_fetch_article_thin_body_is_failed(monkeypatch):
    monkeypatch.setattr(s, "_robots_allows", lambda url: True)
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _FakeResp(text="<p>tiny</p>"))
    story = s.fetch_article("https://example.com/thin")
    assert story["fetch_status"] == "failed"
    assert "paywall" in story["error"].lower()


def test_cache_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "CACHE_DIR", tmp_path)
    article = {"url": "https://example.com/x", "fetch_status": "success",
               "text": "hello"}
    s._cache_put("https://example.com/x", article)
    got = s._cache_get("https://example.com/x")
    assert got == article
    assert s._cache_get("https://example.com/never") is None


def test_validate_structured_flags_empty_watchlist():
    bad = {"live_ticker_dashboard": [{"ticker": "NVDA"}], "watchlist": [],
           "news_stories": [], "key_levels": {}, "data_gaps": []}
    errs = s.validate_structured(bad)
    assert any("watchlist empty" in e for e in errs)


def test_validate_structured_clean():
    d = s.build_structured_data(MARKET_DATA, fetch=False)
    assert s.validate_structured(d) == []


def test_structured_json_serializable():
    d = s.build_structured_data(MARKET_DATA, fetch=False)
    json.dumps(d)  # must not raise
