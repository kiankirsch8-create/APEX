"""Unit tests for CFG guardrails in continuous_backtester."""
from __future__ import annotations

from datetime import date

import continuous_backtester as cb


def _reset_histories() -> None:
    cb._ST_MEDIUM_HISTORY.clear()
    cb._STRAT_PNL_HISTORY.clear()


def test_warmup_multiplier_before_warmup_days() -> None:
    _reset_histories()
    cb._STRAT_PNL_HISTORY["T01"] = [1.0] * cb.COLD_START_MIN_TRADES
    ai = {
        "_position_size": 100.0,
        "_leveraged_exposure": 500.0,
        "_max_risk_dollars": 200.0,
        "_account_risk_pct": 0.02,
        "_ab_throttle": 1.0,
    }
    act = date(2024, 1, 1)
    scan = date(2024, 1, 15)  # ~10 trading days
    row = cb._apply_cfg_guardrails_to_ai_sizing(
        ai,
        strategy_id="T01",
        capital=10_000.0,
        peak_capital=10_000.0,
        scan_d=scan,
        activation_date=act,
        day_realized_pnl=0.0,
        day_anchor=10_000.0,
    )
    assert row["guard_warmup_mult"] == cb.WARMUP_MULTIPLIER
    assert row["guard_cold_start_mult"] == 1.0
    assert ai["_max_risk_dollars"] == round(200.0 * cb.WARMUP_MULTIPLIER, 2)


def test_cold_start_uses_strat_history_count() -> None:
    _reset_histories()
    cb._STRAT_PNL_HISTORY["T02"] = [1.0, 2.0]
    ai = {
        "_position_size": 100.0,
        "_leveraged_exposure": 500.0,
        "_max_risk_dollars": 200.0,
        "_account_risk_pct": 0.02,
        "_ab_throttle": 1.0,
    }
    row = cb._apply_cfg_guardrails_to_ai_sizing(
        ai,
        strategy_id="T02",
        capital=10_000.0,
        peak_capital=10_000.0,
        scan_d=date(2024, 3, 1),
        activation_date=date(2024, 1, 1),
        day_realized_pnl=0.0,
        day_anchor=10_000.0,
    )
    assert row["guard_cold_start_mult"] == cb.COLD_START_MULTIPLIER
    assert row["guard_warmup_mult"] == 1.0


def test_daily_loss_stop_blocks() -> None:
    assert cb._guard_daily_loss_stopped(day_realized_pnl=-350.0, day_anchor=10_000.0)
    row = cb._apply_cfg_guardrails_to_ai_sizing(
        {},
        strategy_id="T03",
        capital=9_650.0,
        peak_capital=10_000.0,
        scan_d=date(2024, 2, 1),
        activation_date=date(2024, 1, 1),
        day_realized_pnl=-350.0,
        day_anchor=10_000.0,
    )
    assert row["guard_blocked"] is True
    assert row["guard_mode"] == "DAILY_STOPPED"


def test_baseline_divides_ab_and_guard() -> None:
    row = cb._ab_trade_record_fields(
        pnl_dollars=18.0,
        max_risk_dollars=100.0,
        ab_throttle=0.18,
        ab_mult_a=0.18,
        ab_mult_b=1.0,
        guard_total_mult=0.25,
    )
    assert row["shadow_baseline_pnl_dollars"] == round(18.0 / (0.18 * 0.25), 2)


if __name__ == "__main__":
    test_warmup_multiplier_before_warmup_days()
    test_cold_start_uses_strat_history_count()
    test_daily_loss_stop_blocks()
    test_baseline_divides_ab_and_guard()
    print("ok")
