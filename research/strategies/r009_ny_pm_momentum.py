"""R009: intraday time-series momentum into the NY afternoon.

FX day starts 17:00 New York. ref = close of the first M1 bar stamped >= 17:00 NY.
At the first bar stamped in [13:30, 14:00) NY on a weekday, r1 = close - ref (ref must
come from the previous calendar day; Sunday for Monday). If |r1| >= 15 pips, trade in
the direction of r1 at market. Catastrophe SL 30 pips, no TP. Flat at 16:30 NY (before
the 22:00 UTC funding roll) or if the NY date changes. One trade per day.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")


class R009NyPmMomentum(Strategy):
    name = "r009_ny_pm_momentum"

    def __init__(self, min_move_pips: float = 15.0, sl_pips: float = 30.0, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.min_move_pips, self.sl_pips = float(min_move_pips), float(sl_pips)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref: float | None = None
        self._ref_day: _dt.date | None = None   # NY calendar date of the 17:00 bar that set the ref
        self._traded: set = set()

    def parameters(self) -> dict:
        return {"min_move_pips": self.min_move_pips, "sl_pips": self.sl_pips, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
        loc = ts.astimezone(_NY)
        hm = loc.hour * 60 + loc.minute
        nyday = loc.date()
        if hm >= 17 * 60 and self._ref_day != nyday:
            self._ref, self._ref_day = float(ctx.close), nyday
        if ctx.positions:
            for p in ctx.positions:
                e = p.entry_time
                ets = _dt.datetime(int(e[0:4]), int(e[5:7]), int(e[8:10]), int(e[11:13]), int(e[14:16]), tzinfo=_UTC)
                if hm >= 16 * 60 + 30 or ets.astimezone(_NY).date() != nyday:
                    intents.append(CloseIntent(p.id))
            return intents
        if not (13 * 60 + 30 <= hm < 14 * 60) or nyday in self._traded or self._ref is None:
            return intents
        wd = nyday.weekday()
        if wd >= 5:
            return intents
        prev = nyday - _dt.timedelta(days=1)   # Monday -> Sunday 17:00 open
        if self._ref_day != prev:
            return intents
        self._traded.add(nyday)
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        px = float(ctx.close)
        r1 = px - self._ref
        if abs(r1) < self.min_move_pips * pip:
            return intents
        sl = self.sl_pips * pip
        if r1 > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tag="S"))
        return intents
