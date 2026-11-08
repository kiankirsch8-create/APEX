"""Deterministic EURUSD path for the demo runner and engine checks.

This is not market data. It only exists so the reference strategies can be
scored without a Dukascopy download.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scalping.data.bars import ticks_to_m1
from scalping.market import MarketData, build_market


def synthetic_eurusd(
    n_bars: int = 600,
    seed: int = 7,
    spread: float = 0.00008,
    start: str = "2020-01-06T08:00:00",
) -> MarketData:
    rng = np.random.default_rng(seed)
    closes = np.empty(n_bars, dtype=np.float64)
    price = 1.1000
    for i in range(n_bars):
        shock = 0.0
        # Occasional pushes so a band or a channel can actually break.
        if i % 47 == 0 and i > 0:
            shock = 0.0012 * (1.0 if (i // 47) % 2 == 0 else -1.0)
        price = price + 0.12 * (1.1000 - price) + shock + float(rng.normal(0.0, 0.00022))
        closes[i] = price
    origin = np.datetime64(start)
    half = spread / 2.0
    rows: list[dict] = []
    for i, close in enumerate(closes):
        previous = float(closes[i - 1]) if i else float(close)
        open_ = previous
        low = min(open_, float(close)) - abs(float(rng.normal(0.0, 0.00012)))
        high = max(open_, float(close)) + abs(float(rng.normal(0.0, 0.00012)))
        for k, mid in enumerate((open_, low, high, close)):
            stamp = origin + np.timedelta64(i, "m") + np.timedelta64(k * 12, "s")
            rows.append(
                {
                    "timestamp": stamp,
                    "bid": mid - half,
                    "ask": mid + half,
                    "bid_volume": np.float32(1.0),
                    "ask_volume": np.float32(1.0),
                }
            )
    ticks = pd.DataFrame(rows)
    ticks["timestamp"] = pd.to_datetime(ticks["timestamp"], utc=True)
    bars = ticks_to_m1(ticks)
    return build_market(ticks, bars)
