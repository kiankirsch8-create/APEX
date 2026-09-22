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


def test_chrono_session_no_longer_hardcodes_new_york_for_daily() -> None:
    tags = cb.session_tag_fields(analysis_date="2024-03-01")
    assert tags["entry_hour_utc"] == 0
    assert tags["session"] in ("sydney", "tokyo")
    assert tags["session"] in tags["sessions"]
    # Old helper mapped every 1d/1w trade to new_york — that must not be the entry-hour tag.
    assert tags["session"] != "new_york"


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


def test_calc_session_performance_uses_primary_tags() -> None:
    trades = [
        {"session": "sydney", "outcome": "WIN", "pnl_dollars": 10.0},
        {"session": "new_york", "outcome": "LOSS", "pnl_dollars": -5.0},
        {"session": "new_york", "outcome": "WIN", "pnl_dollars": 2.0},
    ]
    perf = cb._calc_session_performance(trades)
    assert perf["sydney"]["total"] == 1
    assert perf["new_york"]["total"] == 2
    assert "asia" not in perf
