"""Per-bar tick and clock features, and the economic calendar."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scalping.costs import CostModel
from scalping.data.bars import ticks_to_m1
from scalping.data.calendar import events, news_distances
from scalping.engine import run_backtest
from scalping.instruments import get_instrument
from scalping.market import build_market, with_min_spread
from scalping.strategies.base import Strategy
from scalping.strategies.timeframe import make_timeframe


def _market(rows):
    frame = pd.DataFrame([{"timestamp": pd.Timestamp(t, tz="UTC"), "bid": b, "ask": a,
                           "bid_volume": 0.0, "ask_volume": 0.0} for t, b, a in rows])
    return build_market(frame, ticks_to_m1(frame))


def test_tick_features_per_minute():
    m = _market([("2024-01-10T10:00:01", 1.1000, 1.10002), ("2024-01-10T10:00:02", 1.1001, 1.10012),
                 ("2024-01-10T10:00:03", 1.1000, 1.10010), ("2024-01-10T10:01:01", 1.1002, 1.10022)])
    assert m.tick_count.tolist() == [3.0, 1.0]
    assert m.upticks.tolist() == [1.0, 1.0]
    assert m.downticks.tolist() == [1.0, 0.0]
    assert m.spread_high_raw[0] == pytest.approx(0.00010)
    floored = with_min_spread(m, 0.0007)
    assert floored.spread_high_raw[0] == pytest.approx(0.00010)   # raw spread survives the broker floor


def test_local_clocks_follow_daylight_saving():
    winter = _market([("2024-01-10T16:00:05", 1.1, 1.10002)])
    summer = _market([("2024-07-10T15:00:05", 1.1, 1.10002)])
    assert winter.london_minute[0] == 16 * 60 and summer.london_minute[0] == 16 * 60   # both the 16:00 London fix
    assert winter.ny_minute[0] == 11 * 60 and summer.ny_minute[0] == 11 * 60


def test_calendar_times_and_distances():
    ev = events()
    nfp = ev[ev["event"] == "US_NFP"]["utc"]
    assert pd.Timestamp("2024-01-05 13:30", tz="UTC") in set(nfp)        # winter: 08:30 ET = 13:30 UTC
    assert pd.Timestamp("2024-06-07 12:30", tz="UTC") in set(nfp)        # summer: 12:30 UTC
    ecb = ev[ev["event"] == "ECB"]["utc"]
    assert pd.Timestamp("2022-06-09 11:45", tz="UTC") in set(ecb)        # 13:45 CEST
    assert pd.Timestamp("2022-07-21 12:15", tz="UTC") in set(ecb)        # 14:15 CEST after the change
    t = np.array([pd.Timestamp("2024-01-05 13:00", tz="UTC").value])
    nxt, last = news_distances(t)
    assert nxt[0] == pytest.approx(30.0)


class _Peek(Strategy):
    name = "peek"
    seen = []

    def on_bar(self, ctx):
        _Peek.seen.append((len(ctx.window), float(ctx.window.tick_count[-1]), float(ctx.window.london_minute[-1])))
        return []


def test_timeframe_wrapper_aggregates_tick_features():
    rows = [(f"2024-01-10T10:{m:02d}:{s:02d}", 1.1, 1.10002) for m in range(15) for s in (1, 2)]
    _Peek.seen = []
    run_backtest(_market(rows), make_timeframe(_Peek, 15)(), get_instrument("EURUSD"), CostModel())
    assert _Peek.seen == [(1, 30.0, 600.0)]   # 15 minutes x 2 ticks; clock at the bar open (10:00 London)
