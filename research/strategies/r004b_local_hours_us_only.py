"""R004 variant b: local-hours depreciation, US leg moved to US-ONLY hours.

Variant a (US leg 13-20 UTC) showed the Europe leg at +1.37 pips (t 1.7) and the
US leg at -0.34: 13-16 UTC is the London/NY overlap, where both regions' local
flows are present and net out. The mechanism is net local demand, so the US leg
should sit where only the US is open: after the London close (16:00 UTC) to 21:00
UTC (still before the 22:00 funding roll). Europe leg unchanged.

Original R004 description:
R004: local-trading-hours depreciation (Breedon & Ranaldo 2013).

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


class R004bLocalHoursUsOnly(Strategy):
    name = "r004b_local_hours_us_only"

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
                if (p.tag == "EU" and hm >= 12 * 60) or (p.tag == "US" and hm >= 21 * 60) or p.entry_time[:10] != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if _weekday(t) >= 5 or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        sl = self.sl_pips * 10 * ctx.point
        px = ctx.close
        if self.eu_leg and 7 * 60 <= hm < 7 * 60 + 30 and (day, "EU") not in self._done:
            self._done.add((day, "EU"))
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tag="EU"))
        elif self.us_leg and 16 * 60 <= hm < 16 * 60 + 30 and (day, "US") not in self._done:
            self._done.add((day, "US"))
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tag="US"))
        return intents
