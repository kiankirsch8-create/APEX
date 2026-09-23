"""Tests for shadow alternate stop-placement forward sims."""
from __future__ import annotations

import pandas as pd

import continuous_backtester as cb


def _ohlc_df(n: int = 40, start: float = 100.0, step: float = 0.1) -> pd.DataFrame:
    rows = []
    px = start
    for i in range(n):
        o = px
        h = px + abs(step) * 2
        l = px - abs(step) * 2
        c = px + step
        rows.append({"Open": o, "High": h, "Low": l, "Close": c})
        px = c
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(rows, index=idx)


def test_shadow_stop_config_shape() -> None:
    assert cb.SHADOW_STOP_ENABLED is True
    names = [n for n, _ in cb.SHADOW_STOP_CONFIGS]
    assert names == [
        "fixed_current",
        "fixed_075",
        "fixed_125",
        "fixed_200",
        "atr_1_0",
        "atr_1_5",
        "atr_2_0",
        "atr_2_5",
        "swing_10",
        "swing_20",
        "swing_20_buf",
        "max_atr15_swing",
    ]
    assert cb.SHADOW_STOP_MIN_LOT_ACCTS == (25_000, 50_000, 100_000)


def test_fixed_pct_and_atr_stop_distances() -> None:
    entry = 100.0
    cur_stop = 99.5  # 0.50%
    past = _ohlc_df(30)
    dist_cache: dict[str, float] = {}
    atr = 1.0

    fixed = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "fixed_pct", "mult": 2.0},
        direction="LONG",
        entry=entry,
        current_stop=cur_stop,
        atr=atr,
        past=past,
        dist_cache=dist_cache,
    )
    assert fixed is not None
    assert abs((entry - fixed) - 1.0) < 1e-9  # 0.5 * 2

    atr_stop = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "atr", "mult": 1.5},
        direction="LONG",
        entry=entry,
        current_stop=cur_stop,
        atr=atr,
        past=past,
        dist_cache=dist_cache,
    )
    assert atr_stop is not None
    assert abs((entry - atr_stop) - 1.5) < 1e-9


def test_swing_and_max_wider() -> None:
    # Build past where swing low is clearly below entry
    past = _ohlc_df(30, start=100.0, step=0.05)
    # Force a deep low 15 bars before the last
    past.iloc[-16, past.columns.get_loc("Low")] = 97.0
    entry = float(past.iloc[-1]["Close"])
    atr = 1.0
    dist_cache: dict[str, float] = {}

    swing = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "swing", "lookback": 20, "buffer_atr": 0.25},
        direction="LONG",
        entry=entry,
        current_stop=entry * 0.995,
        atr=atr,
        past=past,
        dist_cache=dist_cache,
    )
    assert swing is not None
    assert swing < entry
    # 97 - 0.25*ATR
    assert abs(swing - (97.0 - 0.25)) < 1e-6

    atr15 = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "atr", "mult": 1.5},
        direction="LONG",
        entry=entry,
        current_stop=entry * 0.995,
        atr=atr,
        past=past,
        dist_cache=dist_cache,
    )
    assert atr15 is not None
    dist_cache["atr_1_5"] = abs(entry - atr15)
    dist_cache["swing_20_buf"] = abs(entry - swing)

    hybrid = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "max", "of": ["atr_1_5", "swing_20_buf"]},
        direction="LONG",
        entry=entry,
        current_stop=entry * 0.995,
        atr=atr,
        past=past,
        dist_cache=dist_cache,
    )
    assert hybrid is not None
    # Wider of the two (larger distance → lower stop for long)
    assert hybrid == min(atr15, swing)


def test_sizing_inverse_to_stop_distance() -> None:
    past = _ohlc_df(40)
    # Forward: immediate move up then flat — both stops should win similarly scaled
    fwd_rows = []
    px = 100.0
    for i in range(15):
        if i == 0:
            h, l, c = 100.8, 99.9, 100.5
        else:
            h, l, c = px + 0.3, px - 0.1, px + 0.2
        fwd_rows.append({"Open": px, "High": h, "Low": l, "Close": c})
        px = c
    forward = pd.DataFrame(fwd_rows, index=pd.date_range("2024-03-01", periods=15, freq="D"))

    smap = cb._shadow_stop_placement_fields(
        direction="LONG",
        entry=100.0,
        stop_loss=99.5,
        tp1=101.0,
        tp2=101.5,
        tp3=102.5,
        forward_df=forward,
        past=past,
        max_risk_dollars=50.0,
        timeframe="1d",
        atr=0.8,
        ticker="EURUSD",
    )
    assert "fixed_current" in smap and smap["fixed_current"] is not None
    assert "fixed_200" in smap and smap["fixed_200"] is not None
    # Wider stop → fewer lots (same dollar risk)
    assert smap["fixed_200"]["lots"] < smap["fixed_current"]["lots"]
    assert abs(smap["fixed_current"]["stop"] - 99.5) < 1e-6
    # Dollar risk: lots * 100000 * dist ≈ max_risk
    for name in ("fixed_current", "fixed_200"):
        lots = smap[name]["lots"]
        dist = abs(100.0 - smap[name]["stop"])
        risk = lots * 100000.0 * dist
        assert abs(risk - 50.0) < 0.05
    for name, payload in smap.items():
        if payload is None:
            continue
        assert set(payload.keys()) >= {
            "stop",
            "lots",
            "pnl",
            "exit_reason",
            "nights",
            "stopped_then_target",
        }


def test_stopped_then_target_diagnostic() -> None:
    # Stop hit on bar 0, then TP reached later
    fwd = pd.DataFrame(
        [
            {"Open": 100.0, "High": 100.1, "Low": 99.4, "Close": 99.5},  # stop 99.5
            {"Open": 99.5, "High": 100.2, "Low": 99.4, "Close": 100.0},
            {"Open": 100.0, "High": 101.2, "Low": 99.9, "Close": 101.0},  # TP 101
        ],
        index=pd.date_range("2024-04-01", periods=3, freq="D"),
    )
    assert (
        cb._shadow_stopped_then_target(
            direction="LONG",
            original_tp=101.0,
            forward_df=fwd,
            hit_stop=True,
            candles_to_exit=1,
        )
        is True
    )
    assert (
        cb._shadow_stopped_then_target(
            direction="LONG",
            original_tp=101.0,
            forward_df=fwd,
            hit_stop=False,
            candles_to_exit=1,
        )
        is False
    )


def test_shadow_stop_runner_summary_shape() -> None:
    runner = cb._ShadowStopRunner()
    assert len(runner.curves) == 12
    runner.on_new_day("2024-01-02")
    row = {
        "date": "2024-01-02",
        "outcome": "WIN",
        "entry_price": 100.0,
        "max_risk_dollars": 20.0,
        "shadow_stop": {
            "fixed_current": {
                "stop": 99.5,
                "lots": 0.04,
                "pnl": 10.0,
                "exit_reason": "TP1",
                "nights": 2,
                "stopped_then_target": False,
            },
            "atr_1_5": {
                "stop": 98.5,
                "lots": 0.0133,
                "pnl": 5.0,
                "exit_reason": "STOP",
                "nights": 1,
                "stopped_then_target": True,
            },
        },
    }
    # Fill remaining configs with null-safe skip by omitting them
    runner.process_trade(row)
    runner.finalize_day("2024-01-02")
    summaries = runner.summaries()
    assert "fixed_current" in summaries
    assert "atr_1_5" in summaries
    fc = summaries["fixed_current"]
    for key in (
        "final_capital",
        "peak_capital",
        "max_drawdown_pct",
        "worst_day_pct",
        "positive_months",
        "net_r",
        "r_per_trade",
        "stop_hit_rate",
        "median_stop_distance_pct",
        "stopped_then_target_count",
        "median_lots",
        "pct_below_min_lot",
    ):
        assert key in fc
    assert set(fc["pct_below_min_lot"].keys()) == {
        "acct_25000",
        "acct_50000",
        "acct_100000",
    }
    assert summaries["atr_1_5"]["stopped_then_target_count"] == 1
    assert summaries["atr_1_5"]["stop_hit_rate"] == 100.0
    # Real-curve constants untouched
    assert cb.MIN_STOP_PCT["1d"] == 0.5
    assert cb.MIN_STOP_PCT["1w"] == 0.8
