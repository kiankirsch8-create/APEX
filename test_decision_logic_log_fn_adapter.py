"""Regression: ``apex_v76_decision_logic`` is message-first, ``live_log`` is
level-first. Injecting ``live_log`` directly crashes with::

    TypeError: live_log() got multiple values for argument 'level'

Both engines ship a ``_decision_log`` adapter that bridges the convention.
The two entry points called out in the original crash — ``v72_load_strategy_status``
and ``resolve_trailing_regime`` — must accept ``log_fn=_decision_log`` without
raising, including when the module actually fires an "[info] msg" log line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import apex_v76_decision_logic as _v76_logic
import apex_trader_v76 as v76_public
import apex_trader_v76_private as v76_private


ENGINES = [
    ("apex_trader_v76", v76_public),
    ("apex_trader_v76_private", v76_private),
]


@pytest.mark.parametrize("name,engine", ENGINES)
def test_decision_log_adapter_exists_and_bridges_live_log(name, engine, monkeypatch):
    assert hasattr(engine, "_decision_log"), (
        f"{name} is missing the _decision_log adapter required to bridge "
        "message-first decision_logic callers to level-first live_log."
    )

    captured = []

    def fake_live_log(level, msg, **fields):
        captured.append((level, msg, fields))

    monkeypatch.setattr(engine, "live_log", fake_live_log)

    # Message-first call (how decision_logic always calls log_fn).
    engine._decision_log("hello world", level="warning", ticker="EURUSD")
    engine._decision_log("default-level call")  # exercise the "info" default

    assert captured[0] == ("warning", "hello world", {"ticker": "EURUSD"})
    assert captured[1] == ("info", "default-level call", {})


@pytest.mark.parametrize("name,engine", ENGINES)
def test_v72_load_strategy_status_accepts_decision_log(name, engine, tmp_path, monkeypatch):
    """The exact failure in the bug report: scheduler crashed here with
    ``TypeError: live_log() got multiple values for argument 'level'``.
    With the adapter the call must complete cleanly even when the module
    actually emits its "[v76] strategy_status locked=... blocked=..." log line.
    """
    monkeypatch.setattr(_v76_logic, "bind_backtester_parity_state", lambda: None)

    _v76_logic.v72_load_strategy_status(Path(tmp_path), log_fn=engine._decision_log)


@pytest.mark.parametrize("name,engine", ENGINES)
def test_resolve_trailing_regime_accepts_decision_log(name, engine):
    """Second decision_logic entry point we inject into. STRONG_TAILWIND forces
    the log line to actually fire — proving the adapter handles the real
    ``log_fn(msg, level="info")`` call site, not just a no-op path.
    """
    out = _v76_logic.resolve_trailing_regime(
        "STRONG_TAILWIND",
        0.75,
        0.0,
        timeframe="1h",
        log_fn=engine._decision_log,
    )
    assert out == "TRENDING"

    # 4h branch — different code path, same adapter.
    out2 = _v76_logic.resolve_trailing_regime(
        "NEUTRAL",
        0.0,
        0.0,
        timeframe="4h",
        log_fn=engine._decision_log,
    )
    assert out2 == "CHOPPY"


@pytest.mark.parametrize("name,engine", ENGINES)
def test_live_log_still_level_first(name, engine):
    """We did NOT rewrite live_log; it must stay (level, msg, **fields)."""
    import inspect

    sig = inspect.signature(engine.live_log)
    params = list(sig.parameters.values())
    assert params[0].name == "level"
    assert params[1].name == "msg"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
