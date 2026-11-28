"""R037: Stall-confirmed liquidity reversal.

R017's high-vol M15 band excursion, but enter only after a following bar closes
back inside the band (the push stalled: liquidity demand, not information).

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip new signals 20:45-23:15 UTC; spread <= 2 pips.
  signal: vol96 (sd of M15 close changes) >= 5.4 pips; close beyond mean20 -/+ 2 sd20.
  store m, s, extreme X.  Armed 2 bars: update X; enter at MARKET at the first armed
  bar closing back inside the band but not past the mean.
  SL = X -/+ 1 pip; TP = m; skip if TP distance < 2 pips; close after 120 min.
  One position at a time.
See research/LEDGER.md R037.
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


class R037StallConfirmedReversal(Strategy):
    name = "r037_stall_confirmed_reversal"

    def __init__(self, vol_min_pips: float = 5.4, vol_bars: int = 96, period: int = 20, num_std: float = 2.0,
                 armed_bars: int = 2, hold_min: int = 120, lots: float = 0.10, max_spread_points: float = 20.0):
        self.vol_min_pips, self.vol_bars = float(vol_min_pips), int(vol_bars)
        self.period, self.num_std = int(period), float(num_std)
        self.armed_bars, self.hold_min = int(armed_bars), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)
        self._setup = None  # (side, m, s, X, bars_left, last_k)

    def parameters(self) -> dict:
        return {"vol_min_pips": self.vol_min_pips, "vol_bars": self.vol_bars, "period": self.period,
                "num_std": self.num_std, "armed_bars": self.armed_bars, "hold_min": self.hold_min,
                "lots": self.lots, "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)
        k = ctx.bar_index
        if self._setup is not None and k != self._setup[5] + 1:
            self._setup = None  # bars skipped or new block
        if ctx.positions:
            self._setup = None
            for p in ctx.positions:
                if now - _utc(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            self._setup = None
            return intents
        closes = ctx.window.close
        close, pip = float(closes[-1]), 10.0 * ctx.point
        spread_ok = ctx.spread_close <= self.max_spread_points * ctx.point

        # 1) armed setup: check for the stall bar
        if self._setup is not None:
            side, m, s, X, left, _ = self._setup
            X = min(X, float(ctx.low)) if side == "L" else max(X, float(ctx.high))
            inside = (m - self.num_std * s < close < m) if side == "L" else (m < close < m + self.num_std * s)
            if inside and spread_ok and abs(m - close) >= 2 * pip:
                self._setup = None
                if side == "L":
                    intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=X - pip, tp=m, tag="L"))
                else:
                    intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=X + pip, tp=m, tag="S"))
                return intents
            left -= 1
            self._setup = (side, m, s, X, left, k) if left > 0 else None

        # 2) new signal on this bar (replaces an armed one)
        hm = now.hour * 60 + now.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            if self._setup is not None:
                self._setup = self._setup[:5] + (k,)
            return intents
        if len(closes) >= self.vol_bars + 1:
            vol = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1)) / pip
            c = closes[-self.period:]
            m, s = float(c.mean()), float(c.std())
            if vol >= self.vol_min_pips and s > 0:
                if close <= m - self.num_std * s:
                    self._setup = ("L", m, s, float(ctx.low), self.armed_bars, k)
                elif close >= m + self.num_std * s:
                    self._setup = ("S", m, s, float(ctx.high), self.armed_bars, k)
        if self._setup is not None:
            self._setup = self._setup[:5] + (k,)
        return intents


R037StallConfirmedReversalM15 = make_timeframe(R037StallConfirmedReversal, 15)
