"""Tests for zone-based shadow sizing / filter layers."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pandas as pd

import continuous_backtester as cb


def _row(
    *,
    date: str = "2024-01-02",
    ticker: str = "EURUSD",
    timeframe: str = "1h",
    direction: str = "LONG",
    zone_pct: float = 25.0,
    pnl: float = 100.0,
    trade_r: float = 1.0,
    ab: float = 1.0,
    confluence: int = 2,
    entry: float = 1.10,
    outcome: str = "WIN",
) -> dict[str, Any]:
    return {
        "date": date,
        "ticker": ticker,
        "timeframe": timeframe,
        "direction": direction,
        "outcome": outcome,
        "pnl_dollars": pnl,
        "pnl_r_net": trade_r,
        "ab_throttle": ab,
        "entry_price": entry,
        "zone_position_pct": zone_pct,
        "zone_pct": zone_pct,
        "zone_label": "DISCOUNT" if zone_pct < 30 else "EQUILIBRIUM",
        "strategy_confluence_count": confluence,
        "confluence_points": confluence,
    }


def test_zone_config_shape() -> None:
    assert cb.SHADOW_ZONE_ENABLED is True
    names = [n for n, _ in cb.SHADOW_ZONE_CONFIGS]
    assert "zone_size_disc_15" in names
    assert "zone_lb_7" in names
    assert "zone_lb_current" in names
    assert "zone_disc_x_nonjpy" in names
    assert "price_zone" not in str(cb.SHADOW_ZONE_CONFIGS)


def test_zone_ladder_helpers() -> None:
    ladder = [(50.0, 1.25), (30.0, 1.75), (20.0, 2.5)]
    assert cb._zone_ladder_mult_below(15.0, ladder) == 2.5
    assert cb._zone_ladder_mult_below(25.0, ladder) == 1.75
    assert cb._zone_ladder_mult_below(40.0, ladder) == 1.25
    assert cb._zone_ladder_mult_below(60.0, ladder) == 1.0

    up = [(50.0, 1.25), (70.0, 1.75), (80.0, 2.5)]
    assert cb._zone_ladder_mult_above(85.0, up) == 2.5
    assert cb._zone_ladder_mult_above(75.0, up) == 1.75
    assert cb._zone_ladder_mult_above(55.0, up) == 1.25
    assert cb._zone_ladder_mult_above(40.0, up) == 1.0


def test_zone_sizing_and_long_only() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-02")
    ctx = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=15.0, pnl=100.0))
    out = runner.process_trade(ctx)
    # deep discount → 2.5x on disc_deep_25 and ladder
    assert out["zone_size_disc_deep_25"]["taken"] is True
    assert out["zone_size_disc_deep_25"]["mult"] == 2.5
    assert out["zone_size_disc_deep_25"]["pnl"] == 250.0
    assert out["zone_size_disc_ladder"]["mult"] == 2.5

    # SHORT blocked on all curves
    short = cb._shadow_zone_trade_ctx_from_row(_row(direction="SHORT", zone_pct=15.0))
    out_s = runner.process_trade(short)
    assert out_s["zone_size_disc_15"]["blocked"] is True
    assert out_s["zone_size_disc_15"]["reason"] == "short_blocked"


def test_zone_filters() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-02")
    mid = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=55.0, pnl=50.0))
    out = runner.process_trade(mid)
    assert out["zone_only_disc"]["blocked"] is True
    assert out["zone_only_prem"]["blocked"] is True
    assert out["zone_skip_extreme_prem"]["taken"] is True  # 55 < 90

    deep = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=5.0, pnl=50.0))
    out2 = runner.process_trade(deep)
    assert out2["zone_skip_extreme_disc"]["blocked"] is True
    assert out2["zone_only_disc_deep"]["taken"] is True


def test_zone_conf_gates() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-02")
    # Premium with weak confluence → gate blocks
    weak = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=75.0, confluence=1, pnl=40.0))
    out = runner.process_trade(weak)
    assert out["zone_conf_gate"]["blocked"] is True
    assert out["zone_conf_gate_strict"]["blocked"] is True
    # Premium with confluence 2 → gate ok, strict still blocks
    ok = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=75.0, confluence=2, pnl=40.0))
    out2 = runner.process_trade(ok)
    assert out2["zone_conf_gate"]["taken"] is True
    assert out2["zone_conf_gate_strict"]["blocked"] is True
    # conf_size: discount + conf≥2 → 2x; premium → weak 0.5
    disc = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=40.0, confluence=2, pnl=100.0))
    out3 = runner.process_trade(disc)
    assert out3["zone_conf_size"]["mult"] == 2.0
    assert out3["zone_conf_size"]["pnl"] == 200.0
    prem = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=80.0, confluence=3, pnl=100.0))
    out4 = runner.process_trade(prem)
    assert out4["zone_conf_size"]["mult"] == 0.5


def test_zone_lookback_recompute_and_distribution() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-10")

    def _fake_lookback(ticker: str, date_str: str, entry: float, days: int) -> float:
        # Distinct values per lookback so distributions diverge from "current"
        return {7: 15.0, 14: 35.0, 30: 55.0}[int(days)]

    with patch.object(cb, "_zone_pct_from_daily_lookback", side_effect=_fake_lookback):
        ctx = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=45.0, pnl=80.0, entry=1.1))
        out = runner.process_trade(ctx)

    assert out["zone_lb_7"]["zone_pct"] == 15.0
    assert out["zone_lb_7"]["mult"] == 2.5  # <20 → ladder top
    assert out["zone_lb_14"]["zone_pct"] == 35.0
    assert out["zone_lb_14"]["mult"] == 1.25  # <50
    assert out["zone_lb_30"]["zone_pct"] == 55.0
    assert out["zone_lb_30"]["mult"] == 1.0
    assert out["zone_lb_current"]["zone_pct"] == 45.0
    assert out["zone_lb_current"]["mult"] == 1.25

    runner.finalize_day("2024-01-10")
    summaries = runner.summaries()
    for name in ("zone_lb_7", "zone_lb_14", "zone_lb_30", "zone_lb_current"):
        dist = summaries[name]["zone_pct_distribution"]
        assert dist["n"] == 1
        assert "max_drawdown_pct" in summaries[name]
        assert "net_r" in summaries[name]
        assert "r_per_trade" in summaries[name]


def test_zone_interactions() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-02")
    # Unthrottled only when ab==1.0
    throttled = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=15.0, ab=0.18, pnl=20.0))
    out = runner.process_trade(throttled)
    assert out["zone_disc_x_unthrottled"]["blocked"] is True

    full = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=15.0, ab=1.0, pnl=20.0))
    out2 = runner.process_trade(full)
    assert out2["zone_disc_x_unthrottled"]["taken"] is True
    assert out2["zone_disc_x_unthrottled"]["mult"] == 2.5

    # Weekly only
    h1 = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=15.0, timeframe="1h"))
    assert runner.process_trade(h1)["zone_disc_x_weekly"]["blocked"] is True
    w1 = cb._shadow_zone_trade_ctx_from_row(_row(zone_pct=15.0, timeframe="1w"))
    assert runner.process_trade(w1)["zone_disc_x_weekly"]["taken"] is True

    # Non-JPY
    jpy = cb._shadow_zone_trade_ctx_from_row(_row(ticker="USDJPY", zone_pct=15.0))
    assert runner.process_trade(jpy)["zone_disc_x_nonjpy"]["blocked"] is True
    eur = cb._shadow_zone_trade_ctx_from_row(_row(ticker="EURUSD", zone_pct=15.0))
    assert runner.process_trade(eur)["zone_disc_x_nonjpy"]["taken"] is True


def test_zone_pct_from_daily_lookback_math() -> None:
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {
            "Open": [1.0] * 10,
            "High": [1.20] * 10,
            "Low": [1.00] * 10,
            "Close": [1.10] * 10,
        },
        index=idx,
    )
    # Last 7 days: high 1.20 low 1.00; entry 1.05 → 25%
    with patch.object(cb, "_get_daily_past_for_zone", return_value=df):
        zp = cb._zone_pct_from_daily_lookback("EURUSD", "2024-01-10", 1.05, 7)
    assert zp == 25.0


def test_zone_summary_reports_drawdown() -> None:
    runner = cb._ShadowZoneRunner()
    runner.on_new_day("2024-01-02")
    # Big loss at 2.5x sizing → drawdown visible
    loss = cb._shadow_zone_trade_ctx_from_row(
        _row(zone_pct=15.0, pnl=-400.0, trade_r=-2.0, outcome="LOSS")
    )
    runner.process_trade(loss)
    runner.finalize_day("2024-01-02")
    s = runner.summaries()["zone_size_disc_ladder"]
    assert s["max_drawdown_pct"] > 0
    assert s["trades_taken"] == 1
    assert s["final_capital"] < cb.STARTING_CAPITAL
