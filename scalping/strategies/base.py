"""Strategy contract.

A strategy sees closed M1 bars and the positions that already exist. It
returns order, cancel, close, and modify intents. Those intents are active
on the next bar, so a signal cannot fill on the bar that produced it.

Parameters are fixed by the constructor. The runner builds a fresh instance
per pair per block from the same parameter dict and never feeds results back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from scalping.costs import OrderType, Side


@dataclass(frozen=True)
class OrderIntent:
    type: OrderType
    side: Side
    lots: float
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    tag: str = ""


@dataclass(frozen=True)
class CancelIntent:
    order_id: int


@dataclass(frozen=True)
class CloseIntent:
    """Market-close a position on the next bar. ``None`` closes every position."""

    position_id: int | None = None


@dataclass(frozen=True)
class ModifyIntent:
    position_id: int
    sl: float | None = None
    tp: float | None = None


@dataclass(frozen=True)
class PositionView:
    id: int
    side: Side
    lots: float
    entry_price: float
    sl: float | None
    tp: float | None
    entry_time: str
    tag: str


@dataclass(frozen=True)
class OrderView:
    id: int
    type: OrderType
    side: Side
    lots: float
    price: float | None
    sl: float | None
    tp: float | None
    tag: str


class BarWindow:
    """Closed bars ``[0, end)``. Index ``end`` and beyond are not exposed.

    ``volume`` is the M1 bid+ask volume already stored on the bar. Strategies
    can read it for a later VWAP or volume filter. It is not required.
    """

    def __init__(self, market, end: int):
        self._market = market
        self._end = int(end)

    def __len__(self) -> int:
        return self._end

    @property
    def open(self) -> np.ndarray:
        return self._view(self._market.open)

    @property
    def high(self) -> np.ndarray:
        return self._view(self._market.high)

    @property
    def low(self) -> np.ndarray:
        return self._view(self._market.low)

    @property
    def close(self) -> np.ndarray:
        return self._view(self._market.close)

    @property
    def spread_close(self) -> np.ndarray:
        return self._view(self._market.spread_close)

    # Tick and clock features (see MarketData). Empty-safe: absent fields read as NaN.
    def _feature(self, name: str) -> np.ndarray:
        values = getattr(self._market, name, None)
        if values is None:
            return np.full(self._end, np.nan)
        return self._view(values)

    @property
    def tick_count(self) -> np.ndarray:
        return self._feature("tick_count")

    @property
    def spread_high_raw(self) -> np.ndarray:
        return self._feature("spread_high_raw")

    @property
    def upticks(self) -> np.ndarray:
        return self._feature("upticks")

    @property
    def downticks(self) -> np.ndarray:
        return self._feature("downticks")

    @property
    def london_minute(self) -> np.ndarray:
        return self._feature("london_minute")

    @property
    def ny_minute(self) -> np.ndarray:
        return self._feature("ny_minute")

    @property
    def weekday(self) -> np.ndarray:
        return self._feature("weekday")

    @property
    def news_next_min(self) -> np.ndarray:
        return self._feature("news_next_min")

    @property
    def news_last_min(self) -> np.ndarray:
        return self._feature("news_last_min")

    @property
    def volume(self) -> np.ndarray:
        values = getattr(self._market, "volume", None)
        if values is None:
            return np.ones(self._end, dtype=np.float64)
        return self._view(values)

    def _view(self, values: np.ndarray) -> np.ndarray:
        view = values[: self._end]
        try:
            view.setflags(write=False)
        except ValueError:
            return view
        return view


@dataclass(frozen=True)
class BarContext:
    symbol: str
    point: float
    bar_index: int
    time: str
    open: float
    high: float
    low: float
    close: float
    spread_close: float
    spread_mean: float
    window: BarWindow
    positions: tuple[PositionView, ...]
    pending: tuple[OrderView, ...]
    equity: float

    @property
    def volume(self) -> float:
        """Bid+ask volume of this closed bar. The window carries the history."""
        if len(self.window) == 0:
            return 0.0
        return float(self.window.volume[-1])


class Strategy(ABC):
    """Plug-in point for a scalping idea. One instance per block per pair."""

    name = "strategy"

    def parameters(self) -> dict:
        """JSON-safe parameters. The runner records these and does not change them."""
        return {}

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> list:
        """Return intents to activate on the following bar."""
