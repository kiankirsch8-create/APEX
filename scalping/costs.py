"""Spread, commission, and slippage.

Spread is never a configured constant. A fill reads the historical bid and ask
of the tick that executes it. Slippage is an extra penalty, in points, applied
to market orders, stop entries, and stop-loss exits. Limit entries and
take-profits fill at their level.

P&L and commission are converted into the account currency (EUR by default).
Conversion is pessimistic: a credit buys the account currency at the ask, a
debit sells it at the bid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scalping.instruments import Instrument


class Side(int, Enum):
    BUY = 1
    SELL = -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class FillKind(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class CostModel:
    """Account-currency commission, market-order slippage, broker spread floor
    and overnight financing.

    The spread is the tick's ask − bid. ``min_spread_points`` (default 0, off)
    widens any tick whose spread is tighter than the broker's, symmetrically
    around the mid, so a raw ECN/aggregator feed can model a spread-only CFD
    broker (Capital.com EURUSD: about 7 points = 0.7 pip, no commission).

    ``financing_long_pct`` / ``financing_short_pct`` are the overnight funding
    cost per night as a percent of position notional (positive = you pay),
    charged for every 22:00 UTC rollover a position is held across, weekend
    nights included (calendar-day accrual, as CFD brokers charge).
    """

    commission_per_lot_side: float = 3.5
    commission_currency: str = "USD"
    slippage_points: float = 2.0
    account_currency: str = "EUR"
    min_spread_points: float = 0.0
    financing_long_pct: float = 0.0
    financing_short_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.commission_per_lot_side < 0:
            raise ValueError("commission_per_lot_side must be >= 0")
        if self.slippage_points < 0:
            raise ValueError("slippage_points must be >= 0")
        if self.min_spread_points < 0:
            raise ValueError("min_spread_points must be >= 0")
        object.__setattr__(self, "commission_currency", self.commission_currency.upper())
        object.__setattr__(self, "account_currency", self.account_currency.upper())

    def slippage_price(self, instrument: Instrument) -> float:
        return float(self.slippage_points) * float(instrument.point)

    def commission_cash(self, lots: float) -> tuple[float, str]:
        """One side of commission, as a positive amount in ``commission_currency``."""
        return abs(float(lots)) * float(self.commission_per_lot_side), self.commission_currency

    def as_dict(self) -> dict[str, float | str]:
        return {
            "commission_per_lot_side": self.commission_per_lot_side,
            "commission_currency": self.commission_currency,
            "slippage_points": self.slippage_points,
            "account_currency": self.account_currency,
            "min_spread_points": self.min_spread_points,
            "financing_long_pct": self.financing_long_pct,
            "financing_short_pct": self.financing_short_pct,
        }


def execution_price(
    *,
    side: Side,
    kind: FillKind,
    bid: float,
    ask: float,
    level: float | None,
    slippage_price: float,
) -> float | None:
    """Fill price on this quote, or ``None`` when the order does not trigger.

    Rules (long-side wording; shorts are the mirror):

    * market buy fills at ask + slippage; market sell at bid − slippage
    * limit buy fills only when ask <= level, at the level (no price improvement)
    * limit sell fills only when bid >= level, at the level
    * stop buy fills when ask >= level, at ask + slippage (a gap fills worse than the stop)
    * stop sell fills when bid <= level, at bid − slippage
    * long stop-loss fills when bid <= level, at min(level, bid) − slippage
    * short stop-loss fills when ask >= level, at max(level, ask) + slippage
    * take-profit fills at the level (long: bid >= level, short: ask <= level)
    """
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        return None
    slip = float(slippage_price)
    buy = side is Side.BUY or int(side) == int(Side.BUY)

    if kind is FillKind.MARKET:
        return (ask + slip) if buy else (bid - slip)

    if level is None or level <= 0.0:
        return None

    if kind is FillKind.LIMIT:
        if buy:
            return level if ask <= level else None
        return level if bid >= level else None

    if kind is FillKind.STOP:
        if buy:
            return (ask + slip) if ask >= level else None
        return (bid - slip) if bid <= level else None

    if kind is FillKind.STOP_LOSS:
        # ``side`` is the position side, not the exit side.
        if buy:
            if bid <= level:
                return min(level, bid) - slip
            return None
        if ask >= level:
            return max(level, ask) + slip
        return None

    if kind is FillKind.TAKE_PROFIT:
        if buy:
            return level if bid >= level else None
        return level if ask <= level else None

    raise ValueError(f"Unknown fill kind {kind!r}")


def quote_pnl(side: Side, entry: float, exit_: float, lots: float, contract_size: float) -> float:
    """P&L in the pair's quote currency. Spread and slippage are already in the prices."""
    signed = 1.0 if int(side) == int(Side.BUY) else -1.0
    return (float(exit_) - float(entry)) * signed * float(lots) * float(contract_size)


def convert_to_eur(
    amount: float,
    currency: str,
    *,
    eurusd_bid: float,
    eurusd_ask: float,
    usdjpy_bid: float | None = None,
    usdjpy_ask: float | None = None,
) -> float:
    """Convert a signed cash amount into EUR, pessimistically.

    * EUR is already account currency.
    * A positive USD amount buys EUR at the EURUSD ask (fewer euros).
      A negative USD amount sells EUR at the EURUSD bid (more euros lost).
    * JPY is converted to USD at the worse USDJPY side, then USD to EUR.
    """
    ccy = currency.upper()
    value = float(amount)
    if value == 0.0 or ccy == "EUR":
        return value
    if eurusd_bid <= 0.0 or eurusd_ask <= 0.0 or eurusd_ask < eurusd_bid:
        raise ValueError("EURUSD bid/ask required to convert into EUR")

    if ccy == "JPY":
        if usdjpy_bid is None or usdjpy_ask is None:
            raise ValueError("USDJPY bid/ask required to convert JPY into EUR")
        if usdjpy_bid <= 0.0 or usdjpy_ask <= 0.0 or usdjpy_ask < usdjpy_bid:
            raise ValueError("USDJPY bid/ask are invalid")
        usd = value / usdjpy_ask if value > 0.0 else value / usdjpy_bid
        return convert_to_eur(usd, "USD", eurusd_bid=eurusd_bid, eurusd_ask=eurusd_ask)

    if ccy == "USD":
        rate = eurusd_ask if value > 0.0 else eurusd_bid
        return value / rate

    raise ValueError(f"Cannot convert {ccy} into EUR")
