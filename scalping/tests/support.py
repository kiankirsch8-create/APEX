"""Shared builders for scalping tests. Not part of the backtest runtime."""

from __future__ import annotations

import pandas as pd

from scalping.data.bars import ticks_to_m1
from scalping.market import MarketData, build_market
from scalping.strategies.base import Strategy


def market_from_minutes(minutes: list[list[tuple[float, float]]], start: str = "2020-01-06T10:00:00") -> MarketData:
    """Each inner list is the (bid, ask) ticks of one minute, in order."""
    origin = pd.Timestamp(start, tz="UTC")
    rows = []
    for bar_i, ticks in enumerate(minutes):
        for tick_i, (bid, ask) in enumerate(ticks):
            rows.append(
                {
                    "timestamp": origin + pd.Timedelta(minutes=bar_i, seconds=tick_i),
                    "bid": bid,
                    "ask": ask,
                    "bid_volume": 1.0,
                    "ask_volume": 1.0,
                }
            )
    frame = pd.DataFrame(rows)
    return build_market(frame, ticks_to_m1(frame))


class Scripted(Strategy):
    """Emits a fixed intent list when it is shown a given closed bar."""

    name = "scripted"

    def __init__(self, plan: dict[int, list]):
        self.plan = plan
        self.seen: list[int] = []

    def on_bar(self, ctx):
        self.seen.append(ctx.bar_index)
        return list(self.plan.get(ctx.bar_index, []))
