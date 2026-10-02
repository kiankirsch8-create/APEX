"""Continuous-loop backtest_results.jsonl gate (default off)."""
from __future__ import annotations

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
