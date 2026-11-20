"""R020: Next-day European-session continuation of large prior day-session moves.

Delayed real-money execution / daily time-series momentum: European desks act on a large
prior-session information move the next London morning, in the same direction.

Rules (M15 decisions on completed bars, UTC):
  P0 = close at first decision in [07:00, 07:30); P1 = close at last decision in [20:30, 20:45].
  D = P1 - P0 for that weekday. sigma = sd of the last 480 M15 close changes at the P1 decision
  (needs 481 bars in the block); z = D / (sigma * sqrt(55)).
  Next weekday (Monday uses Friday's D), first decision in [07:00, 07:30) with spread <= 2 pips:
  z >= thr -> MARKET BUY, z <= -thr -> MARKET SELL. thr = 1.0 (r020), 1.5 (r020b, pre-registered eval 2).
  Catastrophe SL 40 pips, no TP. Close at first decision >= 16:00 UTC. One trade per day.
See research/LEDGER.md R020.
"""

from __future__ import annotations

import datetime as _dt
import math

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R020NextdayContinuation(Strategy):
    name = "r020_nextday_continuation"
    z_thr = 1.0

    def __init__(self, vol_bars: int = 480, sl_pips: float = 40.0, lots: float = 0.10,
                 max_spread_points: float = 20.0, z_thr: float | None = None):
        if z_thr is not None:
            self.z_thr = float(z_thr)
        self.vol_bars, self.sl_pips = int(vol_bars), float(sl_pips)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._day = None
        self._p0 = None
        self._p1 = None
        self._p1_sigma = None
        self._signal = None        # (day of D, sign) from the last completed weekday session
        self._traded: set = set()

    def parameters(self) -> dict:
        return {"z_thr": self.z_thr, "vol_bars": self.vol_bars, "sl_pips": self.sl_pips,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def _finish_day(self) -> None:
        """Turn the stored session into a signal (or none) when the next day starts."""
        if self._day is None or self._day.weekday() >= 5:
            return
        sig = None
        if self._p0 is not None and self._p1 is not None and self._p1_sigma:
            z = (self._p1 - self._p0) / (self._p1_sigma * math.sqrt(55.0))
            if z >= self.z_thr:
                sig = 1
            elif z <= -self.z_thr:
                sig = -1
        self._signal = (self._day, sig)

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)
        day = now.date()
        hm = now.hour * 60 + now.minute
        if day != self._day:
            self._finish_day()
            self._day, self._p0, self._p1, self._p1_sigma = day, None, None, None
        closes = ctx.window.close
        close = float(closes[-1])

        if ctx.positions:
            for p in ctx.positions:
                if hm >= 16 * 60 or _utc(p.entry_time).date() != day:
                    intents.append(CloseIntent(p.id))
        elif (now.weekday() < 5 and 7 * 60 <= hm < 7 * 60 + 30 and day not in self._traded
              and self._signal is not None and self._signal[1] is not None):
            sday, sgn = self._signal
            if (day - sday).days <= 3 and ctx.spread_close <= self.max_spread_points * ctx.point:
                self._traded.add(day)
                sl = self.sl_pips * 10 * ctx.point
                if sgn > 0:
                    intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - sl, tag="L"))
                else:
                    intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + sl, tag="S"))

        # record today's session after the entry check (P0 is taken at the same 07:00 decision)
        if 7 * 60 <= hm < 7 * 60 + 30 and self._p0 is None:
            self._p0 = close
        if 20 * 60 + 30 <= hm <= 20 * 60 + 45:
            self._p1 = close
            if len(closes) >= self.vol_bars + 1:
                self._p1_sigma = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1))
            else:
                self._p1_sigma = None
        return intents


class R020bNextdayContinuation15(R020NextdayContinuation):
    name = "r020b_nextday_continuation_15"
    z_thr = 1.5


R020NextdayContinuationM15 = make_timeframe(R020NextdayContinuation, 15)
R020bNextdayContinuation15M15 = make_timeframe(R020bNextdayContinuation15, 15)
