"""FX symbols the scalping backtest knows how to price and convert."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    """One traded pair.

    ``point`` is the smallest quoted increment (a pipette: 1e-5 on EURUSD,
    1e-3 on USDJPY). Slippage is configured in points.

    ``price_scale`` is the Dukascopy ``.bi5`` integer divisor
    (100_000 for 5-digit pairs, 1_000 for JPY pairs).
    """

    symbol: str
    base: str
    quote: str
    point: float
    pip: float
    price_scale: int
    contract_size: float = 100_000.0
    digits: int = 5

    @property
    def quote_is_usd(self) -> bool:
        return self.quote == "USD"

    @property
    def quote_is_jpy(self) -> bool:
        return self.quote == "JPY"


_PAIRS: dict[str, Instrument] = {
    "EURUSD": Instrument(
        symbol="EURUSD",
        base="EUR",
        quote="USD",
        point=1e-5,
        pip=1e-4,
        price_scale=100_000,
        digits=5,
    ),
    "GBPUSD": Instrument(
        symbol="GBPUSD",
        base="GBP",
        quote="USD",
        point=1e-5,
        pip=1e-4,
        price_scale=100_000,
        digits=5,
    ),
    "USDJPY": Instrument(
        symbol="USDJPY",
        base="USD",
        quote="JPY",
        point=1e-3,
        pip=1e-2,
        price_scale=1_000,
        digits=3,
    ),
}


def get_instrument(symbol: str) -> Instrument:
    key = symbol.upper().replace("/", "").replace("-", "")
    try:
        return _PAIRS[key]
    except KeyError as exc:
        known = ", ".join(sorted(_PAIRS))
        raise KeyError(f"Unknown scalping symbol {symbol!r}. Known: {known}") from exc


def known_symbols() -> tuple[str, ...]:
    return tuple(_PAIRS)
