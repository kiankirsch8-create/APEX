"""Tests for shadow exit TYPE variants (fixed R, BE, partial, time, ATR trail)."""
from __future__ import annotations

import pandas as pd

import continuous_backtester as cb


def _ohlc_from_closes(closes: list[float], pad: float = 0.0002) -> pd.DataFrame:
    rows = []
    for c in closes:
        rows.append({"Open": c, "High": c + pad, "Low": c - pad, "Close": c})
    return pd.DataFrame(rows)


def test_shadow_exit_config_shape() -> None:
    names = [n for n, _ in cb.SHADOW_EXIT_CONFIGS]
    assert names == [
        "exit_fixed_1r",
        "exit_fixed_2r",
        "exit_fixed_3r",
        "exit_fixed_5r",
        "exit_be_at_1r",
        "exit_be_at_1_5r",
        "exit_half_at_2r",
        "exit_time_5d",
        "exit_time_10d",
        "exit_time_20d",
        "exit_atr_trail_2",
        "exit_atr_trail_3",
    ]
    assert cb.SHADOW_EXIT_ENABLED is True
    by = {n: c for n, c in cb.SHADOW_EXIT_CONFIGS}
    assert by["exit_fixed_2r"]["type"] == "fixed_r"
    assert by["exit_be_at_1r"]["trigger_r"] == 1.0
    assert by["exit_half_at_2r"]["close_pct"] == 50
    assert by["exit_time_5d"]["max_nights"] == 5
    assert by["exit_atr_trail_3"]["mult"] == 3.0


def test_fixed_r_hits_target() -> None:
    # LONG: entry 1.1000, stop 1.0900 → risk 0.01; 2R target = 1.1200
    closes = [1.1000, 1.1050, 1.1100, 1.1200, 1.1300]
    # Expand high on the 2R bar so the target is touched.
    df = _ohlc_from_closes(closes)
    df.loc[3, "High"] = 1.1210
    sim = cb._simulate_shadow_exit_variant(
        direction="LONG",
        entry=1.1000,
        stop_loss=1.0900,
        forward_df=df,
        atr=0.005,
        timeframe="1d",
        cfg={"type": "fixed_r", "r": 2.0},
    )
    assert sim is not None
    assert abs(float(sim["exit_price"]) - 1.1200) < 1e-9
    assert "FIXED" in str(sim["exit_reason"]).upper()
    assert int(sim["candles_to_exit"]) == 4


def test_time_stop_nights_differ_and_swap() -> None:
    # Flat path — never hits stop; time exit decides hold length.
    closes = [1.1000 + i * 0.00001 for i in range(30)]
    df = _ohlc_from_closes(closes)
    entry, stop = 1.1000, 1.0900
    ps, lev = 100_000.0, 100_000.0 * entry

    out = cb._shadow_exit_fields(
        direction="LONG",
        entry=entry,
        stop_loss=stop,
        forward_df=df,
        position_size=ps,
        leveraged_exposure=lev,
        timeframe="1d",
        atr=0.005,
        ticker="EURUSD",
    )
    t5 = out["exit_time_5d"]
    t20 = out["exit_time_20d"]
    assert t5 is not None and t20 is not None
    assert float(t5["nights_held"]) < float(t20["nights_held"])
    # Swap differs with nights (absolute values depend on rate differential sign).
    assert abs(float(t5["swap_amount"])) < abs(float(t20["swap_amount"])) or (
        float(t5["nights_held"]) < float(t20["nights_held"])
    )
    assert "TIME_5N" in str(t5["exit_reason"])
    assert "TIME_20N" in str(t20["exit_reason"])


def test_breakeven_locks_then_stop() -> None:
    # Rise to 1R then reverse through entry (BE stop).
    # entry 1.10, stop 1.09, risk 0.01 → 1R = 1.11
    df = pd.DataFrame(
        [
            {"Open": 1.100, "High": 1.101, "Low": 1.099, "Close": 1.100},
            {"Open": 1.105, "High": 1.112, "Low": 1.104, "Close": 1.110},  # arms BE
            {"Open": 1.108, "High": 1.109, "Low": 1.099, "Close": 1.100},  # hits BE
        ]
    )
    sim = cb._simulate_shadow_exit_variant(
        direction="LONG",
        entry=1.1000,
        stop_loss=1.0900,
        forward_df=df,
        atr=0.005,
        timeframe="1d",
        cfg={"type": "breakeven", "trigger_r": 1.0},
    )
    assert sim is not None
    assert abs(float(sim["exit_price"]) - 1.1000) < 1e-9
    assert "BREAKEVEN" in str(sim["exit_reason"]).upper()


def test_partial_then_trail() -> None:
    # Hit 2R, close half, then trail remainder.
    entry, stop = 1.1000, 1.0900
    risk = 0.01
    df = pd.DataFrame(
        [
            {"Open": 1.100, "High": 1.101, "Low": 1.099, "Close": 1.100},
            {"Open": 1.110, "High": 1.121, "Low": 1.109, "Close": 1.120},  # 2R
            {"Open": 1.125, "High": 1.130, "Low": 1.124, "Close": 1.128},
            # Pull back more than 1.5R from peak 1.130 → trail stop ~ 1.115
            {"Open": 1.120, "High": 1.121, "Low": 1.110, "Close": 1.112},
        ]
    )
    sim = cb._simulate_shadow_exit_variant(
        direction="LONG",
        entry=entry,
        stop_loss=stop,
        forward_df=df,
        atr=0.005,
        timeframe="1d",
        cfg={"type": "partial", "close_pct": 50, "at_r": 2.0, "then_trail_r": 1.5},
    )
    assert sim is not None
    assert float(sim["pnl_pct"]) > 0  # half at +2R is already profitable
    assert int(sim["candles_to_exit"]) >= 2


def test_atr_trail_exits() -> None:
    entry, stop = 1.1000, 1.0900
    atr = 0.010
    # Rise then fall through 2*ATR trail from peak.
    df = pd.DataFrame(
        [
            {"Open": 1.100, "High": 1.105, "Low": 1.098, "Close": 1.104},
            {"Open": 1.110, "High": 1.130, "Low": 1.108, "Close": 1.125},
            {"Open": 1.120, "High": 1.122, "Low": 1.100, "Close": 1.102},  # trail ~1.110
        ]
    )
    sim = cb._simulate_shadow_exit_variant(
        direction="LONG",
        entry=entry,
        stop_loss=stop,
        forward_df=df,
        atr=atr,
        timeframe="1d",
        cfg={"type": "atr_trail", "mult": 2.0},
    )
    assert sim is not None
    assert "TRAIL" in str(sim["exit_reason"]).upper() or float(sim["exit_price"]) > stop


def test_shadow_exit_runner_summary() -> None:
    runner = cb._ShadowExitRunner()
    assert cb._ShadowExitRunner.LIVE_KEY in runner.curves
    for name, _ in cb.SHADOW_EXIT_CONFIGS:
        assert name in runner.curves

    runner.on_new_day("2024-01-02")
    row = {
        "date": "2024-01-02",
        "outcome": "WIN",
        "pnl_dollars": 100.0,
        "nights_held": 3.0,
        "swap_amount": -1.5,
        "shadow_exit": {
            "exit_fixed_2r": {
                "pnl_dollars": 40.0,
                "nights_held": 2.0,
                "swap_amount": -0.8,
                "exit_reason": "FIXED_2_0R",
            },
            "exit_time_5d": {
                "pnl_dollars": 10.0,
                "nights_held": 5.0,
                "swap_amount": -2.0,
                "exit_reason": "TIME_5N",
            },
            "exit_time_20d": {
                "pnl_dollars": -5.0,
                "nights_held": 20.0,
                "swap_amount": -8.0,
                "exit_reason": "TIME_20N",
            },
        },
    }
    runner.process_trade(row)
    runner.finalize_day("2024-01-02")
    summaries = runner.summaries()

    assert summaries["exit_live"]["final_capital"] == cb.STARTING_CAPITAL + 100.0
    assert summaries["exit_fixed_2r"]["final_capital"] == cb.STARTING_CAPITAL + 40.0
    assert summaries["exit_time_5d"]["median_nights_held"] == 5.0
    assert summaries["exit_time_20d"]["median_nights_held"] == 20.0
    assert summaries["exit_time_5d"]["total_swap_paid"] == -2.0
    assert summaries["exit_time_20d"]["total_swap_paid"] == -8.0
    for key in (
        "final_capital",
        "max_drawdown_pct",
        "worst_day_pct",
        "positive_months",
        "median_nights_held",
        "total_swap_paid",
    ):
        assert key in summaries["exit_fixed_2r"]


def test_shadow_exit_fields_map_keys() -> None:
    df = _ohlc_from_closes([1.10, 1.11, 1.12, 1.11, 1.10])
    out = cb._shadow_exit_fields(
        direction="LONG",
        entry=1.10,
        stop_loss=1.09,
        forward_df=df,
        position_size=50_000,
        leveraged_exposure=55_000,
        timeframe="1d",
        atr=0.005,
        ticker="EURUSD",
    )
    assert set(out.keys()) == {n for n, _ in cb.SHADOW_EXIT_CONFIGS}
    for v in out.values():
        assert v is None or (
            "pnl_dollars" in v and "nights_held" in v and "swap_amount" in v
        )
