"""Reference strategies and the registry the runner uses."""

from __future__ import annotations

from scalping.strategies.base import (
    BarContext,
    CancelIntent,
    CloseIntent,
    ModifyIntent,
    OrderIntent,
    Strategy,
)
from scalping.strategies.bollinger import BollingerReversion
from scalping.strategies.donchian import DonchianBreakout

REGISTRY: dict[str, type[Strategy]] = {
    BollingerReversion.name: BollingerReversion,
    DonchianBreakout.name: DonchianBreakout,
}


def get_strategy(name: str) -> type[Strategy]:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"Unknown strategy {name!r}. Known: {known}") from exc


def all_strategies() -> list[type[Strategy]]:
    return [REGISTRY[name] for name in sorted(REGISTRY)]


__all__ = [
    "REGISTRY",
    "BarContext",
    "BollingerReversion",
    "CancelIntent",
    "CloseIntent",
    "DonchianBreakout",
    "ModifyIntent",
    "OrderIntent",
    "Strategy",
    "all_strategies",
    "get_strategy",
]
