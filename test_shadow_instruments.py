"""Tests for unified shadow instrument evaluation."""
from __future__ import annotations

import continuous_backtester as cb
import shadow_instruments as si


def test_hard_block_bypassed_in_shadow_eval() -> None:
    si.enter_shadow_eval("AUDUSD", shadow_class="blocked_fx")
    try:
        assert cb._hard_block_skip_reason("AUDUSD", "T01") is None
        assert si.trade_fields_for_row("AUDUSD")["shadow_class"] == "blocked_fx"
    finally:
        si.exit_shadow_eval()
    assert cb._hard_block_skip_reason("AUDUSD", "T01") is not None


def test_shadow_ab_histories_per_class() -> None:
    si.reset_run_state()
    cb._STRAT_PNL_HISTORY.clear()
    cb._STRAT_PNL_HISTORY["T01"] = [50.0]
    row = {
        "strategy_id": "T01",
        "confidence": "HIGH",
        "macro_bias": "NEUTRAL",
        "outcome": "WIN",
        "pnl_dollars": 120.0,
        "max_risk_dollars": 100.0,
        "ticker": "EURAUD",
    }
    si._record_ab_histories(row, "blocked_fx")
    assert si._shadow_strat_by_class["blocked_fx"]["T01"] == [120.0]
    assert cb._STRAT_PNL_HISTORY["T01"] == [50.0]


def test_compute_summary_r_primary() -> None:
    trades = [
        {
            "shadow_instrument": "AUDUSD",
            "ticker": "AUDUSD",
            "date": "2022-03-01",
            "outcome": "WIN",
            "pnl_dollars": 100.0,
            "pnl_r": 1.0,
        },
        {
            "shadow_instrument": "AUDUSD",
            "ticker": "AUDUSD",
            "date": "2022-06-01",
            "outcome": "LOSS",
            "pnl_dollars": -50.0,
            "pnl_r": -0.5,
        },
    ]
    summary = si.compute_summary(trades)
    aud = summary["by_ticker"]["AUDUSD"]
    assert aud["net_r"] == 0.5
    assert aud["estimated_dollars"]["net_pnl"] == 50.0
    assert "2022" in aud["by_year"]


def test_instrument_sizing_contracts() -> None:
    ai: dict = {"stop_loss": 99.0}
    spec = si.SHADOW_INSTRUMENTS["ES"]
    si.apply_instrument_sizing(ai, spec=spec, entry=100.0, risk_dollars=100.0)
    assert ai["_instrument_contracts"] > 0
    assert ai["_max_risk_dollars"] == 100.0


def test_real_curve_tickers_exclude_blocked() -> None:
    real = cb._real_chrono_forex_tickers()
    assert "AUDUSD" not in real
    assert "EURUSD" in real


def test_shadow_flag_off() -> None:
    assert cb._shadow_eval_active("AUDUSD", chrono_yfinance=False) is False
