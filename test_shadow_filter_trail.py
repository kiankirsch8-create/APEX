"""Tests for shadow filter (no_shorts / period gates) and trail capital summary."""
from __future__ import annotations

import continuous_backtester as cb


def test_filter_and_trail_config_shape() -> None:
    names = [n for n, _ in cb.SHADOW_FILTER_CONFIGS]
    assert names == [
        "no_shorts",
        "gate_prev_month_r",
        "gate_prev_month_pnl",
        "gate_rolling_20",
    ]
    assert cb.SHADOW_FILTER_CONFIGS[0][1]["direction_filter"] == "LONG"
    assert cb.SHADOW_TRAIL_SUMMARY_ENABLED is True
    trail_runner = cb._ShadowTrailSummaryRunner()
    assert cb._ShadowTrailSummaryRunner.LIVE_KEY in trail_runner.curves
    for name, _ in cb._SHADOW_TRAIL_ACTIVATE_RS:
        assert name in trail_runner.curves
    assert "shadow_trail_at_1_50r" in trail_runner.curves


def test_no_shorts_blocks_short_takes_long() -> None:
    runner = cb._ShadowFilterRunner()
    st = runner.curves["no_shorts"]
    out_s = runner.process_trade(
        {
            "date": "2024-01-05",
            "direction": "SHORT",
            "pnl_dollars": 50.0,
            "pnl_r_net": 1.0,
            "outcome": "WIN",
        }
    )
    assert out_s["no_shorts"]["blocked"] is True
    assert out_s["no_shorts"]["pnl"] == 0.0
    assert st.capital == cb.STARTING_CAPITAL
    assert st.trades_blocked == 1

    out_l = runner.process_trade(
        {
            "date": "2024-01-06",
            "direction": "LONG",
            "pnl_dollars": -20.0,
            "pnl_r_net": -0.5,
            "outcome": "LOSS",
        }
    )
    assert out_l["no_shorts"]["taken"] is True
    assert out_l["no_shorts"]["pnl"] == -20.0
    assert st.capital == cb.STARTING_CAPITAL - 20.0
    assert st.trades_taken == 1


def test_prev_month_gate_no_lookahead() -> None:
    runner = cb._ShadowFilterRunner()
    st = runner.curves["gate_prev_month_r"]

    # January: no prior month → allow (warm-up)
    out_jan = runner.process_trade(
        {
            "date": "2024-01-10",
            "direction": "LONG",
            "pnl_dollars": -100.0,
            "pnl_r_net": -2.0,
            "outcome": "LOSS",
        }
    )
    assert out_jan["gate_prev_month_r"]["taken"] is True
    assert out_jan["gate_prev_month_r"]["gate_open"] is True
    assert st.month_r["2024-01"] == -2.0

    # February: prev month R negative → block
    out_feb = runner.process_trade(
        {
            "date": "2024-02-05",
            "direction": "LONG",
            "pnl_dollars": 80.0,
            "pnl_r_net": 1.5,
            "outcome": "WIN",
        }
    )
    assert out_feb["gate_prev_month_r"]["blocked"] is True
    assert out_feb["gate_prev_month_r"]["gate_open"] is False
    assert out_feb["gate_prev_month_r"]["pnl"] == 0.0
    # Blocked trade still updates month signal for March
    assert st.month_r["2024-02"] == 1.5
    assert st.capital == cb.STARTING_CAPITAL - 100.0

    # March: prev month R positive → take
    out_mar = runner.process_trade(
        {
            "date": "2024-03-01",
            "direction": "LONG",
            "pnl_dollars": 40.0,
            "pnl_r_net": 0.8,
            "outcome": "WIN",
        }
    )
    assert out_mar["gate_prev_month_r"]["taken"] is True
    assert out_mar["gate_prev_month_r"]["gate_open"] is True
    assert st.capital == cb.STARTING_CAPITAL - 100.0 + 40.0


def test_rolling_20_gate_warmup_then_blocks() -> None:
    runner = cb._ShadowFilterRunner()
    st = runner.curves["gate_rolling_20"]
    # 19 losses: still warm-up (len < 20) → allow
    for i in range(19):
        out = runner.process_trade(
            {
                "date": f"2024-01-{i + 1:02d}",
                "direction": "LONG",
                "pnl_dollars": -10.0,
                "pnl_r_net": -0.5,
                "outcome": "LOSS",
            }
        )
        assert out["gate_rolling_20"]["gate_open"] is True
        assert out["gate_rolling_20"]["taken"] is True
    assert len(st.rolling_r) == 19

    # 20th loss fills window; next trade sees sum < 0 → block
    runner.process_trade(
        {
            "date": "2024-01-20",
            "direction": "LONG",
            "pnl_dollars": -10.0,
            "pnl_r_net": -0.5,
            "outcome": "LOSS",
        }
    )
    assert len(st.rolling_r) == 20
    out_block = runner.process_trade(
        {
            "date": "2024-01-21",
            "direction": "LONG",
            "pnl_dollars": 50.0,
            "pnl_r_net": 1.0,
            "outcome": "WIN",
        }
    )
    assert out_block["gate_rolling_20"]["gate_open"] is False
    assert out_block["gate_rolling_20"]["blocked"] is True


def test_challenge_metrics_months_to_plus_10() -> None:
    runner = cb._ShadowFilterRunner()
    st = runner.curves["no_shorts"]
    runner.on_new_day("2024-01-15")
    # +$1,100 from 10k = +11%
    runner.process_trade(
        {
            "date": "2024-01-15",
            "direction": "LONG",
            "pnl_dollars": 1100.0,
            "pnl_r_net": 2.0,
            "outcome": "WIN",
        }
    )
    runner.finalize_day("2024-01-15")
    summary = runner.summaries()["no_shorts"]
    assert summary["hit_plus_10pct"] is True
    assert summary["months_to_plus_10pct"] == 1
    assert summary["dd_under_10pct_to_plus_10"] is True
    assert "max_drawdown_pct" in summary
    assert "worst_day_pct" in summary


def test_trail_summary_from_existing_fields() -> None:
    runner = cb._ShadowTrailSummaryRunner()
    runner.on_new_day("2024-02-01")
    row = {
        "date": "2024-02-01",
        "outcome": "WIN",
        "pnl_dollars": 25.0,
        "shadow_trail_at_0_50r": {"pnl_dollars": 10.0, "exit_reason": "TRAIL_STOP"},
        "shadow_trail_at_1_00r": {"pnl_dollars": 15.0, "exit_reason": "TRAIL_STOP"},
        "shadow_trail_at_1_50r": {"pnl_dollars": 30.0, "exit_reason": "TP2"},
        "shadow_trail_at_2_50r": {"pnl_dollars": 5.0, "exit_reason": "STOP"},
    }
    runner.process_trade(row)
    runner.finalize_day("2024-02-01")
    summaries = runner.summaries()
    assert summaries["trail_live"]["final_capital"] == cb.STARTING_CAPITAL + 25.0
    assert summaries["shadow_trail_at_1_50r"]["final_capital"] == cb.STARTING_CAPITAL + 30.0
    assert summaries["shadow_trail_at_2_50r"]["final_capital"] == cb.STARTING_CAPITAL + 5.0
    for key in (
        "final_capital",
        "max_drawdown_pct",
        "worst_day_pct",
        "positive_months",
    ):
        assert key in summaries["shadow_trail_at_1_50r"]
        assert key in summaries["trail_live"]
    # Real curve constants untouched
    assert cb.APPLY_AB_THROTTLE is True
    assert cb._AB_THROTTLE_FACTOR == 0.18
