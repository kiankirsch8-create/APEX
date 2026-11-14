"""R002 variant b: Asia stop-run fade, entered at MARKET only after a completed M5 bar
pokes beyond the 4h channel extreme and closes back inside it (stop flow absorbed).

Why: R002 variant a (resting limit beyond the channel) had pre-cost edge -0.17 (t -1.1),
and R001 showed the same pattern: touch-fills at an extreme are adversely selected,
while close-confirmed entries carry the positive reversion edge. The mechanism (stop
runs in thin Asia liquidity revert) predicts the close back inside is the tell that
the stop flow is done. Channel, session, width filter, TP and time exits unchanged.
"""

from __future__ import annotations

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe


def _minutes_between(a: str, b: str) -> float:
    return float((np.datetime64(b[:19]) - np.datetime64(a[:19])) / np.timedelta64(1, "m"))


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> float:
    prev = close[-period - 1:-1]
    tr = np.maximum(high[-period:], prev) - np.minimum(low[-period:], prev)
    return float(tr.mean())


class R002bAsiaStopRunReject(Strategy):
    name = "r002b_asia_stoprun_reject"

    def __init__(self, channel: int = 48, atr_period: int = 14, sl_buffer_atr: float = 0.5,
                 min_width_pips: float = 8.0, max_width_pips: float = 40.0, max_hold_bars: int = 24,
                 bar_minutes: int = 5, start_hour: int = 23, end_hour: int = 6, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.channel, self.atr_period, self.sl_buffer_atr = int(channel), int(atr_period), float(sl_buffer_atr)
        self.min_width_pips, self.max_width_pips = float(min_width_pips), float(max_width_pips)
        self.max_hold_bars, self.bar_minutes = int(max_hold_bars), int(bar_minutes)
        self.start_hour, self.end_hour = int(start_hour), int(end_hour)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"channel": self.channel, "atr_period": self.atr_period, "sl_buffer_atr": self.sl_buffer_atr,
                "min_width_pips": self.min_width_pips, "max_width_pips": self.max_width_pips,
                "max_hold_bars": self.max_hold_bars, "bar_minutes": self.bar_minutes,
                "start_hour": self.start_hour, "end_hour": self.end_hour, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def _in_window(self, hour: int) -> bool:
        if self.start_hour > self.end_hour:
            return hour >= self.start_hour or hour < self.end_hour
        return self.start_hour <= hour < self.end_hour

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        hour = int(ctx.time[11:13])
        if ctx.positions:
            for p in ctx.positions:
                held = _minutes_between(p.entry_time, ctx.time)
                if 7 <= hour < 22 or held >= self.max_hold_bars * self.bar_minutes:
                    intents.append(CloseIntent(p.id))
            return intents
        need = max(self.channel + 1, self.atr_period + 1)
        if not self._in_window(hour) or len(ctx.window) < need:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        w = ctx.window
        hi = float(w.high[-self.channel - 1:-1].max())   # channel excludes the signal bar
        lo = float(w.low[-self.channel - 1:-1].min())
        width, pip = hi - lo, 10 * ctx.point
        if not (self.min_width_pips * pip <= width <= self.max_width_pips * pip):
            return intents
        bh, bl, bc = float(w.high[-1]), float(w.low[-1]), float(w.close[-1])
        buf = self.sl_buffer_atr * _atr(w.high, w.low, w.close, self.atr_period)
        mid = 0.5 * (hi + lo)
        if bh > hi and bc < hi and not bl < lo:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=bh + buf, tp=mid, tag="S"))
        elif bl < lo and bc > lo and not bh > hi:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=bl - buf, tp=mid, tag="L"))
        return intents


R002bAsiaStopRunRejectM5 = make_timeframe(R002bAsiaStopRunReject, 5)
