"""R016: Prior-day high/low stop cascade in London/NY hours.

Stop-loss orders cluster just beyond salient levels and execute as market orders when crossed,
so price accelerates through them (Osler 2003, 2005). The prior trading day's high and low
(17:00 NY daily candle) are the most watched stop levels.

Rules (UTC bars, weekdays):
  Trading day = America/New_York 17:00 -> 17:00. PDH/PDL = high/low of the previous trading day
  (needs >= 600 M1 bars). A level is live until touched in the current trading day.
  07:00 <= UTC < 16:00, live level within 15 pips of last close, spread <= 2 pips:
    BUY STOP at PDH + 2 pips, SL PDH - 6, TP PDH + 14
    SELL STOP at PDL - 2 pips, SL PDL + 6, TP PDL - 14
  Orders refreshed each bar. Touched level (price or fill) is dead for the day.
  Close after 60 minutes or at 16:30 UTC. One position at a time, one trade per level per day.
See research/LEDGER.md R016.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R016PDHLStopCascade(Strategy):
    name = "r016_pdhl_stop_cascade"

    def __init__(self, start_min: int = 7 * 60, end_min: int = 16 * 60, flat_min: int = 16 * 60 + 30,
                 offset_pips: float = 2.0, sl_back_pips: float = 6.0, tp_pips: float = 14.0,
                 near_pips: float = 15.0, hold_min: int = 60, min_bars: int = 600,
                 lots: float = 0.10, max_spread_points: float = 20.0):
        self.start_min, self.end_min, self.flat_min = int(start_min), int(end_min), int(flat_min)
        self.offset_pips, self.sl_back_pips, self.tp_pips = float(offset_pips), float(sl_back_pips), float(tp_pips)
        self.near_pips, self.hold_min, self.min_bars = float(near_pips), int(hold_min), int(min_bars)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._day = None
        self._hi = self._lo = None      # current trading day so far
        self._n = 0
        self._pdh = self._pdl = None    # previous trading day
        self._dead: set = set()         # {"H", "L"} dead for the day

    def parameters(self) -> dict:
        return {"start_min": self.start_min, "end_min": self.end_min, "flat_min": self.flat_min,
                "offset_pips": self.offset_pips, "sl_back_pips": self.sl_back_pips, "tp_pips": self.tp_pips,
                "near_pips": self.near_pips, "hold_min": self.hold_min, "min_bars": self.min_bars,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time)
        tday = (now.astimezone(_NY) + _dt.timedelta(hours=7)).date()
        if tday != self._day:
            # A stub "day" (e.g. the hour of Friday bars after 17:00 NY) is not a trading day:
            # keep the last full day's levels instead of resetting them.
            if self._day is not None and self._n >= self.min_bars:
                self._pdh, self._pdl = self._hi, self._lo
            self._day, self._hi, self._lo, self._n = tday, None, None, 0
            self._dead = set()
        # ctx.open/high/low/close are the bar that just completed (stamped ctx.time).
        self._hi = ctx.high if self._hi is None else max(self._hi, ctx.high)
        self._lo = ctx.low if self._lo is None else min(self._lo, ctx.low)
        self._n += 1
        if self._pdh is not None:
            if self._hi >= self._pdh:
                self._dead.add("H")
            if self._lo <= self._pdl:
                self._dead.add("L")

        hm = now.hour * 60 + now.minute
        if ctx.positions:
            for p in ctx.positions:
                if p.tag in ("H", "L"):
                    self._dead.add(p.tag)
                held = (now - _utc(p.entry_time)).total_seconds() / 60.0
                if held >= self.hold_min or hm >= self.flat_min:
                    intents.append(CloseIntent(p.id))
            return intents

        if self._pdh is None or tday.weekday() >= 5:
            return intents
        if not (self.start_min <= hm < self.end_min):
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        pip = 10 * ctx.point
        near = self.near_pips * pip
        if "H" not in self._dead and self._pdh - ctx.close <= near:
            px = self._pdh + self.offset_pips * pip
            intents.append(OrderIntent(OrderType.STOP, Side.BUY, self.lots, price=px,
                                       sl=self._pdh - self.sl_back_pips * pip,
                                       tp=self._pdh + self.tp_pips * pip, tag="H"))
        if "L" not in self._dead and ctx.close - self._pdl <= near:
            px = self._pdl - self.offset_pips * pip
            intents.append(OrderIntent(OrderType.STOP, Side.SELL, self.lots, price=px,
                                       sl=self._pdl + self.sl_back_pips * pip,
                                       tp=self._pdl - self.tp_pips * pip, tag="L"))
        return intents
