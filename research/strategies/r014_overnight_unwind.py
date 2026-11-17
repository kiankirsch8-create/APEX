"""R014: Overnight Asia unwind of the European/US day-session move.

Inventory price pressure (Hendershott & Menkveld 2014; Evans & Lyons 2002): part of a large
day-session move is liquidity demand absorbed by dealers, laid off in thin Asian hours.

Rules (UTC, day sessions Mon-Thu):
  D = close of last bar in [20:30, 21:00) - open of first bar in [07:00, 07:30) same day.
  If |D| >= min_move pips: at first bar in [22:15, 23:00) that evening with spread <= 2 pips,
  MARKET order opposite sign(D). Catastrophe SL 30 pips, no TP. Close at first bar >= 06:00 UTC.
  One trade per night. Entry after the 22:00 funding roll, exit before the next one.

r014b_overnight_unwind_30: same rules with min_move = 30 pips (pre-registered eval 2).
See research/LEDGER.md R014.
"""

from __future__ import annotations

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
import datetime as _dt


class R014OvernightUnwind(Strategy):
    name = "r014_overnight_unwind"
    min_move_pips = 20.0

    def __init__(self, sl_pips: float = 30.0, lots: float = 0.10, max_spread_points: float = 20.0):
        self.sl_pips, self.lots, self.max_spread_points = float(sl_pips), float(lots), float(max_spread_points)
        self._day = None
        self._open = None      # open of first bar in [07:00, 07:30)
        self._close = None     # close of last bar in [20:30, 21:00)
        self._signal = None    # (day, sign of D) waiting for the 22:15 entry
        self._done: set = set()

    def parameters(self) -> dict:
        return {"min_move_pips": self.min_move_pips, "sl_pips": self.sl_pips, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        day = _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))
        hm = int(t[11:13]) * 60 + int(t[14:16])
        if day != self._day:
            self._day, self._open, self._close = day, None, None
        # ctx.time is the stamp of the bar that just completed; ctx.open/close are that bar.
        if 7 * 60 <= hm < 7 * 60 + 30 and self._open is None:
            self._open = ctx.open
        if 20 * 60 + 30 <= hm < 21 * 60:
            self._close = ctx.close
        if hm >= 21 * 60 and (day, "sig") not in self._done:
            self._done.add((day, "sig"))
            if day.weekday() <= 3 and self._open is not None and self._close is not None:
                d_pips = (self._close - self._open) / (10 * ctx.point)
                if abs(d_pips) >= self.min_move_pips:
                    self._signal = (day, 1 if d_pips > 0 else -1)

        if ctx.positions:
            for p in ctx.positions:
                if 6 * 60 <= hm < 22 * 60:
                    intents.append(CloseIntent(p.id))
            return intents

        if self._signal is not None:
            sday, sgn = self._signal
            if sday != day or hm >= 23 * 60:
                self._signal = None
            elif 22 * 60 + 15 <= hm < 23 * 60 and ctx.spread_close <= self.max_spread_points * ctx.point:
                self._signal = None
                sl = self.sl_pips * 10 * ctx.point
                if sgn > 0:
                    intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=ctx.close + sl, tag="F"))
                else:
                    intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=ctx.close - sl, tag="F"))
        return intents


class R014bOvernightUnwind30(R014OvernightUnwind):
    name = "r014b_overnight_unwind_30"
    min_move_pips = 30.0
