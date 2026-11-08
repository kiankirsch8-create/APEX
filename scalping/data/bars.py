"""Derive M1 bars from ticks. Spread on each bar is the real ask − bid."""

from __future__ import annotations

import pandas as pd


BAR_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "open_bid",
    "high_bid",
    "low_bid",
    "close_bid",
    "open_ask",
    "high_ask",
    "low_ask",
    "close_ask",
    "spread_open",
    "spread_high",
    "spread_low",
    "spread_close",
    "spread_mean",
    "volume",
    "tick_count",
)


def empty_bars() -> pd.DataFrame:
    dtype = {
        "timestamp": "datetime64[ns, UTC]",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "open_bid": "float64",
        "high_bid": "float64",
        "low_bid": "float64",
        "close_bid": "float64",
        "open_ask": "float64",
        "high_ask": "float64",
        "low_ask": "float64",
        "close_ask": "float64",
        "spread_open": "float64",
        "spread_high": "float64",
        "spread_low": "float64",
        "spread_close": "float64",
        "spread_mean": "float64",
        "volume": "float64",
        "tick_count": "int32",
    }
    return pd.DataFrame({name: pd.Series(dtype=kind) for name, kind in dtype.items()})


def ticks_to_m1(ticks: pd.DataFrame) -> pd.DataFrame:
    """One row per minute that contains at least one tick.

    OHLC is the mid price ``(bid + ask) / 2``. Bid and ask OHLC are kept beside
    it. ``spread_*`` is ``ask - bid`` at the open, the extreme, the close, and
    the mean of the minute. Strategy logic reads the mid OHLC; the engine fills
    on the underlying ticks, not on these bars.
    """
    if ticks is None or ticks.empty:
        return empty_bars()
    frame = ticks.loc[:, ["timestamp", "bid", "ask", "bid_volume", "ask_volume"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp")
    frame["mid"] = (frame["bid"] + frame["ask"]) / 2.0
    frame["spread"] = frame["ask"] - frame["bid"]
    frame["volume"] = frame["bid_volume"].astype("float64") + frame["ask_volume"].astype("float64")
    frame["minute"] = frame["timestamp"].dt.floor("min")
    grouped = frame.groupby("minute", sort=True)
    out = grouped.agg(
        open=("mid", "first"),
        high=("mid", "max"),
        low=("mid", "min"),
        close=("mid", "last"),
        open_bid=("bid", "first"),
        high_bid=("bid", "max"),
        low_bid=("bid", "min"),
        close_bid=("bid", "last"),
        open_ask=("ask", "first"),
        high_ask=("ask", "max"),
        low_ask=("ask", "min"),
        close_ask=("ask", "last"),
        spread_open=("spread", "first"),
        spread_high=("spread", "max"),
        spread_low=("spread", "min"),
        spread_close=("spread", "last"),
        spread_mean=("spread", "mean"),
        volume=("volume", "sum"),
        tick_count=("bid", "size"),
    )
    out = out.reset_index().rename(columns={"minute": "timestamp"})
    out["tick_count"] = out["tick_count"].astype("int32")
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out.loc[:, list(BAR_COLUMNS)].reset_index(drop=True)
