"""Unit tests for parallel shadow guard configurations."""
from __future__ import annotations

from typing import Any

import continuous_backtester as cb


def test_shadow_guard_config_count() -> None:
    assert len(cb.SHADOW_GUARD_CONFIGS) == 23


def test_shadow_runner_parallel_curves() -> None:
    runner = cb._ShadowGuardRunner()
    ctx = {
        "date": "2024-01-15",
        "strategy_id": "T01",
        "confidence": "MEDIUM",
        "macro_bias": "NEUTRAL",
        "baseline_pnl": 100.0,
        "ab_throttle": 1.0,
        "pre_ab_risk_pct": 0.01,
        "pre_ab_max_risk": 100.0,
        "max_risk_dollars": 100.0,
        "outcome": "WIN",
    }
    maps = runner.process_trade(ctx)
    assert "ramp40_025" in maps
    assert "full_stack" in maps
    assert "compound_050_bare" in maps
    assert "compound_050_full" in maps
    guard, compound, event = cb._split_shadow_trade_maps(maps)
    assert len(guard) == 23
    assert len(compound) == len(cb.SHADOW_COMPOUND_RISK_FRACTIONS) * 2
    assert len(event) == len(cb.SHADOW_EVENT_CONFIGS)
    assert maps["ramp40_025"]["blocked"] is False
    assert maps["ramp40_025"]["mult"] == cb.WARMUP_MULTIPLIER


def test_compound_risk_scales_with_curve_capital() -> None:
    runner = cb._ShadowGuardRunner()
    st = runner.curves["compound_050_bare"]
    assert st.compound_risk_fraction == 0.005
    assert st.capital == cb.STARTING_CAPITAL
    target_10k = st.capital * float(st.compound_risk_fraction)
    assert round(target_10k, 2) == 50.00
    st.capital = 20_000.0
    target_20k = st.capital * float(st.compound_risk_fraction)
    assert round(target_20k, 2) == 100.00
    ratio_10k = cb._compound_risk_ratio(
        curve_capital=10_000.0,
        compound_risk_fraction=0.005,
        reference_risk_dollars=100.0,
    )
    ratio_20k = cb._compound_risk_ratio(
        curve_capital=20_000.0,
        compound_risk_fraction=0.005,
        reference_risk_dollars=100.0,
    )
    assert round(ratio_10k, 2) == 0.50
    assert round(ratio_20k, 2) == 1.00


def test_shadow_daily_stop_blocks_config_only() -> None:
    runner = cb._ShadowGuardRunner()
    st = runner.curves["daily3"]
    st.day_anchor = 10_000.0
    st.day_pnl = -350.0
    ctx = {
        "date": "2024-02-01",
        "strategy_id": "T02",
        "confidence": "LOW",
        "macro_bias": "NEUTRAL",
        "baseline_pnl": 50.0,
        "ab_throttle": 1.0,
        "pre_ab_risk_pct": 0.005,
        "pre_ab_max_risk": 50.0,
        "max_risk_dollars": 50.0,
        "outcome": "WIN",
    }
    row = runner.process_trade(ctx)
    assert row["daily3"]["blocked"] is True
    assert row["daily3"]["pnl"] == 0.0
    assert row["ramp40_025"]["blocked"] is False


def test_shadow_summaries() -> None:
    runner = cb._ShadowGuardRunner()
    runner.on_new_day("2024-01-02")
    runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T03",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "baseline_pnl": 200.0,
            "ab_throttle": 1.0,
            "pre_ab_risk_pct": 0.02,
            "pre_ab_max_risk": 200.0,
            "max_risk_dollars": 200.0,
            "outcome": "WIN",
        }
    )
    runner.finalize_day("2024-01-02")
    summary = runner.guard_summaries()["full_stack"]
    assert summary["trades_taken"] == 1
    assert summary["final_capital"] > cb.STARTING_CAPITAL


def test_skipped_row_does_not_update_shadow_histories() -> None:
    runner = cb._ShadowGuardRunner()
    snapshots = {
        cid: (st.capital, st.trades_taken, dict(st.strat_pnl_history))
        for cid, st in runner.curves.items()
    }
    result = runner.process_trade(
        {
            "date": "2024-02-01",
            "strategy_id": "T04",
            "confidence": "LOW",
            "macro_bias": "NEUTRAL",
            "baseline_pnl": 0.0,
            "ab_throttle": 1.0,
            "pre_ab_risk_pct": 0.005,
            "pre_ab_max_risk": 50.0,
            "max_risk_dollars": 50.0,
            "outcome": "SKIP",
            "skipped": True,
        }
    )
    assert result == {}
    for cid, st in runner.curves.items():
        cap, taken, hist = snapshots[cid]
        assert st.capital == cap
        assert st.trades_taken == taken
        assert st.strat_pnl_history == hist


def test_shadow_histories_store_count_and_last3_only() -> None:
    hist: dict[str, dict[str, Any]] = {}
    for i in range(5):
        cb._shadow_record_strat_pnl(hist, "T05", float(i + 1))
    assert hist["T05"]["n"] == 5
    assert hist["T05"]["last3"] == [3.0, 4.0, 5.0]
    assert cb._shadow_strat_closed_count(hist, "T05") == 5
    assert cb._shadow_strat_last3_sum(hist, "T05") == 12.0
    st_hist = cb._normalize_shadow_st_medium_history(
        [[1.0, True], [2.0, False], [3.0, True], [4.0, True]]
    )
    assert st_hist["n"] == 4
    assert len(st_hist["last3"]) == 3


if __name__ == "__main__":
    test_shadow_guard_config_count()
    test_shadow_runner_parallel_curves()
    test_compound_risk_scales_with_curve_capital()
    test_shadow_daily_stop_blocks_config_only()
    test_shadow_summaries()
    test_skipped_row_does_not_update_shadow_histories()
    test_shadow_histories_store_count_and_last3_only()
    print("ok")
