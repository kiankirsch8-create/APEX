"""R030: Daily-horizon time-series momentum (hold the N-day trend direction for one day).

Mechanism: FX time-series momentum (Moskowitz-Ooi-Pedersen 2012; Menkhoff et al. 2012):
slow information diffusion plus mechanical trend-follower flow keep pushing price in the
direction of the recent multi-day move.

Rules (M1, stored clock, block-local data only):
  s_d = close of the first bar with stored time >= 12:00 on weekday d.
  Mon-Thu at that bar: if >= N earlier samples exist, r = s_d - s_{d-N};
  MARKET order in sign(r) (skip r == 0, skip spread > 2 pips).
  Catastrophe SL 150 pips, no TP. Close at the next weekday's 12:00 sample bar
  (24h hold), then take that day's entry decision on the same bar. No Friday entries.

r030_daily_tsmom:      N = 10 (eval 1, as registered)
r030b_daily_tsmom_20:  N = 20 (pre-registered eval 2)
See research/LEDGER.md R030.
"""

from __future__ import annotations

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy

import datetime as _dt


class R030DailyTsmom(Strategy):
    name = "r030_daily_tsmom"
    lookback = 10

    def __init__(self, sl_pips: float = 150.0, sample_hour: int = 12, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.sl_pips, self.sample_hour = float(sl_pips), int(sample_hour)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._samples: list[float] = []
        self._last_date = None

    def parameters(self) -> dict:
        return {"lookback": self.lookback, "sl_pips": self.sl_pips, "sample_hour": self.sample_hour,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        t = ctx.time
        d = _dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))
        hour = int(t[11:13])
        if d.weekday() > 4 or hour < self.sample_hour or d == self._last_date:
            return intents
        # first bar at/after the sample hour on a new weekday
        self._last_date = d
        for p in ctx.positions:
            intents.append(CloseIntent(p.id))
        n = self.lookback
        past = self._samples[-n] if len(self._samples) >= n else None
        self._samples.append(float(ctx.close))
        if past is None or d.weekday() > 3:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        r = ctx.close - past
        if r == 0:
            return intents
        sl = self.sl_pips * 10 * ctx.point
        if r > 0:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=ctx.close - sl, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=ctx.close + sl, tag="S"))
        return intents


class R030bDailyTsmom20(R030DailyTsmom):
    name = "r030b_daily_tsmom_20"
    lookback = 20
