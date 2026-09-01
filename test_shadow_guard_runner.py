"""Unit tests for parallel shadow guard configurations."""
from __future__ import annotations

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
    assert "compound_10_bare" in maps
    assert "compound_10_full" in maps
    guard, compound = cb._split_shadow_trade_maps(maps)
    assert len(guard) == 23
    assert len(compound) == len(cb.SHADOW_COMPOUND_RISK_PCTS) * 2
    assert maps["ramp40_025"]["blocked"] is False
    assert maps["ramp40_025"]["mult"] == cb.WARMUP_MULTIPLIER


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


if __name__ == "__main__":
    test_shadow_guard_config_count()
    test_shadow_runner_parallel_curves()
    test_shadow_daily_stop_blocks_config_only()
    test_shadow_summaries()
    print("ok")
