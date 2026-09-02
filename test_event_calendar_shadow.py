"""Unit tests for static event calendar, attribution fields, and event shadow curves."""
from __future__ import annotations

from datetime import date

import continuous_backtester as cb


def test_high_impact_events_cover_range() -> None:
    assert len(cb.HIGH_IMPACT_EVENTS) > 500
    first = cb.HIGH_IMPACT_EVENTS[0][0]
    last = cb.HIGH_IMPACT_EVENTS[-1][0]
    assert first >= "2021-01-01"
    assert last <= "2026-12-31"
    usd_types = {e[2] for e in cb.HIGH_IMPACT_EVENTS if e[1] == "USD"}
    assert "FOMC" in usd_types
    assert "CPI" in usd_types
    assert "NFP" in usd_types


def test_events_for_currency_fomc_day() -> None:
    evs = cb._events_for_currency("USD", "2021-01-27")
    assert any(e[2] == "FOMC" for e in evs)


def test_days_to_next_event_usdjpy() -> None:
    # 2021-01-26 is one day before 2021-01-27 FOMC (USD leg)
    dtn = cb._days_to_next_event("USDJPY", "2021-01-26")
    assert dtn == 1


def test_event_attribution_fields() -> None:
    fields = cb._event_attribution_fields(
        ticker="USDJPY",
        entry_date="2021-01-26",
        nights_held=2.0,
    )
    assert fields["event_days_to_next"] == 1
    assert fields["event_entry_window"] == "PRE_1D"
    assert "FOMC" in fields["event_held_types"]
    assert fields["event_held_through"] >= 1


def test_event_shadow_block_pre3() -> None:
    runner = cb._ShadowGuardRunner()
    ctx = {
        "date": "2021-01-25",
        "ticker": "USDJPY",
        "timeframe": "1d",
        "strategy_id": "T01",
        "confidence": "MEDIUM",
        "macro_bias": "NEUTRAL",
        "baseline_pnl": 100.0,
        "ab_throttle": 1.0,
        "pre_ab_risk_pct": 0.01,
        "pre_ab_max_risk": 100.0,
        "max_risk_dollars": 100.0,
        "outcome": "WIN",
    }
    row = runner.process_trade(ctx)
    assert row["ev_block_pre3"]["blocked"] is True
    assert row["ev_block_pre3"]["pnl"] == 0.0
    assert row["ev_block_pre1"]["blocked"] is False


def test_event_shadow_only_pre3_allows_near_event() -> None:
    runner = cb._ShadowGuardRunner()
    ctx = {
        "date": "2021-01-25",
        "ticker": "USDJPY",
        "timeframe": "1d",
        "strategy_id": "T01",
        "confidence": "MEDIUM",
        "macro_bias": "NEUTRAL",
        "baseline_pnl": 100.0,
        "ab_throttle": 1.0,
        "pre_ab_risk_pct": 0.01,
        "pre_ab_max_risk": 100.0,
        "max_risk_dollars": 100.0,
        "outcome": "WIN",
    }
    near = runner.process_trade(ctx)
    assert near["ev_only_pre3"]["blocked"] is False
    ctx["date"] = "2021-02-16"
    far = runner.process_trade(ctx)
    assert far["ev_only_pre3"]["blocked"] is True


def test_split_shadow_trade_maps_includes_event() -> None:
    runner = cb._ShadowGuardRunner()
    maps = runner.process_trade(
        {
            "date": "2024-06-01",
            "ticker": "EURUSD",
            "timeframe": "1d",
            "strategy_id": "T02",
            "confidence": "LOW",
            "macro_bias": "NEUTRAL",
            "baseline_pnl": 50.0,
            "ab_throttle": 1.0,
            "pre_ab_risk_pct": 0.005,
            "pre_ab_max_risk": 50.0,
            "max_risk_dollars": 50.0,
            "outcome": "LOSS",
        }
    )
    guard, compound, event = cb._split_shadow_trade_maps(maps)
    assert len(guard) == 23
    assert len(event) == len(cb.SHADOW_EVENT_CONFIGS)
    assert len(compound) == len(cb.SHADOW_COMPOUND_RISK_FRACTIONS) * 2


def test_event_summaries() -> None:
    runner = cb._ShadowGuardRunner()
    runner.process_trade(
        {
            "date": "2024-03-01",
            "ticker": "GBPUSD",
            "timeframe": "1d",
            "strategy_id": "T03",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "baseline_pnl": 80.0,
            "ab_throttle": 1.0,
            "pre_ab_risk_pct": 0.02,
            "pre_ab_max_risk": 80.0,
            "max_risk_dollars": 80.0,
            "outcome": "WIN",
        }
    )
    summary = runner.event_summaries()["ev_half_pre1"]
    assert summary["trades_taken"] == 1


def test_nfp_first_friday() -> None:
    # Feb 2021 first Friday is 2021-02-05
    evs = cb._events_for_currency("USD", date(2021, 2, 5))
    assert any(e[2] == "NFP" for e in evs)


if __name__ == "__main__":
    test_high_impact_events_cover_range()
    test_events_for_currency_fomc_day()
    test_days_to_next_event_usdjpy()
    test_event_attribution_fields()
    test_event_shadow_block_pre3()
    test_event_shadow_only_pre3_allows_near_event()
    test_split_shadow_trade_maps_includes_event()
    test_event_summaries()
    test_nfp_first_friday()
    print("ok")
