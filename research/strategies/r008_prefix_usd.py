"""R008: pre-London-fix dollar appreciation (Krohn, Mueller & Whelan 2024).

Short EURUSD 13:00 -> 15:55 Europe/London (DST-aware), weekdays, market entry,
catastrophe stop 40 pips, no TP, M1 decisions. See research/LEDGER.md R008.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_LDN = ZoneInfo("Europe/London")


class R008PrefixUsd(Strategy):
    name = "r008_prefix_usd"

    def __init__(self, entry_min: int = 13 * 60, exit_min: int = 15 * 60 + 55, sl_pips: float = 40.0,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.entry_min, self.exit_min, self.sl_pips = int(entry_min), int(exit_min), float(sl_pips)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._done: set = set()

    def parameters(self) -> dict:
        return {"entry_min": self.entry_min, "exit_min": self.exit_min, "sl_pips": self.sl_pips,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
        loc = ts.astimezone(_LDN)
        hm, day = loc.hour * 60 + loc.minute, loc.date().isoformat()
        if ctx.positions:
            for p in ctx.positions:
                if hm >= self.exit_min or p.entry_time[:10] != t[:10]:
                    intents.append(CloseIntent(p.id))
            return intents
        if loc.weekday() >= 5 or ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        if self.entry_min <= hm < self.entry_min + 30 and day not in self._done:
            self._done.add(day)
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots,
                                       sl=ctx.close + self.sl_pips * 10 * ctx.point, tag="FIX"))
        return intents
