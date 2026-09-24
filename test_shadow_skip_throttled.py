"""Tests for shadow skip-A+B-throttled curves (virtual history, no capital)."""
from __future__ import annotations

import continuous_backtester as cb


def test_skip_throttled_config_shape() -> None:
    assert cb.SHADOW_SKIP_THROTTLED_ENABLED is True
    names = [n for n, _ in cb.SHADOW_SKIP_THROTTLED_CONFIGS]
    assert names == [
        "no_skip_control",
        "skip_all_throttled",
        "skip_double_only",
        "skip_throttled_long",
    ]
    assert cb.SHADOW_SKIP_THROTTLED_CONFIGS[0][1]["skip_below"] == 0.0
    assert cb.SHADOW_SKIP_THROTTLED_CONFIGS[1][1]["skip_below"] == 1.00
    assert cb.SHADOW_SKIP_THROTTLED_CONFIGS[2][1]["skip_below"] == 0.18
    assert cb.SHADOW_SKIP_THROTTLED_CONFIGS[3][1].get("longs_only") is True


def test_no_skip_control_never_virtual() -> None:
    """skip_below=0 → every trade taken at baseline * ab_shadow (real-curve mirror)."""
    runner = cb._ShadowSkipThrottledRunner()
    st = runner.curves["no_skip_control"]
    _seed_losing_strat(st, "T01")
    out = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 100.0,
            "pre_ab_max_risk": 50.0,
            "outcome": "WIN",
        }
    )
    assert out["no_skip_control"]["virtual"] is False
    assert abs(out["no_skip_control"]["ab_throttle"] - 0.18) < 1e-9
    assert out["no_skip_control"]["pnl"] == 18.0
    assert out["no_skip_control"]["hist_pnl"] == 18.0  # taken (throttled) PnL in history
    assert st.trades_virtual == 0
    assert st.trades_taken == 1
    assert st.capital == cb.STARTING_CAPITAL + 18.0
    # Sibling still virtualizes the same signal
    assert out["skip_all_throttled"]["virtual"] is True
    assert out["skip_all_throttled"]["pnl"] == 0.0


def _seed_losing_strat(st: cb._SkipThrottledCurveState, sid: str = "T01") -> None:
    """Make factor A throttle (last3 sum <= 0)."""
    for pnl in (-10.0, -5.0, -1.0):
        cb._shadow_record_strat_pnl(st.strat_pnl_history, sid, pnl)


def test_virtual_feeds_history_not_capital() -> None:
    runner = cb._ShadowSkipThrottledRunner()
    st = runner.curves["skip_all_throttled"]
    _seed_losing_strat(st, "T01")
    cap0 = float(st.capital)

    out = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 100.0,  # full-size win
            "pre_ab_max_risk": 50.0,
            "outcome": "WIN",
        }
    )
    assert out["skip_all_throttled"]["virtual"] is True
    assert out["skip_all_throttled"]["pnl"] == 0.0
    assert out["skip_all_throttled"]["hist_pnl"] == 100.0
    assert out["skip_all_throttled"]["ab_throttle"] == cb._AB_THROTTLE_FACTOR
    assert st.capital == cap0
    assert st.trades_virtual == 1
    assert st.trades_taken == 0
    # Full-size win recorded in history
    last3 = cb._shadow_strat_last3_sum(st.strat_pnl_history, "T01")
    assert last3 is not None
    assert last3 == (-5.0) + (-1.0) + 100.0


def test_virtual_win_releases_throttle_then_taken() -> None:
    runner = cb._ShadowSkipThrottledRunner()
    st = runner.curves["skip_all_throttled"]
    _seed_losing_strat(st, "T01")

    # First trade virtual (throttled). Full-size +100 flips last3 positive.
    out1 = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 100.0,
            "pre_ab_max_risk": 50.0,
            "outcome": "WIN",
        }
    )
    assert out1["skip_all_throttled"]["virtual"] is True
    assert st.trades_virtual == 1
    assert st.capital == cb.STARTING_CAPITAL

    # Next trade: A+B released → taken at full size
    out2 = runner.process_trade(
        {
            "date": "2024-01-03",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 40.0,
            "pre_ab_max_risk": 20.0,
            "outcome": "WIN",
        }
    )
    assert out2["skip_all_throttled"]["virtual"] is False
    assert out2["skip_all_throttled"]["ab_throttle"] == 1.0
    assert out2["skip_all_throttled"]["pnl"] == 40.0
    assert st.trades_taken == 1
    assert st.capital == cb.STARTING_CAPITAL + 40.0


def test_skip_double_only_takes_single_throttle() -> None:
    runner = cb._ShadowSkipThrottledRunner()
    st = runner.curves["skip_double_only"]
    # Seed only factor A → ab = 0.18 (>= skip_below 0.18) → taken at 0.18
    _seed_losing_strat(st, "T02")

    out = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T02",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 100.0,
            "pre_ab_max_risk": 50.0,
            "outcome": "WIN",
        }
    )
    assert out["skip_double_only"]["virtual"] is False
    assert abs(out["skip_double_only"]["ab_throttle"] - 0.18) < 1e-9
    assert out["skip_double_only"]["pnl"] == 18.0  # 100 * 0.18
    assert st.trades_taken == 1
    assert st.capital == cb.STARTING_CAPITAL + 18.0

    # Re-seed BOTH factors so ab = 0.0324 < 0.18 → virtual
    st.strat_pnl_history.clear()
    st.st_medium_history = {"n": 0, "last3": []}
    _seed_losing_strat(st, "T02")
    for pnl in (-10.0, -5.0, -1.0):
        cb._shadow_record_st_medium(st.st_medium_history, pnl, False)
    out2 = runner.process_trade(
        {
            "date": "2024-01-03",
            "strategy_id": "T02",
            "confidence": "MEDIUM",
            "macro_bias": "STRONG_TAILWIND",
            "direction": "LONG",
            "baseline_pnl": 100.0,
            "pre_ab_max_risk": 50.0,
            "outcome": "WIN",
        }
    )
    assert out2["skip_double_only"]["virtual"] is True
    assert abs(out2["skip_double_only"]["ab_throttle"] - (0.18 * 0.18)) < 1e-9
    assert out2["skip_double_only"]["pnl"] == 0.0
    assert st.trades_virtual == 1

def test_longs_only_takes_throttled_short() -> None:
    runner = cb._ShadowSkipThrottledRunner()
    st = runner.curves["skip_throttled_long"]
    _seed_losing_strat(st, "T03")

    out_short = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T03",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "SHORT",
            "baseline_pnl": -80.0,
            "pre_ab_max_risk": 40.0,
            "outcome": "LOSS",
        }
    )
    assert out_short["skip_throttled_long"]["virtual"] is False
    assert out_short["skip_throttled_long"]["pnl"] == round(-80.0 * 0.18, 2)

    out_long = runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T03",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": -80.0,
            "pre_ab_max_risk": 40.0,
            "outcome": "LOSS",
        }
    )
    assert out_long["skip_throttled_long"]["virtual"] is True
    assert out_long["skip_throttled_long"]["pnl"] == 0.0


def test_summary_virtual_vs_taken() -> None:
    runner = cb._ShadowSkipThrottledRunner()
    runner.on_new_day("2024-01-02")
    st = runner.curves["skip_all_throttled"]
    _seed_losing_strat(st, "T01")
    runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 20.0,
            "pre_ab_max_risk": 10.0,
            "outcome": "WIN",
        }
    )
    # Release then take
    for _ in range(2):
        runner.process_trade(
            {
                "date": "2024-01-02",
                "strategy_id": "T01",
                "confidence": "HIGH",
                "macro_bias": "NEUTRAL",
                "direction": "LONG",
                "baseline_pnl": 30.0,
                "pre_ab_max_risk": 10.0,
                "outcome": "WIN",
            }
        )
    runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "direction": "LONG",
            "baseline_pnl": 15.0,
            "pre_ab_max_risk": 10.0,
            "outcome": "WIN",
        }
    )
    runner.finalize_day("2024-01-02")
    summary = runner.summaries()["skip_all_throttled"]
    for key in (
        "final_capital",
        "peak_capital",
        "max_drawdown_pct",
        "worst_day_pct",
        "positive_months",
        "trades_taken",
        "trades_virtual",
        "taken_net_r",
        "virtual_net_r",
        "r_per_taken_trade",
    ):
        assert key in summary
    assert summary["trades_virtual"] >= 1
    assert summary["trades_taken"] >= 1
    # Real curve throttle unchanged
    assert cb._AB_THROTTLE_FACTOR == 0.18
    assert cb.APPLY_AB_THROTTLE is True
