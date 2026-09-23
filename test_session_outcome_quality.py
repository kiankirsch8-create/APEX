"""Tests for session tagging and net-P&L outcome."""
from __future__ import annotations

import continuous_backtester as cb


def test_sessions_for_utc_hour_overlaps_and_primary() -> None:
    primary, sessions = cb.sessions_for_utc_hour(8)
    assert "tokyo" in sessions and "london" in sessions
    assert primary in sessions
    # 08:00 is equidistant from tokyo mid 04:30 and london mid 11:30 — later wins.
    assert primary == "london"

    primary_ny, sessions_ny = cb.sessions_for_utc_hour(14)
    assert sessions_ny == ["london", "new_york"]
    # 14:00 equidistant from london/new_york midpoints — later wins.
    assert primary_ny == "new_york"

    primary_syd, sessions_syd = cb.sessions_for_utc_hour(23)
    assert sessions_syd == ["sydney"]
    assert primary_syd == "sydney"

    primary_tok, sessions_tok = cb.sessions_for_utc_hour(1)
    assert "sydney" in sessions_tok and "tokyo" in sessions_tok
    assert primary_tok == "sydney"

    # Clear nearer-midpoint cases (no tie).
    assert cb.sessions_for_utc_hour(5)[0] == "tokyo"
    assert cb.sessions_for_utc_hour(10)[0] == "london"
    assert cb.sessions_for_utc_hour(18)[0] == "new_york"


def test_daily_weekly_session_is_na() -> None:
    for tf in ("1d", "1w", "1D", "1W"):
        tags = cb.session_tag_fields(timeframe=tf, analysis_date="2024-03-01")
        assert tags["session"] == "n/a"
        assert tags["sessions"] == []
        assert tags["entry_hour_utc"] is None


def test_intraday_session_uses_entry_hour() -> None:
    tags = cb.session_tag_fields(
        timeframe="1h",
        analysis_date="2024-03-01",
        entry_hour_utc=14,
    )
    assert tags["entry_hour_utc"] == 14
    assert tags["sessions"] == ["london", "new_york"]
    assert tags["session"] == "new_york"

    tags_4h = cb.session_tag_fields(
        timeframe="4h",
        entry_hour_utc=23,
    )
    assert tags_4h["session"] == "sydney"
    assert tags_4h["sessions"] == ["sydney"]

    # Intraday without a recoverable hour stays untaggable — no invented default.
    tags_missing = cb.session_tag_fields(timeframe="1h", past=None)
    assert tags_missing["session"] == "n/a"
    assert tags_missing["sessions"] == []
    assert tags_missing["entry_hour_utc"] is None


def test_finalize_net_outcome_uses_pnl_dollars() -> None:
    outcome, gross, correct = cb._finalize_net_outcome(
        outcome_gross="WIN",
        pnl_dollars=-12.5,
    )
    assert gross == "WIN"
    assert outcome == "LOSS"
    assert correct is False

    outcome2, gross2, correct2 = cb._finalize_net_outcome(
        outcome_gross="LOSS",
        pnl_dollars=3.0,
    )
    assert gross2 == "LOSS"
    assert outcome2 == "WIN"
    assert correct2 is True


def test_assert_outcome_matches_net_pnl_logs_mismatch(capsys=None) -> None:
    bad = [
        {
            "date": "2024-01-02",
            "ticker": "EURUSD",
            "outcome": "WIN",
            "pnl_dollars": -1.0,
            "exit_reason": "TRAIL_STOP",
            "outcome_gross": "WIN",
        }
    ]
    # Should not raise — loud log only.
    cb._assert_outcome_matches_net_pnl(bad, context="unit-test")
    good = [{"outcome": "WIN", "pnl_dollars": 5.0, "skipped": False}]
    cb._assert_outcome_matches_net_pnl(good, context="unit-test-ok")


def test_calc_session_performance_skips_na() -> None:
    trades = [
        {"session": "sydney", "outcome": "WIN", "pnl_dollars": 10.0},
        {"session": "n/a", "outcome": "WIN", "pnl_dollars": 99.0},
        {"session": "new_york", "outcome": "LOSS", "pnl_dollars": -5.0},
        {"session": "new_york", "outcome": "WIN", "pnl_dollars": 2.0},
        {"session": "n/a", "outcome": "LOSS", "pnl_dollars": -1.0},
    ]
    perf = cb._calc_session_performance(trades)
    assert perf["sydney"]["total"] == 1
    assert perf["new_york"]["total"] == 2
    assert "n/a" not in perf
    assert perf["untaggable_trades"] == 2
