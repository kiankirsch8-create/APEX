"""Unit tests for A+B sizing throttle in continuous_backtester (no network)."""
from __future__ import annotations

import continuous_backtester as cb


def _reset_histories() -> None:
    cb._ST_MEDIUM_HISTORY.clear()
    cb._STRAT_PNL_HISTORY.clear()


def test_ab_throttle_stacks_without_clamp() -> None:
    _reset_histories()
    cb._STRAT_PNL_HISTORY["T01"] = [-10.0, -5.0, -1.0]
    cb._ST_MEDIUM_HISTORY.extend([(-1.0, False), (-2.0, False), (-3.0, False)])
    a, b, t = cb._compute_ab_throttle_at_open(
        strategy_id="T01",
        confidence="MEDIUM",
        macro_bias="STRONG_TAILWIND",
    )
    assert a == cb._AB_THROTTLE_FACTOR
    assert b == cb._AB_THROTTLE_FACTOR
    assert t == cb._AB_THROTTLE_FACTOR * cb._AB_THROTTLE_FACTOR


def test_ab_throttle_short_history_no_throttle() -> None:
    _reset_histories()
    cb._STRAT_PNL_HISTORY["T02"] = [-10.0, 5.0]
    a, b, t = cb._compute_ab_throttle_at_open(
        strategy_id="T02",
        confidence="MEDIUM",
        macro_bias="STRONG_TAILWIND",
    )
    assert a == 1.0
    assert b == 1.0
    assert t == 1.0


def test_apply_ab_throttle_scales_sizing() -> None:
    _reset_histories()
    cb._STRAT_PNL_HISTORY["T03"] = [-1.0, -2.0, -3.0]
    ai = {
        "_position_size": 100000.0,
        "_leveraged_exposure": 500000.0,
        "_max_risk_dollars": 200.0,
        "_account_risk_pct": 0.02,
    }
    cb._apply_ab_throttle_to_ai_sizing(
        ai,
        strategy_id="T03",
        confidence="HIGH",
        macro_bias="NEUTRAL",
    )
    assert ai["_max_risk_dollars"] == round(200.0 * cb._AB_THROTTLE_FACTOR, 2)
    assert ai["_ab_throttle"] == cb._AB_THROTTLE_FACTOR


def test_ab_trade_record_baseline_from_applied() -> None:
    row = cb._ab_trade_record_fields(
        pnl_dollars=18.0,
        max_risk_dollars=100.0,
        ab_throttle=0.18,
        ab_mult_a=0.18,
        ab_mult_b=1.0,
    )
    assert row["shadow_baseline_pnl_dollars"] == 100.0
    assert row["pnl_pct"] == 0.18


if __name__ == "__main__":
    test_ab_throttle_stacks_without_clamp()
    test_ab_throttle_short_history_no_throttle()
    test_apply_ab_throttle_scales_sizing()
    test_ab_trade_record_baseline_from_applied()
    print("ok")
