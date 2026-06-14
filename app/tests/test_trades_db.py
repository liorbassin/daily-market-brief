import trades_db


def _card(ticker, direction, entry, stop, target):
    return {
        "ticker": ticker, "direction": direction, "conviction": "high",
        "timeframe": "swing 3d", "entry": entry, "stop": stop, "target": target,
        "thesis": "x", "in_watchlist": True,
    }


def test_record_is_idempotent_and_skips_avoid(tmp_path, monkeypatch):
    monkeypatch.setattr(trades_db, "DB_PATH", tmp_path / "trades.db")
    trades_db.init()
    cards = [
        _card("NVDA", "long", 100.0, 90.0, 120.0),
        _card("GOOG", "avoid", None, None, None),
    ]
    assert trades_db.record_ideas("2026-05-20", cards) == 2
    assert trades_db.record_ideas("2026-05-20", cards) == 0   # UNIQUE → no-op

    openi = trades_db.open_ideas()
    assert len(openi) == 1 and openi[0]["ticker"] == "NVDA"   # avoid → skipped


def test_mark_outcome_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(trades_db, "DB_PATH", tmp_path / "t.db")
    trades_db.init()
    trades_db.record_ideas("2026-05-20", [_card("NVDA", "long", 100.0, 90.0, 120.0)])

    idea = trades_db.open_ideas()[0]
    trades_db.mark_outcome(idea["id"], "hit", 120.0, "2026-05-22", 20.0)

    summary = trades_db.summary_since("2026-05-01")
    assert summary["resolved"] == 1
    assert summary["wins"] == 1
    assert summary["win_rate"] == 100.0
    assert abs(summary["avg_return"] - 20.0) < 1e-9
