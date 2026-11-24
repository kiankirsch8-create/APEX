"""R029: Top-of-hour benchmark pre-positioning reversal.

Dealers pre-hedge benchmark-fix orders (WMR hourly, BFIX half-hourly) in the minutes before
the stamp; that pressure stops once the benchmark prints, so part of a large move into the
stamp should reverse.

Rules (M1, weekdays, stored clock; NY wall = stored - 5h exactly):
  ref  = close of the bar stamped hh:44 (hh:40..44 accepted if :44 is missing).
  At the close of the bar stamped hh:59 (stamp time T = hh+1:00):
    eligible if T's stored hour in 07..20 and NY wall time of T is not 10:00 / 14:00;
    move = close - ref; if |move| >= 8 pips and spread <= 1.5 pips:
    MARKET opposite sign(move), TP = 0.5*|move| toward ref, SL = 1.0*|move| beyond entry.
  Close at the first bar stamped >= T+19 min (i.e. at T+20). One trade per stamp.

r029_hourfix_reversal:        top-of-hour stamps only (eval 1, as registered)
r029b_hourfix_reversal_half:  also half-hour stamps (hh:14 -> hh:29, T = hh:30), NY 08:30
                              excluded (pre-registered eval 2)
See research/LEDGER.md R029.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


def _parse(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]))


class R029HourFixReversal(Strategy):
    name = "r029_hourfix_reversal"
    half_hours = False

    def __init__(self, threshold_pips: float = 8.0, tp_frac: float = 0.5, sl_frac: float = 1.0,
                 hold_min: int = 19, lots: float = 0.10, max_spread_points: float = 15.0):
        self.threshold_pips, self.tp_frac, self.sl_frac = float(threshold_pips), float(tp_frac), float(sl_frac)
        self.hold_min, self.lots, self.max_spread_points = int(hold_min), float(lots), float(max_spread_points)
        self._ref = {}            # (date, hour, half) -> ref close
        self._entry_T = None

    def parameters(self) -> dict:
        return {"threshold_pips": self.threshold_pips, "tp_frac": self.tp_frac, "sl_frac": self.sl_frac,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points,
                "half_hours": self.half_hours}

    def _excluded(self, T: _dt.datetime) -> bool:
        if not (7 <= T.hour <= 20):
            return True
        ny = T - _dt.timedelta(hours=5)
        if ny.weekday() > 4 or T.weekday() > 4:
            return True
        hm = (ny.hour, ny.minute)
        return hm in ((10, 0), (14, 0), (8, 30))

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _parse(ctx.time)
        pip = 10 * ctx.point
        m = now.minute

        if ctx.positions:
            if self._entry_T is not None and now >= self._entry_T + _dt.timedelta(minutes=self.hold_min):
                for p in ctx.positions:
                    intents.append(CloseIntent(p.id))
                self._entry_T = None
            return intents
        self._entry_T = None

        # reference bars: hh:40..44 (top of hour), hh:10..14 (half hour)
        key_day = (now.date(), now.hour)
        if 40 <= m <= 44:
            self._ref[key_day + (0,)] = ctx.close
        elif self.half_hours and 10 <= m <= 14:
            self._ref[key_day + (1,)] = ctx.close

        if m == 59:
            ref = self._ref.pop(key_day + (0,), None)
            T = now + _dt.timedelta(minutes=1)
        elif self.half_hours and m == 29:
            ref = self._ref.pop(key_day + (1,), None)
            T = now + _dt.timedelta(minutes=1)
        else:
            return intents
        if len(self._ref) > 64:
            self._ref.clear()
        if ref is None or self._excluded(T):
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        move = ctx.close - ref
        if abs(move) < self.threshold_pips * pip:
            return intents
        a = abs(move)
        if move > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots,
                                       sl=ctx.close + self.sl_frac * a, tp=ctx.close - self.tp_frac * a, tag="F"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots,
                                       sl=ctx.close - self.sl_frac * a, tp=ctx.close + self.tp_frac * a, tag="F"))
        self._entry_T = T
        return intents


class R029bHourFixReversalHalf(R029HourFixReversal):
    name = "r029b_hourfix_reversal_half"
    half_hours = True
