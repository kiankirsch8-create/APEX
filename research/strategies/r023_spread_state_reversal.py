"""R023: Spread-state liquidity-provision reversal.

Short-term reversal is the price of immediacy and should be highest when dealer
capacity is scarce. A quoted spread that is wide *for its time of day* measures
that directly. Trade the finding-1 / R017 band fade only in that state.

Rules (M15 decisions on completed bars, UTC, Mon-Fri):
  skip entries 20:45-23:15 UTC; live spread <= 2 pips.
  Spread state: ratio of each completed M15 bar's spread_close to the median spread_close of the
  same UTC M15 slot over the previous 20 observations of that slot (need >= 10);
  r4 = mean of the last 4 ratios. Need r4 >= ratio_min (1.15; r023b: 1.30).
  mean/sd of the last 20 closes; close <= mean - 2 sd -> MARKET BUY; close >= mean + 2 sd -> MARKET SELL.
  TP = mean; SL = same distance beyond entry (1:1); close at market after 120 minutes.
  One position at a time.
State is built only from completed bars of the current block and resets at each block.
See research/LEDGER.md R023.
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


class R023SpreadStateReversal(Strategy):
    name = "r023_spread_state_reversal"

    def __init__(self, ratio_min: float = 1.15, slot_days: int = 20, slot_min_obs: int = 10, state_bars: int = 4,
                 period: int = 20, num_std: float = 2.0, hold_min: int = 120, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.ratio_min, self.slot_days, self.slot_min_obs = float(ratio_min), int(slot_days), int(slot_min_obs)
        self.state_bars, self.period, self.num_std = int(state_bars), int(period), float(num_std)
        self.hold_min, self.lots, self.max_spread_points = int(hold_min), float(lots), float(max_spread_points)
        self._reset()

    def _reset(self) -> None:
        self._slot_hist: dict[int, deque] = {}
        self._ratios: deque = deque(maxlen=self.state_bars)
        self._seen = 0          # number of completed bars already folded into the state
        self._htf_id = None

    def parameters(self) -> dict:
        return {"ratio_min": self.ratio_min, "slot_days": self.slot_days, "slot_min_obs": self.slot_min_obs,
                "state_bars": self.state_bars, "period": self.period, "num_std": self.num_std,
                "hold_min": self.hold_min, "lots": self.lots, "max_spread_points": self.max_spread_points}

    def _update_state(self, ctx: BarContext) -> None:
        htf = ctx.window._market            # completed M15 bars of this block
        if self._htf_id != id(htf) or len(ctx.window) < self._seen:
            self._reset()
            self._htf_id = id(htf)
        n = len(ctx.window)
        ids = htf.ids
        spreads = htf.spread_close
        for i in range(self._seen, n):
            slot = int(ids[i]) % 96          # bucket = epoch_minute // 15 -> slot of the UTC day
            sp = float(spreads[i])
            hist = self._slot_hist.setdefault(slot, deque(maxlen=self.slot_days))
            if len(hist) >= self.slot_min_obs:
                med = float(np.median(hist))
                self._ratios.append(sp / med if med > 0 else np.nan)
            else:
                self._ratios.append(np.nan)
            hist.append(sp)
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
        if len(self._ratios) < self.state_bars:
            return intents
        r4 = float(np.mean(self._ratios))
        if not np.isfinite(r4) or r4 < self.ratio_min:
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


class R023bSpreadStateReversalTop10(R023SpreadStateReversal):
    name = "r023b_spread_state_reversal_top10"

    def __init__(self, ratio_min: float = 1.30, **kw):
        super().__init__(ratio_min=ratio_min, **kw)


R023SpreadStateReversalM15 = make_timeframe(R023SpreadStateReversal, 15)
R023bSpreadStateReversalTop10M15 = make_timeframe(R023bSpreadStateReversalTop10, 15)
