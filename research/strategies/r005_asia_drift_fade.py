"""R005: London-open fade of the Asia-session drift.

Asia ref = open of the first M1 bar at/after 22:00 UTC (previous day). At 07:00 UTC,
if the overnight drift is >= 15 pips, fade it at market: TP at the Asia ref (full
retrace), SL 1.0x drift beyond entry (min 15 pips), flat at 12:00 UTC. One trade/day.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


def _weekday(t: str) -> int:
    return _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10])).weekday()


class R005AsiaDriftFade(Strategy):
    name = "r005_asia_drift_fade"

    def __init__(self, min_drift_pips: float = 15.0, sl_mult: float = 1.0, min_sl_pips: float = 15.0,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.min_drift_pips, self.sl_mult, self.min_sl_pips = float(min_drift_pips), float(sl_mult), float(min_sl_pips)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._ref: float | None = None
        self._ref_day: str | None = None   # date (UTC) of the 22:00 bar that set the ref
        self._traded: set = set()

    def parameters(self) -> dict:
        return {"min_drift_pips": self.min_drift_pips, "sl_mult": self.sl_mult, "min_sl_pips": self.min_sl_pips,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        hm = int(t[11:13]) * 60 + int(t[14:16])
        day = t[:10]
        if hm >= 22 * 60 and self._ref_day != day:
            # ctx.open is the open of the bar that just completed: first bar at/after 22:00
            self._ref, self._ref_day = float(ctx.open), day
        if ctx.positions:
            for p in ctx.positions:
                if hm >= 12 * 60 or p.entry_time[:10] != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if not (7 * 60 <= hm < 7 * 60 + 30) or day in self._traded or self._ref is None:
            return intents
        wd = _weekday(t)
        if wd >= 5:
            return intents
        prev = (_dt.date.fromisoformat(day) - _dt.timedelta(days=3 if wd == 0 else 1)).isoformat()
        if wd == 0:
            prev = (_dt.date.fromisoformat(day) - _dt.timedelta(days=1)).isoformat()  # Sunday open
        if self._ref_day != prev:
            return intents
        self._traded.add(day)
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        px = float(ctx.close)
        drift = px - self._ref
        if abs(drift) < self.min_drift_pips * pip:
            return intents
        sl = max(self.sl_mult * abs(drift), self.min_sl_pips * pip)
        if drift > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tp=self._ref, tag="S"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tp=self._ref, tag="L"))
        return intents
