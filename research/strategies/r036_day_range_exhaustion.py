"""R036: Daily-range exhaustion reversal.

Dealers and intraday speculators end the day near flat; once the day's range has
exceeded its recent typical size and price prints a new day extreme, further
extension is transient.  Fade it.

Rules (M1 strategy, decisions at M15 boundaries, UTC, Mon-Fri):
  trading day = 22:00 UTC -> 22:00 UTC.
  typ = median range of the last 10 completed trading days with >= 600 M1 bars.
  decision 07:00-19:00 UTC, no trade yet today, flat:
    R = day high - day low so far; need R >= 1.1 * typ.
    SELL if day high set within 30 min and close >= high - 0.15 R;
    BUY  if day low  set within 30 min and close <= low  + 0.15 R (both -> skip).
  SL = extreme +/- 0.10 R beyond; TP = entry -/+ 0.25 R;
  close after 240 min or at 20:30 UTC.  One trade per trading day.
See research/LEDGER.md R036.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


def _trading_day(t: _dt.datetime) -> _dt.date:
    return (t + _dt.timedelta(hours=2)).date()


class R036DayRangeExhaustion(Strategy):
    name = "r036_day_range_exhaustion"

    def __init__(self, k: float = 1.1, typ_days: int = 10, min_day_bars: int = 600, near_frac: float = 0.15,
                 recent_min: int = 30, sl_frac: float = 0.10, tp_frac: float = 0.25, hold_min: int = 240,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.k, self.typ_days, self.min_day_bars = float(k), int(typ_days), int(min_day_bars)
        self.near_frac, self.recent_min = float(near_frac), int(recent_min)
        self.sl_frac, self.tp_frac, self.hold_min = float(sl_frac), float(tp_frac), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._reset()

    def _reset(self) -> None:
        self._last_t: _dt.datetime | None = None
        self._day: _dt.date | None = None
        self._hi = self._lo = None
        self._hi_t = self._lo_t = None
        self._nbars = 0
        self._ranges: list[float] = []
        self._traded_day: _dt.date | None = None

    def parameters(self) -> dict:
        return {"k": self.k, "typ_days": self.typ_days, "min_day_bars": self.min_day_bars,
                "near_frac": self.near_frac, "recent_min": self.recent_min, "sl_frac": self.sl_frac,
                "tp_frac": self.tp_frac, "hold_min": self.hold_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        t = _utc(ctx.time)
        if self._last_t is not None and (t <= self._last_t or t - self._last_t > _dt.timedelta(days=5)):
            self._reset()  # new block
        self._last_t = t
        now = t + _dt.timedelta(minutes=1)  # bar end
        day = _trading_day(t)
        if day != self._day:
            if self._day is not None and self._nbars >= self.min_day_bars:
                self._ranges.append(self._hi - self._lo)
                self._ranges = self._ranges[-self.typ_days:]
            self._day, self._hi, self._lo, self._nbars = day, ctx.high, ctx.low, 0
            self._hi_t = self._lo_t = now
        self._nbars += 1
        if ctx.high > self._hi:
            self._hi, self._hi_t = ctx.high, now
        if ctx.low < self._lo:
            self._lo, self._lo_t = ctx.low, now

        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        if ctx.positions:
            for p in ctx.positions:
                age = now - _utc(p.entry_time)
                if age >= _dt.timedelta(minutes=self.hold_min) or (now.hour * 60 + now.minute) >= 20 * 60 + 30:
                    intents.append(CloseIntent(p.id))
            return intents
        if now.minute % 15 != 0 or now.weekday() >= 5:
            return intents
        hm = now.hour * 60 + now.minute
        if not (7 * 60 <= hm <= 19 * 60):
            return intents
        if self._traded_day == day or len(self._ranges) < self.typ_days:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        typ = float(np.median(self._ranges))
        R = self._hi - self._lo
        if typ <= 0 or R < self.k * typ:
            return intents
        close = float(ctx.close)
        recent = _dt.timedelta(minutes=self.recent_min)
        sell = (now - self._hi_t) <= recent and close >= self._hi - self.near_frac * R
        buy = (now - self._lo_t) <= recent and close <= self._lo + self.near_frac * R
        if sell == buy:
            return intents
        if sell:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=self._hi + self.sl_frac * R,
                                       tp=close - self.tp_frac * R, tag="S"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=self._lo - self.sl_frac * R,
                                       tp=close + self.tp_frac * R, tag="L"))
        self._traded_day = day
        return intents
