import core


def test_normalize_ticker_basic():
    assert core.normalize_ticker("$aapl ") == "AAPL"
    assert core.normalize_ticker("brk.b") == "BRK.B"
    assert core.normalize_ticker("^vix") == "^VIX"


def test_normalize_ticker_rejects_garbage():
    assert core.normalize_ticker("123") is None        # digits not allowed
    assert core.normalize_ticker("a b") is None        # space
    assert core.normalize_ticker("toolongticker") is None  # >10 chars
    assert core.normalize_ticker("") is None


def test_escape_md():
    assert core.escape_md("plain text") == "plain text"
    assert core.escape_md("a_b") == r"a\_b"
    assert core.escape_md("x*y`z[w") == r"x\*y\`z\[w"
