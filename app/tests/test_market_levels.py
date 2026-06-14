from datetime import datetime, timedelta

import pandas as pd

import market_brief as mb


def test_format_volume():
    assert mb._format_volume(1_234) == "1.2K"
    assert mb._format_volume(1_500_000) == "1.5M"
    assert mb._format_volume(2_000_000_000) == "2.0B"
    assert mb._format_volume(950) == "950"


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5])
    assert mb._sma(s, 5) == 3.0
    assert mb._sma(s, 10) is None  # too few rows


def test_atr_positive():
    n = 16
    data = pd.DataFrame({
        "High": [10 + i for i in range(n)],
        "Low": [9 + i for i in range(n)],
        "Close": [9.5 + i for i in range(n)],
    })
    atr = mb._atr(data, 14)
    assert atr is not None and atr > 0


def test_option_magnet_levels():
    calls = {100: 50, 110: 200, 120: 30}
    puts = {90: 40, 100: 300, 110: 20}
    max_pain, hoc, hop = mb._option_magnet_levels(calls, puts)
    assert hoc == 110            # highest call OI
    assert hop == 100            # highest put OI
    assert max_pain == 100       # minimizes total holder value
    assert mb._option_magnet_levels({}, {}) == (None, None, None)


def test_classify_grade():
    assert mb._classify_grade({"action": "upgrade"}) == "UPG"
    assert mb._classify_grade({"action": "downgrade"}) == "DOWN"
    assert mb._classify_grade({"newGrade": "Buy", "previousGrade": "Hold"}) == "UPG"


def test_within_catalyst_window():
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    assert mb._within_catalyst_window(recent) is True
    assert mb._within_catalyst_window(old) is False
    assert mb._within_catalyst_window("") is False
