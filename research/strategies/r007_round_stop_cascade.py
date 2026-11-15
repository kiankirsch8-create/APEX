"""R007: round-number stop-loss cascades (Osler 2005). Stop entry just beyond an untouched 00/50 level.

See research/LEDGER.md R007 for mechanism and the pre-registered rules.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe


def _minutes_between(a: str, b: str) -> float:
    return float((np.datetime64(b[:19]) - np.datetime64(a[:19])) / np.timedelta64(1, "m"))


class R007RoundStopCascade(Strategy):
    name = "r007_round_stop_cascade"

    def __init__(self, step_pips: float = 50.0, min_dist_pips: float = 2.0, max_dist_pips: float = 10.0,
                 lookback: int = 12, touch_pad_pips: float = 0.5, entry_off_pips: float = 3.0,
                 tp_beyond_pips: float = 13.0, sl_back_pips: float = 5.0, max_hold_min: int = 60,
                 skip_hours: tuple = (21, 22), lots: float = 0.10, max_spread_points: float = 20.0):
        self.step_pips, self.min_dist_pips, self.max_dist_pips = float(step_pips), float(min_dist_pips), float(max_dist_pips)
        self.lookback, self.touch_pad_pips, self.entry_off_pips = int(lookback), float(touch_pad_pips), float(entry_off_pips)
        self.tp_beyond_pips, self.sl_back_pips, self.max_hold_min = float(tp_beyond_pips), float(sl_back_pips), int(max_hold_min)
        self.skip_hours, self.lots, self.max_spread_points = tuple(skip_hours), float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"step_pips": self.step_pips, "min_dist_pips": self.min_dist_pips, "max_dist_pips": self.max_dist_pips,
                "lookback": self.lookback, "touch_pad_pips": self.touch_pad_pips,
                "entry_off_pips": self.entry_off_pips, "tp_beyond_pips": self.tp_beyond_pips,
                "sl_back_pips": self.sl_back_pips, "max_hold_min": self.max_hold_min,
                "skip_hours": list(self.skip_hours), "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        if ctx.positions:
            for p in ctx.positions:
                if _minutes_between(p.entry_time, ctx.time) >= self.max_hold_min:
                    intents.append(CloseIntent(p.id))
            return intents
        if len(ctx.window) < self.lookback:
            return intents
        t = datetime.fromisoformat(ctx.time[:19])
        if t.weekday() >= 5 or t.hour in self.skip_hours:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        step = self.step_pips * pip
        w = ctx.window
        close = float(w.close[-1])
        hi, lo = float(w.high[-self.lookback:].max()), float(w.low[-self.lookback:].min())
        pad = self.touch_pad_pips * pip
        up = math.ceil(close / step - 1e-9) * step
        dn = math.floor(close / step + 1e-9) * step
        d = (up - close) / pip
        if self.min_dist_pips <= d <= self.max_dist_pips and hi < up - pad:
            intents.append(OrderIntent(OrderType.STOP, Side.BUY, self.lots, price=up + self.entry_off_pips * pip,
                                       sl=up - self.sl_back_pips * pip, tp=up + self.tp_beyond_pips * pip, tag="L"))
        d = (close - dn) / pip
        if self.min_dist_pips <= d <= self.max_dist_pips and lo > dn + pad:
            intents.append(OrderIntent(OrderType.STOP, Side.SELL, self.lots, price=dn - self.entry_off_pips * pip,
                                       sl=dn + self.sl_back_pips * pip, tp=dn - self.tp_beyond_pips * pip, tag="S"))
        return intents


R007RoundStopCascadeM5 = make_timeframe(R007RoundStopCascade, 5)
