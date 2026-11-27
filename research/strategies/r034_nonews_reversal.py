"""R034: No-news liquidity-provision reversal.

R017's high-vol M15 band fade, split by whether a scheduled macro release preceded the signal.
Mechanism (Chan 2003; Evans-Lyons): no-news moves are liquidity-driven and revert; release-driven
moves are information and drift. See research/LEDGER.md R034.

Rules = R017 (vol96 >= 5.4 pips, 20-bar 2-sd band, MARKET, TP mean, SL 1:1, 120-min time stop,
skip 20:45-23:15 stored, spread <= 2 pips), plus a release filter:
  release times (true UTC, stored-5h = NY local, as R028): NY 08:30, 10:00, 14:00; Berlin 14:30.
  "after_release" = some release T with decision-60min < T <= decision.
  r034_nonews_reversal:        skip signals that are after_release (eval 1).
  r034b_news_only_reversal:    take ONLY after_release signals (eval 2, mechanism check).
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import numpy as np

from scalping.costs import OrderType, Side
from scalping.strategies.base import BarContext, CancelIntent, CloseIntent, OrderIntent, Strategy
from scalping.strategies.timeframe import make_timeframe

_UTC = _dt.timezone.utc
_NY = ZoneInfo("America/New_York")
_RELEASES = (
    (_NY, (8 * 60 + 30, 10 * 60, 14 * 60)),
    (ZoneInfo("Europe/Berlin"), (14 * 60 + 30,)),
)


def _stored(t: str) -> _dt.datetime:
    return _dt.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]), tzinfo=_UTC)


def _true_utc(s: _dt.datetime) -> _dt.datetime:
    """Stored stamp -> true UTC. Stored = NY local wall clock + 5h."""
    ny_wall = (s - _dt.timedelta(hours=5)).replace(tzinfo=None)
    return ny_wall.replace(tzinfo=_NY).astimezone(_UTC)


def after_release(decision_stored: _dt.datetime, window_min: int = 60) -> bool:
    """True if a release time T lies in (decision - window, decision]."""
    ts = _true_utc(decision_stored)
    for tz, times in _RELEASES:
        loc = ts.astimezone(tz)
        if loc.weekday() >= 5:
            continue
        hm = loc.hour * 60 + loc.minute
        for T in times:
            if 0 <= hm - T < window_min:
                return True
    return False


class R034NoNewsReversal(Strategy):
    name = "r034_nonews_reversal"
    news_mode = "exclude"  # "exclude" -> skip after-release signals; "only" -> take only them

    def __init__(self, vol_min_pips: float = 5.4, vol_bars: int = 96, period: int = 20, num_std: float = 2.0,
                 hold_min: int = 120, window_min: int = 60, lots: float = 0.10, max_spread_points: float = 20.0,
                 news_mode: str | None = None, clock: str = ""):
        if news_mode is not None:
            self.news_mode = str(news_mode)
        self.vol_min_pips, self.vol_bars = float(vol_min_pips), int(vol_bars)
        self.period, self.num_std, self.hold_min = int(period), float(num_std), int(hold_min)
        self.window_min = int(window_min)
        self.lots, self.max_spread_points = float(lots), float(max_spread_points)

    def parameters(self) -> dict:
        return {"vol_min_pips": self.vol_min_pips, "vol_bars": self.vol_bars, "period": self.period,
                "num_std": self.num_std, "hold_min": self.hold_min, "window_min": self.window_min,
                "news_mode": self.news_mode, "lots": self.lots, "max_spread_points": self.max_spread_points,
                "clock": "stored-5h=NY local"}

    def on_bar(self, ctx: BarContext) -> list:
        intents: list = [CancelIntent(o.id) for o in ctx.pending]
        now = _stored(ctx.time) + _dt.timedelta(minutes=1)  # decision time = close of the M1 bar ending the bucket
        if ctx.positions:
            for p in ctx.positions:
                if now - _stored(p.entry_time) >= _dt.timedelta(minutes=self.hold_min):
                    intents.append(CloseIntent(p.id))
            return intents
        if now.weekday() >= 5:
            return intents
        hm = now.hour * 60 + now.minute
        if 20 * 60 + 45 <= hm < 23 * 60 + 15:
            return intents
        if ctx.spread_close > self.max_spread_points * ctx.point:
            return intents
        closes = ctx.window.close
        if len(closes) < self.vol_bars + 1:
            return intents
        pip = 10.0 * ctx.point
        vol = float(np.std(np.diff(closes[-(self.vol_bars + 1):]), ddof=1)) / pip
        if vol < self.vol_min_pips:
            return intents
        c = closes[-self.period:]
        mean, sd = float(c.mean()), float(c.std())
        if sd <= 0:
            return intents
        close = float(closes[-1])
        dist = abs(close - mean)
        side = None
        if close <= mean - self.num_std * sd:
            side = Side.BUY
        elif close >= mean + self.num_std * sd:
            side = Side.SELL
        if side is None:
            return intents
        news = after_release(now, self.window_min)
        if (self.news_mode == "exclude" and news) or (self.news_mode == "only" and not news):
            return intents
        if side is Side.BUY:
            intents.append(OrderIntent(OrderType.MARKET, Side.BUY, self.lots, sl=close - dist, tp=mean, tag="L"))
        else:
            intents.append(OrderIntent(OrderType.MARKET, Side.SELL, self.lots, sl=close + dist, tp=mean, tag="S"))
        return intents


class R034bNewsOnlyReversal(R034NoNewsReversal):
    name = "r034b_news_only_reversal"
    news_mode = "only"


R034NoNewsReversalM15 = make_timeframe(R034NoNewsReversal, 15)
R034bNewsOnlyReversalM15 = make_timeframe(R034bNewsOnlyReversal, 15)
