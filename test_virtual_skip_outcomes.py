"""Group D / PR9 — virtual outcomes on decision-skips (not capital-affecting)."""
from __future__ import annotations

import pandas as pd

import continuous_backtester as cb


def _future_hits_tp_long(entry: float = 1.1000, stop: float = 1.0950, tp: float = 1.1100) -> pd.DataFrame:
    # Candle 1: goes up through TP without tagging stop.
    idx = pd.date_range("2024-01-02", periods=3, freq="D")
    return pd.DataFrame(
        {
            "Open": [entry, tp, tp],
            "High": [tp + 0.001, tp + 0.001, tp + 0.001],
            "Low": [entry - 0.0005, tp - 0.0005, tp - 0.0005],
            "Close": [tp, tp, tp],
        },
        index=idx,
    )


def _future_hits_stop_long(entry: float = 1.1000, stop: float = 1.0950) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", periods=3, freq="D")
    return pd.DataFrame(
        {
            "Open": [entry, stop, stop],
            "High": [entry + 0.0005, stop + 0.0002, stop + 0.0002],
            "Low": [stop - 0.0005, stop - 0.0005, stop - 0.0005],
            "Close": [stop, stop, stop],
        },
        index=idx,
    )


def test_classifier_allows_decision_skips_not_errors() -> None:
    assert cb.is_decision_skip_for_virtual("[BAD PERIOD] FRAGILE strategy T01 paused")
    assert cb.is_decision_skip_for_virtual("Counter-trend blocked: weekly downtrend")
    assert cb.is_decision_skip_for_virtual("[AB_THROTTLE_SKIP] sized to 0.18 — skip instead")
    assert cb.is_decision_skip_for_virtual("[NEUTRAL+RANGING SKIP] M03 EURUSD: ...")
    assert not cb.is_decision_skip_for_virtual("[BLOCKED PAIR] USDSEK in BLOCKED_PAIRS list")
    assert not cb.is_decision_skip_for_virtual("Insufficient OHLC history or forward window")
    assert not cb.is_decision_skip_for_virtual("Forward simulation failed")


def test_virtual_fields_win_and_loss() -> None:
    entry, stop, tp = 1.1000, 1.0950, 1.1100
    win = cb._virtual_skip_outcome_fields(
        direction="LONG",
        entry=entry,
        stop_loss=stop,
        tp1=tp,
        tp2=tp,
        tp3=tp,
        future=_future_hits_tp_long(entry, stop, tp),
        timeframe="1d",
        ticker="EURUSD",
    )
    assert win["virtual_outcome"] == "WIN"
    assert win["virtual_pnl_r"] > 0

    loss = cb._virtual_skip_outcome_fields(
        direction="LONG",
        entry=entry,
        stop_loss=stop,
        tp1=tp,
        tp2=tp,
        tp3=tp,
        future=_future_hits_stop_long(entry, stop),
        timeframe="1d",
        ticker="EURUSD",
    )
    assert loss["virtual_outcome"] == "LOSS"
    assert loss["virtual_pnl_r"] < 0


def test_skipped_row_attaches_virtual_for_bad_period() -> None:
    ai = {
        "strategy_id": "T01_TREND_PULLBACK",
        "direction": "LONG",
        "entry": 1.1000,
        "stop_loss": 1.0950,
        "tp1": 1.1100,
        "tp2": 1.1150,
        "tp3": 1.1200,
        "period_mode": "BAD",
        "macro_bias": "NEUTRAL",
    }
    row = cb._skipped_backtest_row(
        sym="EURUSD",
        timeframe="1d",
        analysis_date="2024-01-02",
        price=1.1000,
        zone_pct=40.0,
        zone_label="DISCOUNT",
        skip_reason="[BAD PERIOD] FRAGILE strategy T01_TREND_PULLBACK paused — bad period mode active",
        ai=ai,
        tf_key="1d",
        is_exotic=False,
        future=_future_hits_tp_long(),
        period_mode="BAD",
    )
    assert row["skipped"] is True
    assert row["outcome"] == "SKIPPED"
    assert row["pnl_dollars"] == 0.0
    assert row["virtual_outcome"] == "WIN"
    assert "virtual_pnl_r" in row
    # Capital/history untouched — helper has no side effects on module histories.
    before = list(cb._STRAT_PNL_HISTORY.get("T01_TREND_PULLBACK") or [])
    assert before == list(cb._STRAT_PNL_HISTORY.get("T01_TREND_PULLBACK") or [])


def test_blocked_pair_and_no_data_have_no_virtual() -> None:
    ai = {"strategy_id": "SKIP", "direction": "NONE"}
    blocked = cb._skipped_backtest_row(
        sym="USDSEK",
        timeframe="1d",
        analysis_date="2024-01-02",
        price=10.0,
        zone_pct=50.0,
        zone_label="EQUILIBRIUM",
        skip_reason="[BLOCKED PAIR] USDSEK in BLOCKED_PAIRS list",
        ai=ai,
        tf_key="1d",
        is_exotic=True,
        future=_future_hits_tp_long(10.0, 9.95, 10.2),
    )
    assert "virtual_outcome" not in blocked

    nodata = cb._skipped_backtest_row(
        sym="EURUSD",
        timeframe="1d",
        analysis_date="2024-01-02",
        price=1.1,
        zone_pct=50.0,
        zone_label="EQUILIBRIUM",
        skip_reason="Insufficient OHLC history or forward window",
        ai={"strategy_id": "SKIP", "direction": "LONG", "entry": 1.1, "stop_loss": 1.095, "tp1": 1.11},
        tf_key="1d",
        is_exotic=False,
        future=_future_hits_tp_long(),
    )
    assert "virtual_outcome" not in nodata


def test_throttle_skip_reason_gets_virtual() -> None:
    ai = {
        "strategy_id": "T01",
        "direction": "LONG",
        "entry": 1.1000,
        "stop_loss": 1.0950,
        "tp1": 1.1100,
        "tp2": 1.1100,
        "tp3": 1.1100,
        "_ab_throttle": 0.18,
    }
    row = cb._skipped_backtest_row(
        sym="EURUSD",
        timeframe="1d",
        analysis_date="2024-01-02",
        price=1.1000,
        zone_pct=40.0,
        zone_label="DISCOUNT",
        skip_reason="[AB_THROTTLE_SKIP] ab_throttle=0.18 — skip instead of size-down",
        ai=ai,
        tf_key="1d",
        is_exotic=False,
        future=_future_hits_stop_long(),
    )
    assert row["virtual_outcome"] == "LOSS"
    assert row["pnl_dollars"] == 0.0
