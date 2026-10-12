"""Group D / PR11 — liquidity / previous-higher-low stop placement."""
from __future__ import annotations

import pandas as pd

import continuous_backtester as cb


def _hl_past() -> pd.DataFrame:
    # Strict neighbour pivots: low at i is < both neighbours.
    # Pivot lows at 1.00 then 1.03 (higher-low); last row is entry bar (excluded).
    prices = [
        (1.02, 1.05, 1.01, 1.04),  # 0
        (1.04, 1.06, 1.00, 1.02),  # 1 pivot low 1.00
        (1.02, 1.07, 1.01, 1.06),  # 2
        (1.06, 1.10, 1.05, 1.09),  # 3
        (1.09, 1.12, 1.03, 1.05),  # 4 pivot low 1.03 (HL)
        (1.05, 1.14, 1.04, 1.13),  # 5
        (1.13, 1.15, 1.12, 1.14),  # 6 entry bar (excluded)
    ]
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="D")
    rows = [{"Open": o, "High": h, "Low": l, "Close": c} for o, h, l, c in prices]
    return pd.DataFrame(rows, index=idx)


def test_shadow_configs_include_liquidity_modes() -> None:
    names = [n for n, _ in cb.SHADOW_STOP_CONFIGS]
    assert "prev_higher_low" in names
    assert "nearest_liquidity" in names
    assert "fixed_current" in names


def test_previous_higher_low_and_nearest() -> None:
    past = _hl_past()
    entry = 1.12
    phl = cb.previous_higher_low_price(past, entry=entry, lookback=40)
    assert phl is not None
    assert phl < entry
    nearest = cb.nearest_liquidity_stop_price(
        past, direction="LONG", entry=entry, lookback=40
    )
    assert nearest is not None
    assert nearest < entry


def test_v75_guards_prefer_liquidity_when_enabled() -> None:
    past = _hl_past()
    ind = {"atr": 0.01}
    ok, reason, meta = cb._v75_entry_guards(
        sym="EURUSD",
        tf_key="1d",
        direction="LONG",
        entry=1.12,
        normal_stop=1.115,  # tight model stop
        past=past,
        ind=ind,
    )
    assert ok is True
    assert meta.get("stop_source") in ("prev_higher_low", "model")
    assert meta["normal_stop_price"] < 1.12
    # Liquidity stop should be at or wider than the tight model stop when structure found
    if meta.get("liquidity_stop_price") is not None:
        assert meta["normal_stop_price"] <= meta["liquidity_stop_price"] + 1e-9 or meta[
            "stop_source"
        ] == "prev_higher_low"


def test_shadow_stop_price_modes() -> None:
    past = _hl_past()
    entry = 1.12
    model_stop = 1.10
    atr = 0.01
    phl = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "prev_higher_low", "lookback": 40, "buffer_atr": 0.0},
        direction="LONG",
        entry=entry,
        current_stop=model_stop,
        atr=atr,
        past=past,
        dist_cache={},
    )
    near = cb._shadow_stop_price_for_cfg(
        cfg={"mode": "nearest_liquidity", "lookback": 40, "buffer_atr": 0.0},
        direction="LONG",
        entry=entry,
        current_stop=model_stop,
        atr=atr,
        past=past,
        dist_cache={},
    )
    assert phl is None or phl < entry
    assert near is None or near < entry
