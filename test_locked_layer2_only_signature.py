"""Regression: ``_locked_layer2_only`` must accept and forward ``past``/``ind``.

Original crash::

    TypeError: _v75_backtest_strategy_allowed() missing 2 required
    keyword-only arguments: 'past' and 'ind'
      at apex_trader_v76_private.py:2220 (_locked_layer2_only)
      reached from run_full_scan_v76 → evaluate_cell_v76

Root cause
----------
``apex_v76_decision_logic._v75_backtest_strategy_allowed`` has the signature
``(sid, *, past, ind)`` — both kwargs are required (matches
``continuous_backtester._v75_backtest_strategy_allowed`` at line 8584).

Both engines were calling it bare from ``_locked_layer2_only``. The fix is a
pure plumbing change: ``_locked_layer2_only`` now accepts the same ``past``
and ``ind`` already prepared by ``evaluate_cell_v76`` and forwards them
verbatim — exactly the way the backtester does at
``continuous_backtester.py:1886``.
"""
from __future__ import annotations

import inspect

import pytest

import apex_v76_decision_logic as _v76_logic
import apex_trader_v76 as v76_public
import apex_trader_v76_private as v76_private


ENGINES = [
    ("apex_trader_v76", v76_public),
    ("apex_trader_v76_private", v76_private),
]


@pytest.mark.parametrize("name,engine", ENGINES)
def test_locked_layer2_only_signature_requires_past_and_ind(name, engine):
    """``_locked_layer2_only`` must declare ``past`` and ``ind`` as kwargs so it
    can forward them to the decision_logic gate."""
    sig = inspect.signature(engine._locked_layer2_only)
    assert "past" in sig.parameters, (
        f"{name}._locked_layer2_only must take `past` so it can forward it to "
        "_v76_logic._v75_backtest_strategy_allowed(sid, *, past, ind)."
    )
    assert "ind" in sig.parameters, (
        f"{name}._locked_layer2_only must take `ind` so it can forward it to "
        "_v76_logic._v75_backtest_strategy_allowed(sid, *, past, ind)."
    )


@pytest.mark.parametrize("name,engine", ENGINES)
def test_locked_layer2_only_forwards_past_and_ind(name, engine, monkeypatch):
    """Call ``_locked_layer2_only`` with a captured spy for the gate and verify
    the exact ``past`` / ``ind`` objects it was called with get forwarded."""
    captured = {}

    def spy(sid, *, past, ind):
        captured[sid] = (past, ind)
        return True

    monkeypatch.setattr(_v76_logic, "_v75_backtest_strategy_allowed", spy)
    monkeypatch.setattr(
        _v76_logic, "LOCKED_STRATEGY_IDS",
        {"T01_EMA_PULLBACK", "B01_RANGE_BREAKOUT"},
    )

    sentinel_past = object()
    sentinel_ind = {"rsi": 42.0}
    layer2 = [
        ("T01_EMA_PULLBACK", "LONG", 3),
        ("B01_RANGE_BREAKOUT", "SHORT", 2),
    ]

    out = engine._locked_layer2_only(
        layer2, tf_key="1d", past=sentinel_past, ind=sentinel_ind
    )

    assert len(out) == 2
    assert captured["T01_EMA_PULLBACK"] == (sentinel_past, sentinel_ind)
    assert captured["B01_RANGE_BREAKOUT"] == (sentinel_past, sentinel_ind)


@pytest.mark.parametrize("name,engine", ENGINES)
def test_locked_layer2_only_still_filters_non_locked(name, engine, monkeypatch):
    """Plumbing fix must not alter filtering behavior: non-locked strategies
    still get dropped, LOCKED survivors still pass through unchanged."""
    monkeypatch.setattr(
        _v76_logic,
        "_v75_backtest_strategy_allowed",
        lambda sid, *, past, ind: True,
    )
    monkeypatch.setattr(_v76_logic, "LOCKED_STRATEGY_IDS", {"T01_EMA_PULLBACK"})

    layer2 = [
        ("T01_EMA_PULLBACK", "LONG", 3),
        ("Z99_NOT_LOCKED", "LONG", 2),
    ]
    out = engine._locked_layer2_only(layer2, tf_key="1d", past=None, ind={})
    assert len(out) == 1
    assert out[0][0] == "T01_EMA_PULLBACK"


@pytest.mark.parametrize("name,engine", ENGINES)
def test_locked_layer2_only_still_blocks_m02_on_4h(name, engine, monkeypatch):
    """The explicit M02_MACD_ZERO_CROSS @ 4h block must still fire (same
    behavior as before the plumbing change)."""
    monkeypatch.setattr(
        _v76_logic,
        "_v75_backtest_strategy_allowed",
        lambda sid, *, past, ind: True,
    )
    monkeypatch.setattr(_v76_logic, "LOCKED_STRATEGY_IDS", {"M02_MACD_ZERO_CROSS"})
    out = engine._locked_layer2_only(
        [("M02_MACD_ZERO_CROSS", "LONG", 3)],
        tf_key="4h",
        past=None,
        ind={},
    )
    assert out == []


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
