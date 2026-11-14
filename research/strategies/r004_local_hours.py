"""R004: local-trading-hours depreciation (Breedon & Ranaldo 2013).

EUR tends to weaken during European hours, USD during US hours, because domestic
agents are net buyers of foreign currency while their home market is open.

Europe leg: SELL at 07:00 UTC, close at 12:00 UTC.
US leg:     BUY  at 13:00 UTC, close at 20:00 UTC (before the 22:00 funding roll).
Fixed times, market entries, wide catastrophe stop, no TP. M1 decisions.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


def _weekday(t: str) -> int:
    return _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10])).weekday()


class R004LocalHours(Strategy):
    name = "r004_local_hours"

    def __init__(self, eu_leg: bool = True, us_leg: bool = True, sl_pips: float = 50.0,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.eu_leg, self.us_leg = bool(eu_leg), bool(us_leg)
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self._done: set = set()

    def parameters(self) -> dict:
        return {"eu_leg": self.eu_leg, "us_leg": self.us_leg, "sl_pips": self.sl_pips,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        hm = int(t[11:13]) * 60 + int(t[14:16])
        day = t[:10]
        if ctx.positions:
            for p in ctx.positions:
                if (p.tag == "EU" and hm >= 12 * 60) or (p.tag == "US" and hm >= 20 * 60) or p.entry_time[:10] != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if _weekday(t) >= 5 or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        sl = self.sl_pips * 10 * ctx.point
        px = ctx.close
        if self.eu_leg and 7 * 60 <= hm < 7 * 60 + 30 and (day, "EU") not in self._done:
            self._done.add((day, "EU"))
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tag="EU"))
        elif self.us_leg and 13 * 60 <= hm < 13 * 60 + 30 and (day, "US") not in self._done:
            self._done.add((day, "US"))
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tag="US"))
        return intents
