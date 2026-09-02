"""Tests for shadow evaluation of BLOCKED_PAIRS in chrono backtests."""
from __future__ import annotations

import continuous_backtester as cb


def test_hard_block_bypassed_in_shadow_eval() -> None:
    cb._enter_shadow_blocked_eval("AUDUSD")
    try:
        assert cb._hard_block_skip_reason("AUDUSD", "T01") is None
        assert cb._maybe_shadow_blocked_trade_fields("AUDUSD") == {
            "shadow_reason": "BLOCKED_PAIR"
        }
    finally:
        cb._exit_shadow_blocked_eval()
    assert cb._hard_block_skip_reason("AUDUSD", "T01") is not None


def test_shadow_ab_histories_do_not_touch_real() -> None:
    cb._reset_shadow_blocked_run_state()
    cb._STRAT_PNL_HISTORY.clear()
    cb._STRAT_PNL_HISTORY["T01"] = [50.0]
    cb._ST_MEDIUM_HISTORY.clear()
    row = {
        "strategy_id": "T01",
        "confidence": "MEDIUM",
        "macro_bias": "STRONG_TAILWIND",
        "outcome": "WIN",
        "pnl_dollars": 120.0,
    }
    cb._record_shadow_blocked_ab_histories(row)
    assert cb._SHADOW_BLOCKED_STRAT_PNL["T01"] == [120.0]
    assert len(cb._SHADOW_BLOCKED_ST_MEDIUM) == 1
    assert cb._STRAT_PNL_HISTORY["T01"] == [50.0]
    assert cb._ST_MEDIUM_HISTORY == []


def test_ab_throttle_uses_shadow_histories() -> None:
    cb._reset_shadow_blocked_run_state()
    cb._SHADOW_BLOCKED_STRAT_PNL["T02"] = [-10.0, -20.0, -5.0]
    cb._enter_shadow_blocked_eval("USDCAD")
    try:
        _, _, thr = cb._compute_ab_throttle_at_open(
            strategy_id="T02",
            confidence="HIGH",
            macro_bias="NEUTRAL",
        )
        assert thr == cb._AB_THROTTLE_FACTOR
    finally:
        cb._exit_shadow_blocked_eval()


def test_compute_shadow_blocked_summary() -> None:
    trades = [
        {
            "ticker": "AUDUSD",
            "date": "2022-03-01",
            "outcome": "WIN",
            "pnl_dollars": 100.0,
            "pnl_r_net": 1.0,
        },
        {
            "ticker": "AUDUSD",
            "date": "2022-06-01",
            "outcome": "LOSS",
            "pnl_dollars": -50.0,
            "pnl_r_net": -0.5,
        },
        {
            "ticker": "USDCAD",
            "date": "2023-01-15",
            "outcome": "WIN",
            "pnl_dollars": 80.0,
            "pnl_r_net": 0.8,
        },
    ]
    summary = cb._compute_shadow_blocked_summary(trades)
    assert summary["total_trades"] == 3
    assert summary["by_ticker"]["AUDUSD"]["trade_count"] == 2
    assert summary["by_ticker"]["AUDUSD"]["net_pnl"] == 50.0
    assert summary["by_ticker"]["AUDUSD"]["net_r"] == 0.5
    assert "2022" in summary["by_ticker"]["AUDUSD"]["by_year"]
    assert summary["by_ticker"]["USDCAD"]["trade_count"] == 1


def test_shadow_flag_off_keeps_hard_block() -> None:
    assert "AUDUSD" in cb.BLOCKED_PAIRS
    assert cb._shadow_blocked_pair_chrono_eval("AUDUSD", chrono_yfinance=False) is False


def test_excluded_pairs_unchanged() -> None:
    for sym in ("AUDCAD", "AUDNZD", "EURCAD", "EURJPY", "GBPAUD", "NZDJPY"):
        assert sym in cb.EXCLUDED_PAIRS
        assert sym not in cb.BLOCKED_PAIRS


if __name__ == "__main__":
    test_hard_block_bypassed_in_shadow_eval()
    test_shadow_ab_histories_do_not_touch_real()
    test_ab_throttle_uses_shadow_histories()
    test_compute_shadow_blocked_summary()
    test_shadow_flag_off_keeps_hard_block()
    test_excluded_pairs_unchanged()
    print("ok")
