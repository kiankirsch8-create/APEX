"""R031: Time-of-day return periodicity (Heston-Korajczyk-Sadka 2010).

Mechanism: recurring institutional flow executed at the same clock time each day
(scheduled rebalancing / execution algorithms, hedging desks working fixed windows)
makes returns in an intraday interval positively autocorrelated with the same interval
on prior days. Each hour slot is traded in the sign of its own trailing 20-day drift.

Rules (M1, stored clock, block-local data only, weekdays):
  Slots h = stored 07..19. Anchor A[d,h] = ctx.close at the first bar with hour == h,
  recorded only if that bar's minute < 5. Slot return r[d,h] = A[d,h+1] - A[d,h].
  At the first bar of slot h (minute < 5) with >= 20 prior r[.,h] in the block:
  t = mean/(sd/sqrt(20)) over the last 20; if |t| >= T: MARKET in sign(mean),
  catastrophe SL 30 pips, no TP, spread <= 2 pips.
  Any open position is closed at the first bar of a new hour (or a new day).

r031_tod_periodicity:     T = 2.0 (eval 1, as registered)
r031b_tod_periodicity_25: T = 2.5 (pre-registered eval 2, dose-response)
See research/LEDGER.md R031.
"""

from __future__ import annotations

import datetime as _dt
import math

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


class R031TodPeriodicity(Strategy):
    name = "r031_tod_periodicity"
    t_threshold = 2.0

    def __init__(self, lookback: int = 20, first_slot: int = 7, last_slot: int = 19,
                 sl_pips: float = 30.0, lots: float = 0.10, max_spread_points: float = 20.0,
                 anchor_max_minute: int = 5):
        self.lookback, self.first_slot, self.last_slot = int(lookback), int(first_slot), int(last_slot)
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self.anchor_max_minute = int(anchor_max_minute)
        self._hist: dict[int, list[float]] = {}
        self._anchors: dict[int, float] = {}   # hour -> anchor price for the current date
        self._date = None
        self._hour = None

    def parameters(self) -> dict:
        return {"t_threshold": self.t_threshold, "lookback": self.lookback,
                "first_slot": self.first_slot, "last_slot": self.last_slot, "sl_pips": self.sl_pips,
                "lots": self.lots, "max_spread_points": self.max_spread_points,
                "anchor_max_minute": self.anchor_max_minute}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        d = _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))
        hour, minute = int(t[11:13]), int(t[14:16])
        if d == self._date and hour == self._hour:
            return intents
        # first bar of a new hour (or new day)
        if d != self._date:
            self._anchors = {}
        self._date, self._hour = d, hour
        for p in ctx.positions:
            intents.append(CloseIntent(p.id))
        if d.weekday() > 4 or minute >= self.anchor_max_minute:
            return intents
        price = float(ctx.close)
        self._anchors[hour] = price
        prev = hour - 1
        if prev in self._anchors and self.first_slot <= prev <= self.last_slot:
            self._hist.setdefault(prev, []).append(price - self._anchors[prev])
        if not (self.first_slot <= hour <= self.last_slot):
            return intents
        hist = self._hist.get(hour, [])
        n = self.lookback
        if len(hist) < n:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        xs = hist[-n:]
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        if var <= 0:
            return intents
        tstat = mean / math.sqrt(var / n)
        if abs(tstat) < self.t_threshold:
            return intents
        sl = self.sl_pips * 10 * ctx.point
        if tstat > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=price - sl, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=price + sl, tag="S"))
        return intents


class R031bTodPeriodicity25(R031TodPeriodicity):
    name = "r031b_tod_periodicity_25"
    t_threshold = 2.5
