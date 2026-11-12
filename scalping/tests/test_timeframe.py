"""Higher-timeframe wrapper: cadence, no lookahead, gaps, parameter scaling."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from scalping.costs import CostModel
from scalping.data.bars import ticks_to_m1
from scalping.engine import run_backtest
from scalping.instruments import get_instrument
from scalping.market import build_market
from scalping.strategies import get_strategy
from scalping.strategies.base import Strategy
from scalping.strategies.timeframe import make_timeframe, scaled_defaults

EURUSD = get_instrument("EURUSD")


class Recorder(Strategy):
    name = "recorder"
    calls: list = []

    def __init__(self, sl_points: float = 10.0, max_spread_points: float = 30.0):
        self.sl_points = sl_points
        self.max_spread_points = max_spread_points

    def parameters(self):
        return {"sl_points": self.sl_points, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx):
        Recorder.calls.append((ctx.time, len(ctx.window), float(ctx.window.close[-1]), ctx.high, ctx.low))
        return []


def _market(minutes_present):
    origin = pd.Timestamp("2020-01-06T10:00:00", tz="UTC")
    rows = []
    for m in minutes_present:
        price = 1.1 + m * 1e-5
        rows.append({"timestamp": origin + pd.Timedelta(minutes=m, seconds=5),
                     "bid": price, "ask": price + 2e-5, "bid_volume": 0.0, "ask_volume": 0.0})
        rows.append({"timestamp": origin + pd.Timedelta(minutes=m, seconds=50),
                     "bid": price + 3e-5, "ask": price + 5e-5, "bid_volume": 0.0, "ask_volume": 0.0})
    frame = pd.DataFrame(rows)
    return build_market(frame, ticks_to_m1(frame))


def _run(minutes_present, tf=15):
    Recorder.calls = []
    cls = make_timeframe(Recorder, tf)
    run_backtest(_market(minutes_present), cls(), EURUSD, CostModel(account_currency="EUR"))
    return Recorder.calls


def test_called_once_per_completed_bucket_on_its_last_minute():
    calls = _run(range(60))
    assert [c[0][11:16] for c in calls] == ["10:14", "10:29", "10:44", "10:59"]
    assert [c[1] for c in calls] == [1, 2, 3, 4]


def test_window_holds_only_completed_bars_no_lookahead():
    calls = _run(range(60))
    # close of the 15-minute bar = last mid of its last minute (minute 14, 29, ...)
    for (_, _, close, high, low), last_min in zip(calls, (14, 29, 44, 59)):
        mid_last = 1.1 + last_min * 1e-5 + 4e-5
        assert close == pytest.approx(mid_last)
        assert high == pytest.approx(mid_last)       # prices rise every minute
        assert low == pytest.approx(1.1 + (last_min - 14) * 1e-5 + 1e-5)


def test_missing_closing_minute_fires_on_next_bar_without_partial_data():
    present = [m for m in range(30) if m != 14]
    calls = _run(present)
    assert calls[0][0][11:16] == "10:15"
    assert calls[0][1] == 1
    assert calls[0][2] == pytest.approx(1.1 + 13 * 1e-5 + 4e-5)  # minute 15 not included


def test_point_parameters_scale_with_sqrt_minutes_but_spread_cap_does_not():
    p = scaled_defaults(Recorder, 60)
    assert p["sl_points"] == pytest.approx(10.0 * math.sqrt(60), abs=0.01)
    assert p["max_spread_points"] == 30.0


def test_registered_variants_resolve():
    cls = get_strategy("bollinger_reversion@M15")
    assert cls.name == "bollinger_reversion@M15"
    assert cls().parameters()["timeframe_minutes"] == 15
