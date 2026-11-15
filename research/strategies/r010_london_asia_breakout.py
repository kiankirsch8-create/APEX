"""R010: London-open breakout of the Asia range.

Asia range = high/low of M1 bars stamped [00:00, 07:00) Europe/London (same day, >= 300 bars).
At the first bar stamped >= 07:00 London on a weekday, if 10 <= range <= 60 pips and spread
<= 2 pips: BUY STOP at high + 1 pip, SELL STOP at low - 1 pip (a side is skipped if price is
already through its level). SL = range midpoint, TP = entry +/- 1.0 x range. When one fills
the other is cancelled (one trade per day). Unfilled orders cancelled at 10:00 London;
positions closed at 16:00 London.

Implementation note: orders are placed once and left resting (not cancelled and re-sent each
bar); the levels are identical either way, so this is the registered rule.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_LDN = ZoneInfo("Europe/London")


def _ldn(t: str) -> _dt.datetime:
    ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
    return ts.astimezone(_LDN)


class R010LondonAsiaBreakout(Strategy):
    name = "r010_london_asia_breakout"

    def __init__(self, min_range_pips: float = 10.0, max_range_pips: float = 60.0, buffer_pips: float = 1.0,
                 tp_mult: float = 1.0, min_bars: int = 300, lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_range_pips, self.max_range_pips = float(min_range_pips), float(max_range_pips)
        self.buffer_pips, self.tp_mult, self.min_bars = float(buffer_pips), float(tp_mult), int(min_bars)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._day: _dt.date | None = None
        self._hi = -1e9
        self._lo = 1e9
        self._n = 0
        self._armed: set = set()

    def parameters(self) -> dict:
        return {"min_range_pips": self.min_range_pips, "max_range_pips": self.max_range_pips,
                "buffer_pips": self.buffer_pips, "tp_mult": self.tp_mult, "min_bars": self.min_bars,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = []
        loc = _ldn(ctx.time)
        day = loc.date()
        hm = loc.hour * 60 + loc.minute
        if day != self._day:
            self._day, self._hi, self._lo, self._n = day, -1e9, 1e9, 0
        if hm < 7 * 60:
            self._hi, self._lo, self._n = max(self._hi, float(ctx.high)), min(self._lo, float(ctx.low)), self._n + 1

        if ctx.positions:
            intents += [CancelIntent(o.id) for o in ctx.pending]   # OCO
            for p in ctx.positions:
                if hm >= 16 * 60 or _ldn(p.entry_time).date() != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if ctx.pending:
            if hm >= 10 * 60 or hm < 7 * 60 or any(o.tag[1:] != day.isoformat() for o in ctx.pending):
                intents += [CancelIntent(o.id) for o in ctx.pending]
            return intents

        if hm < 7 * 60 or hm >= 10 * 60 or day in self._armed or day.weekday() >= 5:
            return intents
        self._armed.add(day)
        if self._n < self.min_bars or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        rng = self._hi - self._lo
        if not (self.min_range_pips * pip <= rng <= self.max_range_pips * pip):
            return intents
        mid = 0.5 * (self._hi + self._lo)
        buy_px = self._hi + self.buffer_pips * pip
        sell_px = self._lo - self.buffer_pips * pip
        px = float(ctx.close)
        tag = day.isoformat()
        if px < buy_px:
            intents.append(OrderIntent(OrderType.STOP, Side.BUY, self.lots, price=buy_px, sl=mid,
                                       tp=buy_px + self.tp_mult * rng, tag="L" + tag))
        if px > sell_px:
            intents.append(OrderIntent(OrderType.STOP, Side.SELL, self.lots, price=sell_px, sl=mid,
                                       tp=sell_px - self.tp_mult * rng, tag="S" + tag))
        return intents
