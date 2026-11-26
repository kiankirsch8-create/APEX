"""R033: Information-discreteness continuation ("frog in the pan", Da-Gurun-Warachka 2014).

Rules (M15 decisions on completed bars, stored clock, Mon-Fri):
  entries only at decisions in [07:00, 15:45]; spread <= 2 pips; one trade per day;
  one position at a time; need >= 113 bars.
  d = M15 close changes; sigma = sd (ddof 1) of the last 96 d.
  R = close_t - close_{t-16} (4h); z = R / (sigma * 4); require |z| >= 1.0.
  ID = sgn(R) * (frac(d<0) - frac(d>0)) over the 16 changes in the window;
  require ID <= max_id (-0.375; r033b: -0.5).
  MARKET order in sgn(R). No TP; catastrophe SL = 3 * sigma * 4 from entry.
  Close at market after 240 minutes or at the first decision >= 20:00, whichever first.
See research/LEDGER.md R033.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R033FrogInPan(Strategy):
    name = "r033_frog_in_pan"

    def __init__(self, max_id: float = -0.375, min_z: float = 1.0, window: int = 16, vol_n: int = 96,
                 sl_sd: float = 3.0, hold_min: int = 240, lots: float = 0.10, max_spread_points: float = 20.0):
        self.max_id, self.min_z, self.window, self.vol_n = float(max_id), float(min_z), int(window), int(vol_n)
        self.sl_sd, self.hold_min, self.lots = float(sl_sd), int(hold_min), float(lots)
        self.max_spread_points = float(max_spread_points)
        self._last_trade_date = None

    def parameters(self) -> dict:
        return {"max_id": self.max_id, "min_z": self.min_z, "window": self.window, "vol_n": self.vol_n,
                "sl_sd": self.sl_sd, "hold_min": self.hold_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)
        hm = now.hour * 60 + now.minute
        if ctx.positions:
            for p in ctx.positions:
                if now - _utc(p.entry_time) >= _dt.timedelta(minutes=self.hold_min) or hm >= 20 * 60:
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5 or not (7 * 60 <= hm <= 15 * 60 + 45):
            return intents
        if self._last_trade_date == now.date():
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        closes = ctx.window.close
        if len(closes) < self.vol_n + self.window + 1:
            return intents
        d_all = np.diff(closes[-(self.vol_n + 1):])
        sigma = float(np.std(d_all, ddof=1))
        if sigma <= 0:
            return intents
        R = float(closes[-1] - closes[-1 - self.window])
        z = R / (sigma * np.sqrt(self.window))
        if abs(z) < self.min_z:
            return intents
        w = np.diff(closes[-(self.window + 1):])
        s = np.sign(R)
        info_disc = s * (float(np.mean(w < 0)) - float(np.mean(w > 0)))
        if info_disc > self.max_id:
            return intents
        close = float(closes[-1])
        sl_dist = self.sl_sd * sigma * np.sqrt(self.window)
        self._last_trade_date = now.date()
        if s > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - sl_dist, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + sl_dist, tag="S"))
        return intents


class R033bFrogInPanStrict(R033FrogInPan):
    name = "r033b_frog_in_pan_strict"

    def __init__(self, max_id: float = -0.5, **kw):
        super().__init__(max_id=max_id, **kw)


R033FrogInPanM15 = make_timeframe(R033FrogInPan, 15)
R033bFrogInPanStrictM15 = make_timeframe(R033bFrogInPanStrict, 15)
