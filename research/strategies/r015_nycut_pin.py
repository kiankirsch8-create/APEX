"""R015: 10:00 New York option-cut pinning toward the nearest 00/50 strike.

Dealers net long gamma at a strike hedge by selling rallies above it and buying dips below it,
pulling spot toward the strike into expiry (Ni, Pearson & Poteshman 2005; Avellaneda & Lipkin
2003). EURUSD vanillas expire daily at the 10:00 NY cut; large strikes cluster at 00/50.

Rules (America/New_York clock, weekdays):
  At the first bar with NY time in [09:00, 09:15) and spread <= 2 pips:
    L = nearest multiple of 0.0050 to close; dist = |close - L|.
    If 4 <= dist <= 12 pips: MARKET order toward L, TP = L, SL = entry -/+ 12 pips away from L.
  Close at the first bar with NY time >= 10:00, or on NY date change. One trade per day.
See research/LEDGER.md R015.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")


class R015NYCutPin(Strategy):
    name = "r015_nycut_pin"

    def __init__(self, entry_min: int = 9 * 60, entry_window: int = 15, cut_min: int = 10 * 60,
                 grid: float = 0.0050, min_dist_pips: float = 4.0, max_dist_pips: float = 12.0,
                 sl_pips: float = 12.0, lots: float = 0.10, max_spread_points: float = 20.0):
        self.entry_min, self.entry_window, self.cut_min = int(entry_min), int(entry_window), int(cut_min)
        self.grid, self.min_dist_pips, self.max_dist_pips = float(grid), float(min_dist_pips), float(max_dist_pips)
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self._done: set = set()

    def parameters(self) -> dict:
        return {"entry_min": self.entry_min, "entry_window": self.entry_window, "cut_min": self.cut_min,
                "grid": self.grid, "min_dist_pips": self.min_dist_pips, "max_dist_pips": self.max_dist_pips,
                "sl_pips": self.sl_pips, "lots": self.lots, "max_spread_points": self.max_spread_points}

    @staticmethod
    def _ny(t: str) -> _dt.datetime:
        ts = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
        return ts.astimezone(_NY)

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        loc = self._ny(ctx.time)
        hm, day = loc.hour * 60 + loc.minute, loc.date()
        if ctx.positions:
            for p in ctx.positions:
                if hm >= self.cut_min or self._ny(p.entry_time).date() != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if day.weekday() >= 5 or day in self._done:
            return intents
        if not (self.entry_min <= hm < self.entry_min + self.entry_window):
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        self._done.add(day)
        px = ctx.close
        level = round(px / self.grid) * self.grid
        pip = 10 * ctx.point
        dist = abs(px - level) / pip
        if not (self.min_dist_pips <= dist <= self.max_dist_pips):
            return intents
        sl = self.sl_pips * pip
        if level > px:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=px - sl, tp=level, tag="P"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=px + sl, tp=level, tag="P"))
        return intents
