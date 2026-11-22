"""R024: Unexpected-volatility liquidity-shock reversal.

The reversal premium should rise with liquidity *shocks* (unexpected vol), not with the
predictable intraday vol pattern. Normalise 2h realised vol by its usual level at the same
UTC M15 slot and trade the finding-1 / R017 band fade only when that ratio is high.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; live spread <= 2 pips.
  v8 = sd (ddof 1) of the last 8 M15 close-to-close changes;
  rv = v8 / median v8 of the same UTC M15 slot over the previous 20 observations (need >= 10).
  Need rv >= rv_min (1.40; r024b: 1.70).
  mean/sd of the last 20 closes; close <= mean - 2 sd -> MARKET BUY; close >= mean + 2 sd -> MARKET SELL.
  TP = mean; SL = same distance beyond entry (1:1); close at market after 120 minutes.
  One position at a time.
State is built only from completed bars of the current block and resets at each block.
See research/LEDGER.md R024.
"""

from __future__ import annotations

import datetime as _dt
from collections import deque

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc


def _utc(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


class R024VolShockReversal(Strategy):
    name = "r024_vol_shock_reversal"

    def __init__(self, rv_min: float = 1.40, vol_bars: int = 8, slot_days: int = 20, slot_min_obs: int = 10,
                 period: int = 20, num_std: float = 2.0, hold_min: int = 120, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.rv_min, self.vol_bars = float(rv_min), int(vol_bars)
        self.slot_days, self.slot_min_obs = int(slot_days), int(slot_min_obs)
        self.period, self.num_std = int(period), float(num_std)
        self.hold_min, self.lots, self.max_spread_points = int(hold_min), float(lots), float(max_spread_points)
        self._reset()

    def _reset(self) -> None:
        self._slot_hist: dict[int, deque] = {}
        self._rv = float("nan")
        self._seen = 0
        self._htf_id = None

    def parameters(self) -> dict:
        return {"rv_min": self.rv_min, "vol_bars": self.vol_bars, "slot_days": self.slot_days,
                "slot_min_obs": self.slot_min_obs, "period": self.period, "num_std": self.num_std,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def _update_state(self, ctx: BarContext) -> None:
        htf = ctx.window._market            # completed M15 bars of this block
        if self._htf_id != id(htf) or len(ctx.window) < self._seen:
            self._reset()
            self._htf_id = id(htf)
        n = len(ctx.window)
        closes = ctx.window.close
        ids = htf.ids
        for i in range(max(self._seen, self.vol_bars), n):
            v = float(np.std(np.diff(closes[i - self.vol_bars:i + 1]), ddof=1))
            slot = int(ids[i]) % 96          # bucket = epoch_minute // 15 -> slot of the UTC day
            hist = self._slot_hist.setdefault(slot, deque(maxlen=self.slot_days))
            if len(hist) >= self.slot_min_obs:
                med = float(np.median(hist))
                self._rv = v / med if med > 0 else float("nan")
            else:
                self._rv = float("nan")
            hist.append(v)
        self._seen = n

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        self._update_state(ctx)
        now = _utc(ctx.time) + _dt.timedelta(minutes=1)
        if ctx.positions:
            for p in ctx.positions:
                if now - _utc(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            return intents
        hm = now.hour * 60 + now.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        if not np.isfinite(self._rv) or self._rv < self.rv_min:
            return intents
        closes = ctx.window.close
        if len(closes) < self.period:
            return intents
        c = closes[-self.period:]
        mean, sd = float(c.mean()), float(c.std())
        if sd <= 0:
            return intents
        close = float(closes[-1])
        dist = abs(close - mean)
        if close <= mean - self.num_std * sd:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=mean, tag="L"))
        elif close >= mean + self.num_std * sd:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=mean, tag="S"))
        return intents


class R024bVolShockReversalTop10(R024VolShockReversal):
    name = "r024b_vol_shock_reversal_top10"

    def __init__(self, rv_min: float = 1.70, **kw):
        super().__init__(rv_min=rv_min, **kw)


R024VolShockReversalM15 = make_timeframe(R024VolShockReversal, 15)
R024bVolShockReversalTop10M15 = make_timeframe(R024bVolShockReversalTop10, 15)
