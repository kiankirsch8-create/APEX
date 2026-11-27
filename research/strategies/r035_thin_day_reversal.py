"""R035: Thin-market-day liquidity-provision reversal.

Liquidity providers' risk-bearing capacity drops on bank holidays (London or New York
closed) and in the year-end window, so transient price pressure should be larger and
revert more. Trade R017's M15 band fade (no vol filter) only on those days.

Rules (M15 decisions on completed bars, stored clock, Mon-Fri):
  skip entries 20:45-23:15; spread <= 2 pips.
  thin day = decision date is a US market holiday, a UK bank holiday, or a weekday Dec 24 - Jan 2.
  mean/sd (population) of the last 20 closes; close <= mean - 2 sd -> MARKET BUY;
  close >= mean + 2 sd -> MARKET SELL. TP = mean; SL 1:1; close at market after 120 minutes.
  One position at a time.
  r035b (pre-registered mechanism control): identical rules on non-thin weekdays only.
See research/LEDGER.md R035.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import numpy as np
from dateutil.easter import easter

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = dt.timezone.utc


def _utc(t: str) -> dt.datetime:
    return dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


def _nth_wd(y: int, m: int, wd: int, n: int) -> dt.date:
    d = dt.date(y, m, 1)
    d += dt.timedelta((wd - d.weekday()) % 7)
    return d + dt.timedelta(7 * (n - 1))


def _last_wd(y: int, m: int, wd: int) -> dt.date:
    d = (dt.date(y, m + 1, 1) - dt.timedelta(1)) if m < 12 else dt.date(y, 12, 31)
    return d - dt.timedelta((d.weekday() - wd) % 7)


def _uk_observed(d: dt.date) -> dt.date:  # weekend -> next Monday
    return d + dt.timedelta(2) if d.weekday() == 5 else d + dt.timedelta(1) if d.weekday() == 6 else d


def _us_observed(d: dt.date) -> dt.date:  # Saturday -> Friday, Sunday -> Monday
    return d - dt.timedelta(1) if d.weekday() == 5 else d + dt.timedelta(1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def thin_days(y: int) -> frozenset:
    e = easter(y)
    s = set()
    # US market holidays
    s |= {_us_observed(dt.date(y, 1, 1)), _nth_wd(y, 1, 0, 3), _nth_wd(y, 2, 0, 3), e - dt.timedelta(2),
          _last_wd(y, 5, 0), _us_observed(dt.date(y, 7, 4)), _nth_wd(y, 9, 0, 1), _nth_wd(y, 11, 3, 4),
          _nth_wd(y, 11, 3, 4) + dt.timedelta(1), _us_observed(dt.date(y, 12, 25))}
    # UK bank holidays
    may = dt.date(2020, 5, 8) if y == 2020 else _nth_wd(y, 5, 0, 1)
    spring = _last_wd(y, 5, 0)
    if y == 2022:
        spring = dt.date(2022, 6, 2)
        s.add(dt.date(2022, 6, 3))
    s |= {_uk_observed(dt.date(y, 1, 1)), e - dt.timedelta(2), e + dt.timedelta(1), may, spring, _last_wd(y, 8, 0)}
    xmas, box = dt.date(y, 12, 25), dt.date(y, 12, 26)
    if xmas.weekday() >= 5 or box.weekday() >= 5:
        s |= {_uk_observed(xmas), _uk_observed(xmas) + dt.timedelta(1)}
    # year-end window
    s |= {dt.date(y, 12, k) for k in range(24, 32)}
    s |= {dt.date(y, 1, 1), dt.date(y, 1, 2)}
    return frozenset(d for d in s if d.weekday() < 5)


def is_thin(d: dt.date) -> bool:
    return d in thin_days(d.year)


class R035ThinDayReversal(Strategy):
    name = "r035_thin_day_reversal"
    want_thin = True

    def __init__(self, period: int = 20, num_std: float = 2.0, hold_min: int = 120, lots: float = 0.10,
                 max_spread_points: float = 20.0):
        self.period, self.num_std, self.hold_min = int(period), float(num_std), int(hold_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"period": self.period, "num_std": self.num_std, "hold_min": self.hold_min, "lots": self.lots,
                "max_spread_points": self.max_spread_points}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _utc(ctx.time) + dt.timedelta(minutes=1)  # decision time = close of the M1 bar ending the bucket
        if ctx.positions:
            for p in ctx.positions:
                if now - _utc(p.entry_time) >= dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            return intents
        if is_thin(now.date()) != self.want_thin:
            return intents
        hm = now.hour * 60 + now.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
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


class R035bNormalDayReversal(R035ThinDayReversal):
    name = "r035b_normal_day_reversal"
    want_thin = False


R035ThinDayReversalM15 = make_timeframe(R035ThinDayReversal, 15)
R035bNormalDayReversalM15 = make_timeframe(R035bNormalDayReversal, 15)
