import scorecard


def test_window_days():
    assert scorecard._window_days("intraday") == 1
    assert scorecard._window_days("swing 3-5d") == 5     # upper bound of range
    assert scorecard._window_days("7d") == 7
    assert scorecard._window_days(None) == scorecard._DEFAULT_WINDOW_DAYS


def test_return_pct():
    assert scorecard._return_pct(100, 120, "long") == 20.0
    assert scorecard._return_pct(100, 120, "short") == -20.0
    assert scorecard._return_pct(None, 120, "long") is None


def _idea(**kw):
    base = {
        "id": 1, "brief_date": "2026-05-20", "ticker": "X", "direction": "long",
        "entry": 100.0, "stop": 90.0, "target": 120.0, "timeframe": "5d",
    }
    base.update(kw)
    return base


def test_grade_idea_hit():
    bars = [("2026-05-20", 105, 99, 104), ("2026-05-21", 121, 110, 120)]
    status, price, _date, ret = scorecard.grade_idea(_idea(), bars)
    assert status == "hit" and price == 120.0 and ret == 20.0


def test_grade_idea_stopped_wins_ties():
    # single session whose range touches both stop and target -> conservative stop
    bars = [("2026-05-20", 121, 89, 95)]
    status, price, _date, ret = scorecard.grade_idea(_idea(), bars)
    assert status == "stopped" and price == 90.0 and ret == -10.0


def test_grade_idea_open_then_expired():
    # within window, no touch -> still open (None)
    assert scorecard.grade_idea(_idea(timeframe="5d"), [("d0", 105, 99, 104)]) is None
    # window elapsed (7 bars > 5), no touch -> expired at last in-horizon bar
    bars = [(f"d{i}", 105, 99, 104) for i in range(7)]
    status, _price, _date, _ret = scorecard.grade_idea(_idea(timeframe="5d"), bars)
    assert status == "expired"


def test_grade_idea_short():
    idea = _idea(direction="short", entry=100.0, stop=110.0, target=80.0)
    bars = [("d0", 105, 79, 82)]  # low 79 <= target 80 -> hit
    status, price, _date, ret = scorecard.grade_idea(idea, bars)
    assert status == "hit" and price == 80.0 and ret == 20.0
