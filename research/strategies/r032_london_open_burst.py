"""R032: London-open opening-burst reversal.

At the European open (08:00 London) dealers and real-money desks execute the customer
orders that accumulated during the Asian night, at the same time as the market's liquidity
providers are still rebuilding their books (spreads wide, depth thin). The opening burst is
therefore a short spell of concentrated liquidity demand against thin supply; the part of the
first-15-minute move caused by that imbalance (not news) should be partly given back once full
European depth arrives over the next hour.

Rules (M1, weekdays, stored clock; stored = NY wall + 5h = London local time, see run reports):
  ref  = close of the last bar stamped 07:55..07:59 (price at the 08:00 open).
  At the close of the bar stamped 08:14 (time 08:15):
    move = close - ref; if |move| >= threshold and spread <= 1.5 pips:
    MARKET opposite sign(move), TP = 0.5*|move| toward ref, SL = 1.0*|move| beyond entry.
  Close at the first bar stamped >= 09:30. One trade per day.

r032_london_open_burst:     threshold 6 pips (eval 1, as registered; ~p60 of |move|)
r032b_london_open_burst_8: threshold 8 pips (pre-registered eval 2, dose-response; ~p80)
See research/LEDGER.md R032.
"""

from __future__ import annotations

import datetime as _dt

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy


def _parse(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]))


class R032LondonOpenBurst(Strategy):
    name = "r032_london_open_burst"

    def __init__(self, threshold_pips: float = 6.0, tp_frac: float = 0.5, sl_frac: float = 1.0,
                 exit_hhmm: int = 930, lots: float = 0.10, max_spread_points: float = 15.0):
        self.threshold_pips, self.tp_frac, self.sl_frac = float(threshold_pips), float(tp_frac), float(sl_frac)
        self.exit_hhmm, self.lots, self.max_spread_points = int(exit_hhmm), float(lots), float(max_spread_points)
        self._ref_day = None
        self._ref = None
        self._traded_day = None

    def parameters(self) -> dict:
        return {"threshold_pips": self.threshold_pips, "tp_frac": self.tp_frac, "sl_frac": self.sl_frac,
                "exit_hhmm": self.exit_hhmm, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _parse(ctx.time)
        hhmm = now.hour * 100 + now.minute
        pip = 10 * ctx.point

        if ctx.positions:
            if hhmm >= self.exit_hhmm or now.date() != self._traded_day:
                for p in ctx.positions:
                    intents.append(CloseIntent(p.id))
            return intents

        if now.weekday() > 4:
            return intents
        if 755 <= hhmm <= 759:
            self._ref_day, self._ref = now.date(), ctx.close
            return intents
        if hhmm != 814 or self._ref_day != now.date() or self._ref is None:
            return intents
        if self._traded_day == now.date():
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        move = ctx.close - self._ref
        a = abs(move)
        if a < self.threshold_pips * pip:
            return intents
        if move > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots,
                                       sl=ctx.close + self.sl_frac * a, tp=ctx.close - self.tp_frac * a, tag="F"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots,
                                       sl=ctx.close - self.sl_frac * a, tp=ctx.close + self.tp_frac * a, tag="F"))
        self._traded_day = now.date()
        return intents


class R032bLondonOpenBurst8(R032LondonOpenBurst):
    name = "r032b_london_open_burst_8"

    def __init__(self, threshold_pips: float = 8.0, **kw):
        super().__init__(threshold_pips=threshold_pips, **kw)
