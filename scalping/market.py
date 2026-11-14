"""In-memory bars and ticks the engine and the strategies share.

Bar ``i`` covers ticks with timestamps in ``[bar_time[i], bar_time[i] + 1 minute)``.
Strategy code sees bars only. The engine walks the ticks inside the bar.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class MarketData:
    tick_time: np.ndarray  # datetime64[ns], UTC values
    bid: np.ndarray
    ask: np.ndarray
    bar_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    spread_close: np.ndarray
    spread_mean: np.ndarray
    volume: np.ndarray
    tick_start: np.ndarray
    tick_end: np.ndarray
    # Per-bar features (always filled by build_market; None only for hand-built objects)
    tick_count: np.ndarray | None = None       # ticks in the minute (arrival rate)
    spread_high_raw: np.ndarray | None = None  # widest RAW spread in the minute (before any broker floor)
    upticks: np.ndarray | None = None          # ticks where the mid rose
    downticks: np.ndarray | None = None        # ticks where the mid fell
    london_minute: np.ndarray | None = None    # minute of day in Europe/London (DST-aware) at bar open
    ny_minute: np.ndarray | None = None        # minute of day in America/New_York (DST-aware) at bar open
    weekday: np.ndarray | None = None          # UTC weekday at bar open, Monday = 0
    news_next_min: np.ndarray | None = None    # minutes from bar close to the next calendar event (inf if none)
    news_last_min: np.ndarray | None = None    # minutes from the last calendar event to bar close (inf if none)

    def __len__(self) -> int:
        return int(self.bar_time.shape[0])

    @property
    def tick_time_ns(self) -> np.ndarray:
        return self.tick_time.view(np.int64)


def _as_ns(series: pd.Series) -> np.ndarray:
    dt = pd.to_datetime(series, utc=True)
    return dt.to_numpy(dtype="datetime64[ns]")


def build_market(ticks: pd.DataFrame, bars: pd.DataFrame) -> MarketData:
    """Align M1 bars with the tick slice that produced them.

    Bars with no ticks are dropped. Both frames must carry a ``timestamp`` column.
    """
    if ticks.empty or bars.empty:
        raise ValueError("ticks and bars are both required")
    ticks = ticks.sort_values("timestamp").reset_index(drop=True)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    tick_time = _as_ns(ticks["timestamp"])
    bar_time = _as_ns(bars["timestamp"])
    if np.any(tick_time[1:] < tick_time[:-1]):
        raise ValueError("tick timestamps are not sorted")
    start = np.searchsorted(tick_time, bar_time, side="left")
    end = np.searchsorted(tick_time, bar_time + np.timedelta64(1, "m"), side="left")
    keep = end > start
    if not np.any(keep):
        raise ValueError("no bar contains a tick")
    if "volume" in bars.columns:
        volume = bars["volume"].to_numpy(dtype=np.float64)
    else:
        volume = np.ones(len(bars), dtype=np.float64)
    return MarketData(
        tick_time=np.ascontiguousarray(tick_time),
        bid=np.ascontiguousarray(ticks["bid"].to_numpy(dtype=np.float64)),
        ask=np.ascontiguousarray(ticks["ask"].to_numpy(dtype=np.float64)),
        bar_time=np.ascontiguousarray(bar_time[keep]),
        open=np.ascontiguousarray(bars["open"].to_numpy(dtype=np.float64)[keep]),
        high=np.ascontiguousarray(bars["high"].to_numpy(dtype=np.float64)[keep]),
        low=np.ascontiguousarray(bars["low"].to_numpy(dtype=np.float64)[keep]),
        close=np.ascontiguousarray(bars["close"].to_numpy(dtype=np.float64)[keep]),
        spread_close=np.ascontiguousarray(bars["spread_close"].to_numpy(dtype=np.float64)[keep]),
        spread_mean=np.ascontiguousarray(bars["spread_mean"].to_numpy(dtype=np.float64)[keep]),
        volume=np.ascontiguousarray(volume[keep]),
        tick_start=np.ascontiguousarray(start[keep].astype(np.int64)),
        tick_end=np.ascontiguousarray(end[keep].astype(np.int64)),
        **bar_features(ticks, bars, start[keep], end[keep], bar_time[keep]),
    )


def bar_features(ticks: pd.DataFrame, bars: pd.DataFrame, start: np.ndarray, end: np.ndarray,
                 bar_time: np.ndarray) -> dict:
    """Tick-derived and clock features per kept bar. See MarketData field comments."""
    from scalping.data.calendar import news_distances

    bid = ticks["bid"].to_numpy(dtype=np.float64)
    ask = ticks["ask"].to_numpy(dtype=np.float64)
    mid = (bid + ask) / 2.0
    step = np.sign(np.diff(mid, prepend=mid[:1]))
    up_cs = np.concatenate([[0], np.cumsum(step > 0)])
    dn_cs = np.concatenate([[0], np.cumsum(step < 0)])
    spread = ask - bid
    spread_high = np.maximum.reduceat(spread, start) if len(start) else np.zeros(0)
    # reduceat runs to the next start; ticks between bars do not exist, so this equals the bar slice
    stamps = pd.DatetimeIndex(bar_time.astype("datetime64[ns]")).tz_localize("UTC")
    london = stamps.tz_convert("Europe/London")
    ny = stamps.tz_convert("America/New_York")
    close_ns = bar_time.astype("datetime64[ns]").astype(np.int64) + 60_000_000_000
    nxt, last = news_distances(close_ns)
    return {
        "tick_count": np.ascontiguousarray((end - start).astype(np.float64)),
        "spread_high_raw": np.ascontiguousarray(spread_high.astype(np.float64)),
        "upticks": np.ascontiguousarray((up_cs[end] - up_cs[start]).astype(np.float64)),
        "downticks": np.ascontiguousarray((dn_cs[end] - dn_cs[start]).astype(np.float64)),
        "london_minute": np.ascontiguousarray((london.hour * 60 + london.minute).to_numpy(dtype=np.float64)),
        "ny_minute": np.ascontiguousarray((ny.hour * 60 + ny.minute).to_numpy(dtype=np.float64)),
        "weekday": np.ascontiguousarray(stamps.weekday.to_numpy(dtype=np.float64)),
        "news_next_min": np.ascontiguousarray(nxt),
        "news_last_min": np.ascontiguousarray(last),
    }


def with_min_spread(market: MarketData, floor: float) -> MarketData:
    """Widen every tick tighter than ``floor`` (price units) symmetrically around its mid.

    Bar OHLC are mids and stay unchanged; bar spreads are floored the same way.
    Used to model a spread-only broker on top of a tighter raw feed.
    """
    from dataclasses import replace

    if floor <= 0:
        return market
    spread = market.ask - market.bid
    widen = np.maximum(floor - spread, 0.0) / 2.0
    return replace(
        market,
        bid=np.ascontiguousarray(market.bid - widen),
        ask=np.ascontiguousarray(market.ask + widen),
        spread_close=np.ascontiguousarray(np.maximum(market.spread_close, floor)),
        spread_mean=np.ascontiguousarray(np.maximum(market.spread_mean, floor)),
    )
