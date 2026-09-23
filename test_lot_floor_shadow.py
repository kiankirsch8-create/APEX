"""Tests for broker minimum-lot shadow curves."""
from __future__ import annotations

import continuous_backtester as cb


def test_lot_floor_config() -> None:
    assert cb.LOT_FLOOR_ENABLED is True
    assert cb.LOT_MIN == 0.01
    assert cb.LOT_STEP == 0.01
    assert cb.LOT_FLOOR_ACCOUNT_SIZES == [10000, 25000, 50000, 100000, 200000]
    assert set(cb.LOT_FLOOR_POLICIES) == {"skip", "round_up"}


def test_round_lots_down() -> None:
    assert cb._round_lots_down(0.017) == 0.01
    assert cb._round_lots_down(0.029) == 0.02
    assert cb._round_lots_down(0.01) == 0.01
    assert cb._round_lots_down(0.009) == 0.0


def test_apply_lot_floor_skip_and_round_up() -> None:
    # position_size 500 → 0.005 lots at $10k — below minimum
    lots, scale, blocked = cb.apply_lot_floor(
        position_size=500.0,
        account_size=10_000,
        policy="skip",
    )
    assert blocked is True
    assert lots == 0.0
    assert scale == 0.0

    lots_u, scale_u, blocked_u = cb.apply_lot_floor(
        position_size=500.0,
        account_size=10_000,
        policy="round_up",
    )
    assert blocked_u is False
    assert lots_u == 0.01
    assert abs(scale_u - (0.01 / 0.005)) < 1e-9

    # 0.017 lots at $10k → round down to 0.01 (41% cut)
    lots_d, scale_d, blocked_d = cb.apply_lot_floor(
        position_size=1700.0,
        account_size=10_000,
        policy="skip",
    )
    assert blocked_d is False
    assert lots_d == 0.01
    assert abs(scale_d - (0.01 / 0.017)) < 1e-9


def test_lotfloor_runner_ten_curves_and_map_shape() -> None:
    runner = cb._ShadowLotFloorRunner()
    assert len(runner.curves) == 10
    ctx = {
        "date": "2024-01-15",
        "strategy_id": "T01",
        "confidence": "HIGH",
        "macro_bias": "NEUTRAL",
        "position_size": 500.0,  # 0.005 lots @ 10k → skip blocks, round_up takes
        "pnl_dollars": 20.0,
        "max_risk_dollars": 10.0,
        "outcome": "WIN",
    }
    nested = runner.process_trade(ctx)
    assert set(nested.keys()) == {"skip", "round_up"}
    assert nested["skip"]["acct_10000"]["blocked"] is True
    assert nested["skip"]["acct_10000"]["pnl"] == 0.0
    assert nested["round_up"]["acct_10000"]["blocked"] is False
    assert nested["round_up"]["acct_10000"]["lots"] == 0.01
    # round_up scales pnl 2x (0.01/0.005)
    assert nested["round_up"]["acct_10000"]["pnl"] == 40.0
    # $25k: raw lots = 0.0125 → round down to 0.01 under either policy
    assert nested["skip"]["acct_25000"]["blocked"] is False
    assert nested["skip"]["acct_25000"]["lots"] == 0.01


def test_lotfloor_blocked_excluded_from_ab_histories() -> None:
    runner = cb._ShadowLotFloorRunner()
    st_skip = runner.curves[cb._lot_floor_curve_id("skip", 10000)]
    ctx = {
        "date": "2024-02-01",
        "strategy_id": "T05",
        "confidence": "MEDIUM",
        "macro_bias": "STRONG_TAILWIND",
        "position_size": 400.0,
        "pnl_dollars": -15.0,
        "max_risk_dollars": 10.0,
        "outcome": "LOSS",
    }
    runner.process_trade(ctx)
    assert st_skip.trades_blocked == 1
    assert st_skip.trades_taken == 0
    assert st_skip.strat_pnl_history.get("T05") is None
    assert st_skip.st_medium_history.get("n", 0) == 0
    assert st_skip.blocked_net_r == -1.5  # -15/10


def test_lotfloor_summaries_shape() -> None:
    runner = cb._ShadowLotFloorRunner()
    runner.on_new_day("2024-01-02")
    runner.process_trade(
        {
            "date": "2024-01-02",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "position_size": 2000.0,
            "pnl_dollars": 50.0,
            "max_risk_dollars": 25.0,
            "outcome": "WIN",
        }
    )
    runner.finalize_day("2024-01-02")
    summary = runner.summaries()
    assert "skip" in summary and "round_up" in summary
    acct = summary["skip"]["acct_10000"]
    for key in (
        "final_capital",
        "peak_capital",
        "max_drawdown_pct",
        "worst_day_pct",
        "positive_months",
        "trades_taken",
        "trades_blocked",
        "taken_net_r",
        "blocked_net_r",
    ):
        assert key in acct
    assert acct["trades_taken"] == 1


def test_real_curve_untouched_by_lot_floor_flag() -> None:
    # Guard: enabling lot floor must not alter STARTING_CAPITAL / sizing constants.
    assert cb.STARTING_CAPITAL == 10000.0
    assert cb.LOT_FLOOR_ENABLED is True
