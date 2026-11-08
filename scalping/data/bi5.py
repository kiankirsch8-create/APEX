"""Dukascopy ``.bi5`` tick files.

One file is one instrument-hour. The month in the URL is zero-based. The body
is LZMA-compressed. Each record is 20 bytes, big-endian:

    uint32 milliseconds from the start of the hour
    uint32 ask  (integer; divide by the instrument price scale)
    uint32 bid
    float32 ask volume
    float32 bid volume

This is the feed ``duka`` and ``dukascopy-node`` download. Empty hours come
back as an empty body and are cached as empty files so they are not requested
again.
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from scalping.instruments import get_instrument

BI5_HOSTS: tuple[str, ...] = (
    "https://datafeed.dukascopy.com/datafeed",
    "https://www.dukascopy.com/datafeed",
)

_RECORD = struct.Struct(">IIIff")
_DTYPE = np.dtype(
    [
        ("ms", ">u4"),
        ("ask_raw", ">u4"),
        ("bid_raw", ">u4"),
        ("ask_vol", ">f4"),
        ("bid_vol", ">f4"),
    ]
)

TICK_COLUMNS = ("timestamp", "bid", "ask", "bid_volume", "ask_volume")


def bi5_url(symbol: str, hour: datetime, host: str = BI5_HOSTS[0]) -> str:
    """Public URL for one hour. ``hour`` is UTC; the URL month is ``month - 1``."""
    hour = _as_utc(hour)
    return (
        f"{host.rstrip('/')}/{symbol.upper()}/"
        f"{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def cache_relpath(symbol: str, hour: datetime) -> str:
    """Cache path uses the calendar month so the directory is readable."""
    hour = _as_utc(hour)
    return (
        f"bi5/{symbol.upper()}/{hour.year}/{hour.month:02d}/"
        f"{hour.day:02d}/{hour.hour:02d}.bi5"
    )


def encode_bi5(
    records: list[tuple[int, float, float, float, float]],
    price_scale: int,
) -> bytes:
    """Compress records ``(ms, bid, ask, bid_volume, ask_volume)`` to ``.bi5`` bytes."""
    buf = bytearray()
    scale = int(price_scale)
    for ms, bid, ask, bid_vol, ask_vol in records:
        buf += _RECORD.pack(
            int(ms),
            int(round(float(ask) * scale)),
            int(round(float(bid) * scale)),
            float(ask_vol),
            float(bid_vol),
        )
    return lzma.compress(bytes(buf), format=lzma.FORMAT_ALONE)


def decode_bi5(payload: bytes, hour: datetime, symbol: str) -> pd.DataFrame:
    """Decode one hour into a tick frame. An empty payload is an empty frame."""
    hour = _as_utc(hour)
    empty = _empty_ticks()
    if not payload:
        return empty
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError:
        raw = payload
    if not raw:
        return empty
    if len(raw) % _RECORD.size != 0:
        raise ValueError(
            f"{symbol} {hour.isoformat()} bi5 length {len(raw)} is not a multiple of 20"
        )
    rec = np.frombuffer(raw, dtype=_DTYPE)
    ms = rec["ms"].astype(np.int64)
    scale = float(get_instrument(symbol).price_scale)
    bid = rec["bid_raw"].astype(np.float64) / scale
    ask = rec["ask_raw"].astype(np.float64) / scale
    bid_vol = rec["bid_vol"].astype(np.float64)
    ask_vol = rec["ask_vol"].astype(np.float64)
    ok = (
        (ms >= 0)
        & (ms < 3_600_000)
        & (bid > 0.0)
        & (ask > 0.0)
        & (ask >= bid)
    )
    if not np.any(ok):
        return empty
    ms = ms[ok]
    base = np.datetime64(_naive_utc(hour), "ns")
    timestamps = base + ms.astype("timedelta64[ms]")
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "bid": bid[ok],
            "ask": ask[ok],
            "bid_volume": bid_vol[ok].astype(np.float32),
            "ask_volume": ask_vol[ok].astype(np.float32),
        }
    )
    return frame.sort_values("timestamp").reset_index(drop=True)


def _empty_ticks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "bid": pd.Series(dtype="float64"),
            "ask": pd.Series(dtype="float64"),
            "bid_volume": pd.Series(dtype="float32"),
            "ask_volume": pd.Series(dtype="float32"),
        }
    )


def _as_utc(hour: datetime) -> datetime:
    if hour.tzinfo is None:
        return hour.replace(tzinfo=timezone.utc)
    return hour.astimezone(timezone.utc)


def _naive_utc(hour: datetime) -> datetime:
    return _as_utc(hour).replace(tzinfo=None)
