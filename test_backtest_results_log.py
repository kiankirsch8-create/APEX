"""Continuous-loop backtest_results.jsonl gate (default off)."""
from __future__ import annotations

from typing import Any

import continuous_backtester as cb


def test_backtest_results_log_disabled_by_default() -> None:
    assert cb.BACKTEST_RESULTS_LOG_ENABLED is False


def test_shadow_nonfx_disabled_by_default() -> None:
    import shadow_instruments as si

    assert si.SHADOW_NONFX_ENABLED is False
    assert si.SHADOW_INSTRUMENTS == {}


def test_chrono_yf_symbol_uses_fx_suffix_not_misclassified_nonfx() -> None:
    assert cb.chrono_yf_symbol("EURUSD") == "EURUSD=X"
    # Six-letter non-FX catalog symbol must not get a Yahoo FX suffix when disabled.
    assert cb.chrono_yf_symbol("XAUUSD") == "XAUUSD"


def test_cost_and_swap_warnings_once_per_ticker() -> None:
    from unittest import mock

    cb.reset_cost_swap_run_warnings()
    with mock.patch.object(cb, "log") as mock_log:
        cb._estimate_swap_amount(
            ticker="XAUUSD",
            direction="LONG",
            leveraged_exposure=100_000.0,
            nights_held=1.0,
        )
        cb._estimate_swap_amount(
            ticker="XAUUSD",
            direction="LONG",
            leveraged_exposure=100_000.0,
            nights_held=1.0,
        )
    assert mock_log.call_count == 1


def test_yf_startup_probe_keeps_universe_when_majority_fail(monkeypatch: Any) -> None:
    """Transient Yahoo outage must not silently shrink to a near-empty universe."""
    from unittest import mock

    monkeypatch.setattr(cb, "YF_STARTUP_PROBE_RETRY_DELAY_SEC", 0.0)
    cb._CHRONO_YF_EXCLUDED.clear()
    cb._CHRONO_YF_PROBE_LOGGED.clear()
    original = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

    def always_fail(_yf_sym: str, *, ref_date: str | None = None) -> bool:
        return False

    monkeypatch.setattr(cb, "_probe_yf_symbol_resolves", always_fail)
    with mock.patch.object(cb, "log") as mock_log:
        kept, info = cb.filter_chrono_tickers_after_yf_probe(original, context="test")
    assert kept == original
    assert info["probe_failure"] is True
    assert info["kept"] == 4
    assert info["excluded"] == 0
    assert cb._CHRONO_YF_EXCLUDED == set()
    joined = " ".join(str(c) for c in mock_log.call_args_list)
    assert "probe failed" in joined.lower() or "data-source outage" in joined.lower()
    assert "kept=4" in joined


def test_yf_startup_probe_excludes_minority_failures(monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "YF_STARTUP_PROBE_RETRY_DELAY_SEC", 0.0)
    cb._CHRONO_YF_EXCLUDED.clear()
    cb._CHRONO_YF_PROBE_LOGGED.clear()
    original = ["EURUSD", "GBPUSD", "USDJPY", "BADSYM"]

    def resolve(yf_sym: str, *, ref_date: str | None = None) -> bool:
        return "BADSYM" not in yf_sym

    monkeypatch.setattr(cb, "_probe_yf_symbol_resolves", resolve)
    kept, info = cb.filter_chrono_tickers_after_yf_probe(original, context="test")
    assert kept == ["EURUSD", "GBPUSD", "USDJPY"]
    assert info["probe_failure"] is False
    assert info["kept"] == 3
    assert info["excluded"] == 1
    assert "BADSYM" in cb._CHRONO_YF_EXCLUDED


def test_yf_startup_probe_retries_twice_with_delay(monkeypatch: Any) -> None:
    from unittest import mock

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_download(*_a: Any, **_k: Any) -> None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(cb, "safe_yf_download", fake_download)
    monkeypatch.setattr(cb, "time", type("T", (), {"sleep": staticmethod(sleeps.append)})())
    assert cb.YF_STARTUP_PROBE_RETRIES == 2
    ok = cb._probe_yf_symbol_resolves("EURUSD=X", ref_date="2024-06-01")
    assert ok is False
    assert calls["n"] == 2
    assert sleeps == [cb.YF_STARTUP_PROBE_RETRY_DELAY_SEC]


def test_no_eurusd_single_pair_fallback_after_probe() -> None:
    import inspect

    src = inspect.getsource(cb.run_chronological_backtest)
    assert 'tickers = ["EURUSD"]' not in src
    assert "if not tickers:\n                tickers = [\"EURUSD\"]" not in src
