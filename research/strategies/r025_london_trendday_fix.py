"""R025: London trend-day continuation into the 4pm fix (fix-benchmarked order execution).

Large real-money orders benchmarked to the WM/R 4pm London fix are worked over the London day.
A large one-directional morning that sits at the extreme of its range at midday is the footprint
of such an order still being executed; trade in its direction until just before the fix.

Rules (M15 decisions on completed bars, Europe/London clock, weekdays):
  O = close at first decision with London time in [08:00, 08:15). Track high/low of decision
  closes from O through 12:00. At first decision in [12:00, 12:15):
    M = close - O; sigma = sd of the last 480 M15 close changes (needs 481 bars in block);
    z = M / (sigma * sqrt(16)); pos = (close - L) / (H - L).
    z >= thr and pos >= 0.75 -> MARKET BUY; z <= -thr and pos <= 0.25 -> MARKET SELL; spread <= 2 pips.
  thr = 0.8 (r025), 1.2 (r025b, pre-registered eval 2).
  Catastrophe SL 30 pips, no TP. Close at first decision >= 15:45 London or if the date changes.
  One trade per day.
See research/LEDGER.md R025.
"""

from __future__ import annotations

import datetime as _dt
import math
from zoneinfo import ZoneInfo

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc
_LON = ZoneInfo("Europe/London")


def _london(t: str) -> _dt.datetime:
    u = _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)
    return u.astimezone(_LON)


class R025LondonTrenddayFix(Strategy):
    name = "r025_london_trendday_fix"
    z_thr = 0.8

    def __init__(self, vol_bars: int = 480, pos_thr: float = 0.75, sl_pips: float = 30.0,
                 lots: float = 0.10, max_spread_points: float = 20.0, z_thr: float | None = None):
        if z_thr is not None:
            self.z_thr = float(z_thr)
        self.vol_bars, self.pos_thr, self.sl_pips = int(vol_bars), float(pos_thr), float(sl_pips)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._day = None
        self._o = None
        self._hi = None
        self._lo = None
        self._decided = False

    def parameters(self) -> dict:
        return {"z_thr": self.z_thr, "vol_bars": self.vol_bars, "pos_thr": self.pos_thr,
                "sl_pips": self.sl_pips, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _london(ctx.time) + _dt.timedelta(minutes=1)
        day = now.date()
        hm = now.hour * 60 + now.minute
        if day != self._day:
            self._day, self._o, self._hi, self._lo, self._decided = day, None, None, None, False
        closes = ctx.window.close
        close = float(closes[-1])

        if ctx.positions:
            for p in ctx.positions:
                if hm >= 15 * 60 + 45 or _london(p.entry_time).date() != day:
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            return intents

        if self._o is None and 8 * 60 <= hm < 8 * 60 + 15:
            self._o, self._hi, self._lo = close, close, close
            return intents
        if self._o is None:
            return intents
        if hm < 12 * 60:
            self._hi, self._lo = max(self._hi, close), min(self._lo, close)
            return intents
        if self._decided or hm >= 12 * 60 + 15:
            self._decided = True
            return intents
        # first decision in [12:00, 12:15)
        self._decided = True
        self._hi, self._lo = max(self._hi, close), min(self._lo, close)
        if len(closes) < self.vol_bars + 1 or self._hi <= self._lo:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        sigma = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1))
        if sigma <= 0:
            return intents
        z = (close - self._o) / (sigma * math.sqrt(16.0))
        pos = (close - self._lo) / (self._hi - self._lo)
        sl = self.sl_pips * 10 * ctx.point
        if z >= self.z_thr and pos >= self.pos_thr:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - sl, tag="L"))
        elif z <= -self.z_thr and pos <= 1.0 - self.pos_thr:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + sl, tag="S"))
        return intents


class R025bLondonTrenddayFix12(R025LondonTrenddayFix):
    name = "r025b_london_trendday_fix_12"
    z_thr = 1.2


R025LondonTrenddayFixM15 = make_timeframe(R025LondonTrenddayFix, 15)
R025bLondonTrenddayFix12M15 = make_timeframe(R025bLondonTrenddayFix12, 15)
