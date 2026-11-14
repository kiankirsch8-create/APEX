"""R004 variant c: local-hours depreciation on DST-aware LOCAL clocks.

Variants a/b used fixed UTC times. Local trading hours move by an hour against UTC
with European/US daylight saving, so a fixed UTC window drifts out of the "only one
region open" period for half the year. The mechanism is net local-region flow, so
the legs are defined on the local clocks that set those hours:

  Europe leg: SELL at 08:00 Europe/Berlin, close at 08:00 America/New_York
              (European hours before US participants arrive).
  US leg:     BUY at 16:00 Europe/London (London close), close at 16:00 America/New_York
              (US-only hours; always ends <= 21:00 UTC, before the 22:00 funding roll).

Same catastrophe stop (50 pips), no TP, market entries, weekdays, M1 decisions.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_BER = ZoneInfo("Europe/Berlin")
_LDN = ZoneInfo("Europe/London")
_NY = ZoneInfo("America/New_York")


def _local_hm(ts: _dt.datetime, tz) -> int:
    loc = ts.astimezone(tz)
    return loc.hour * 60 + loc.minute


class R004cLocalHoursDst(Strategy):
    name = "r004c_local_hours_dst"

    def __init__(self, sl_pips: float = 50.0, lots: float = 0.10, max_spread_points: float = 20.0):
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self._done: set = set()

    def parameters(self) -> dict:
        return {"sl_pips": self.sl_pips, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
        day = t[:10]
        ny = _local_hm(ts, _NY)
        if ctx.positions:
            for p in ctx.positions:
                if (p.tag == "EU" and ny >= 8 * 60) or (p.tag == "US" and ny >= 16 * 60) or p.entry_time[:10] != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if ts.weekday() >= 5 or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        sl = self.sl_pips * 10 * ctx.point
        px = ctx.close
        ber, ldn = _local_hm(ts, _BER), _local_hm(ts, _LDN)
        if 8 * 60 <= ber < 8 * 60 + 30 and ny < 8 * 60 and (day, "EU") not in self._done:
            self._done.add((day, "EU"))
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tag="EU"))
        elif 16 * 60 <= ldn < 16 * 60 + 30 and ny < 16 * 60 and (day, "US") not in self._done:
            self._done.add((day, "US"))
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tag="US"))
        return intents
