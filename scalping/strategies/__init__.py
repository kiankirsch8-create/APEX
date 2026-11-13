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
from scalping.strategies.ema_zscore import EmaZscoreReversion
from scalping.strategies.grid_reversion import GridReversion
from scalping.strategies.mean_anchor import MeanAnchorReversion
from scalping.strategies.timeframe import Resampled, make_timeframe

REGISTRY: dict[str, type[Strategy]] = {
    BollingerReversion.name: BollingerReversion,
    DonchianBreakout.name: DonchianBreakout,
    EmaZscoreReversion.name: EmaZscoreReversion,
    GridReversion.name: GridReversion,
    MeanAnchorReversion.name: MeanAnchorReversion,
}


# Higher-timeframe variants, e.g. "bollinger_reversion@M15". Kept out of
# REGISTRY so "--strategy all" still means the M1 reference set.
TIMEFRAMES = (5, 15, 60)
TIMEFRAME_REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (make_timeframe(base, m) for base in REGISTRY.values() for m in TIMEFRAMES)
}


# Research plug-ins: every module in each directory listed in
# SCALPING_PLUGIN_DIRS (os.pathsep-separated) is imported, and every Strategy
# subclass defined at its top level with a ``name`` is registered. This lets
# research strategies live outside the harness (research/strategies/).
PLUGIN_REGISTRY: dict[str, type[Strategy]] = {}


def _load_plugins() -> None:
    import importlib.util
    import inspect
    import os
    import sys
    from pathlib import Path

    for directory in filter(None, os.environ.get("SCALPING_PLUGIN_DIRS", "").split(os.pathsep)):
        for path in sorted(Path(directory).glob("*.py")):
            if path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"scalping_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module  # so inspect / pickle can find the source
            spec.loader.exec_module(module)
            for obj in vars(module).values():
                if (inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy
                        and obj is not Resampled and getattr(obj, "name", "strategy") != "strategy"
                        and (obj.__module__ == module.__name__
                             or (issubclass(obj, Resampled)
                                 and getattr(obj, "inner_cls", None) is not None
                                 and obj.inner_cls.__module__ == module.__name__))):
                    if obj.name in REGISTRY or obj.name in TIMEFRAME_REGISTRY:
                        raise ValueError(f"plugin {path.name}: strategy name {obj.name!r} is taken")
                    other = PLUGIN_REGISTRY.get(obj.name)
                    if other is not None and other is not obj:
                        raise ValueError(f"plugin {path.name}: strategy name {obj.name!r} defined twice")
                    PLUGIN_REGISTRY[obj.name] = obj


_load_plugins()


def get_strategy(name: str) -> type[Strategy]:
    for registry in (REGISTRY, TIMEFRAME_REGISTRY, PLUGIN_REGISTRY):
        if name in registry:
            return registry[name]
    known = ", ".join(sorted(REGISTRY) + sorted(TIMEFRAME_REGISTRY) + sorted(PLUGIN_REGISTRY))
    raise KeyError(f"Unknown strategy {name!r}. Known: {known}")


def all_strategies() -> list[type[Strategy]]:
    return [REGISTRY[name] for name in sorted(REGISTRY)]


__all__ = [
    "REGISTRY",
    "PLUGIN_REGISTRY",
    "TIMEFRAME_REGISTRY",
    "Resampled",
    "make_timeframe",
    "BarContext",
    "BollingerReversion",
    "CancelIntent",
    "CloseIntent",
    "DonchianBreakout",
    "EmaZscoreReversion",
    "GridReversion",
    "MeanAnchorReversion",
    "ModifyIntent",
    "OrderIntent",
    "Strategy",
    "all_strategies",
    "get_strategy",
]
