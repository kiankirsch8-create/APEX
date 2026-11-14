"""R001: Asia vol-band fade with resting limit entries and a large-snap filter.

See research/LEDGER.md R001 for mechanism and the pre-registered rules.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe


def _minutes_between(a: str, b: str) -> float:
    return float((np.datetime64(b[:19]) - np.datetime64(a[:19])) / np.timedelta64(1, "m"))


class R001AsiaBandFade(Strategy):
    name = "r001_asia_band_fade"

    def __init__(self, period: int = 20, num_std: float = 2.5, sl_std: float = 2.0, min_snap_pips: float = 5.0,
                 max_hold_bars: int = 8, bar_minutes: int = 15, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.period, self.num_std, self.sl_std = int(period), float(num_std), float(sl_std)
        self.min_snap_pips, self.max_hold_bars, self.bar_minutes = float(min_snap_pips), int(max_hold_bars), int(bar_minutes)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"period": self.period, "num_std": self.num_std, "sl_std": self.sl_std,
                "min_snap_pips": self.min_snap_pips, "max_hold_bars": self.max_hold_bars,
                "bar_minutes": self.bar_minutes, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        hour = int(ctx.time[11:13])
        in_asia = hour >= 22 or hour < 6
        if ctx.positions:
            for p in ctx.positions:
                held = _minutes_between(p.entry_time, ctx.time)
                if 7 <= hour < 22 or held >= self.max_hold_bars * self.bar_minutes:
                    intents.append(CloseIntent(p.id))
            return intents
        if not in_asia or len(ctx.window) < self.period:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        close = ctx.window.close[-self.period:]
        mean, sd = float(close.mean()), float(close.std())
        pip = 10 * ctx.point
        if sd <= 0 or self.num_std * sd < self.min_snap_pips * pip:
            return intents
        lower, upper = mean - self.num_std * sd, mean + self.num_std * sd
        sl = self.sl_std * sd
        intents.append(OrderIntent(OrderType.LIMIT, Side.BUY, self.lots, price=lower, sl=lower - sl, tp=mean, tag="L"))
        intents.append(OrderIntent(OrderType.LIMIT, Side.SELL, self.lots, price=upper, sl=upper + sl, tp=mean, tag="S"))
        return intents


R001AsiaBandFadeM15 = make_timeframe(R001AsiaBandFade, 15)
