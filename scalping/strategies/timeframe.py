"""Run an M1 strategy on higher-timeframe bars (M5, M15, H1, ...).

The engine still replays every M1 bar and fills on ticks, so stops and
targets stay intrabar-exact. Only the *decision* moves up a timeframe:

* M1 bars are grouped into N-minute buckets aligned to the UTC clock
  (``epoch_minute // N``), so H1 buckets are clock hours.
* The wrapped strategy is called once per completed bucket, on the M1 bar
  that closes it (or, if that minute had no ticks, on the first later bar).
  It sees a window of completed N-minute bars only: no partial bar, no
  lookahead. Its orders become active on the next M1 bar, as usual.
* ``ctx.spread_close`` stays the live M1 spread, because that is the spread
  the order will actually pay.

Distance parameters given in points (``*_points`` except
``max_spread_points``, and ``trend_slope_max``) are scaled by ``sqrt(N)``,
the usual random-walk scaling of price ranges with time. The scale is a
fixed rule chosen before any block is scored, not a fit, so out-of-sample
scoring is preserved. Explicit constructor arguments are taken as given
and not scaled.
"""

from __future__ import annotations

import math

import numpy as np

from scalping.strategies.base import BarContext, BarWindow, Strategy

_NS_PER_MIN = 60_000_000_000
_UNSCALED = {"max_spread_points"}


def scaled_defaults(inner_cls: type[Strategy], minutes: int) -> dict:
    params = inner_cls().parameters()
    factor = math.sqrt(minutes)
    out = {}
    for key, value in params.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (
            (key.endswith("_points") and key not in _UNSCALED) or key == "trend_slope_max"
        ):
            out[key] = round(float(value) * factor, 2)
        else:
            out[key] = value
    return out


class _HtfBars:
    """Completed N-minute bars built once from the block's M1 arrays."""

    def __init__(self, market, minutes: int):
        ns = market.bar_time.view(np.int64)
        bucket = (ns // _NS_PER_MIN) // minutes
        self.ids, first = np.unique(bucket, return_index=True)
        last = np.append(first[1:], len(bucket)) - 1
        self.open = market.open[first].astype(np.float64)
        self.close = market.close[last].astype(np.float64)
        self.high = np.maximum.reduceat(market.high, first).astype(np.float64)
        self.low = np.minimum.reduceat(market.low, first).astype(np.float64)
        self.spread_close = market.spread_close[last].astype(np.float64)
        self.volume = np.add.reduceat(market.volume, first).astype(np.float64)


class Resampled(Strategy):
    """Base for generated ``<inner>@M<N>`` strategies. See module docstring."""

    inner_cls: type[Strategy]
    minutes: int

    def __init__(self, **params):
        merged = scaled_defaults(self.inner_cls, self.minutes)
        merged.update(params)
        self.inner = self.inner_cls(**merged)
        self._htf: _HtfBars | None = None
        self._market_id = None
        self._last_k = -1

    def parameters(self) -> dict:
        return {"timeframe_minutes": self.minutes, **self.inner.parameters()}

    def on_bar(self, ctx: BarContext) -> list:
        market = ctx.window._market
        if self._htf is None or self._market_id != id(market):
            self._htf = _HtfBars(market, self.minutes)
            self._market_id = id(market)
            self._last_k = -1
        htf = self._htf
        bar_ns = int(market.bar_time.view(np.int64)[ctx.bar_index])
        completed = (bar_ns + _NS_PER_MIN) // _NS_PER_MIN // self.minutes - 1
        k = int(np.searchsorted(htf.ids, completed, side="right")) - 1
        if k <= self._last_k:
            return []
        self._last_k = k
        inner_ctx = BarContext(
            symbol=ctx.symbol,
            point=ctx.point,
            bar_index=k,
            time=ctx.time,
            open=float(htf.open[k]),
            high=float(htf.high[k]),
            low=float(htf.low[k]),
            close=float(htf.close[k]),
            spread_close=ctx.spread_close,
            spread_mean=ctx.spread_mean,
            window=BarWindow(htf, k + 1),
            positions=ctx.positions,
            pending=ctx.pending,
            equity=ctx.equity,
        )
        return self.inner.on_bar(inner_ctx)


def make_timeframe(inner_cls: type[Strategy], minutes: int) -> type[Resampled]:
    label = f"H{minutes // 60}" if minutes % 60 == 0 else f"M{minutes}"
    return type(
        f"{inner_cls.__name__}_{label}",
        (Resampled,),
        {"name": f"{inner_cls.name}@{label}", "inner_cls": inner_cls, "minutes": minutes},
    )
