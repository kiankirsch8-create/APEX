"""Group D / PR10 — skip_all_throttled on the real path with virtual A+B feed."""
from __future__ import annotations

import continuous_backtester as cb
import apex_trader_v76_private as live


def test_flags_default_on() -> None:
    assert cb.SKIP_ALL_THROTTLED is True
    assert live.SKIP_ALL_THROTTLED is True


def test_apply_ab_sets_skip_keeps_full_size() -> None:
    cb._reset_health_signal_histories()
    cb._reset_st_medium_history()
    sid = "T01_TEST"
    # Seed factor A throttle (last3 sum <= 0)
    for pnl in (-10.0, -5.0, -1.0):
        cb._STRAT_PNL_HISTORY.setdefault(sid, []).append(pnl)
    ai = {
        "_position_size": 1000.0,
        "_leveraged_exposure": 110000.0,
        "_max_risk_dollars": 50.0,
        "_account_risk_pct": 0.01,
    }
    out = cb._apply_ab_throttle_to_ai_sizing(
        ai, strategy_id=sid, confidence="HIGH", macro_bias="NEUTRAL"
    )
    assert out["ab_throttle"] == cb._AB_THROTTLE_FACTOR
    assert out["ab_skip"] is True
    assert ai["_ab_skip"] is True
    # Full size retained for virtual sim
    assert ai["_position_size"] == 1000.0
    assert ai["_max_risk_dollars"] == 50.0


def test_virtual_throttle_feeds_ab_and_releases() -> None:
    cb._reset_health_signal_histories()
    cb._reset_st_medium_history()
    sid = "T02_REL"
    for pnl in (-10.0, -5.0, -1.0):
        cb._STRAT_PNL_HISTORY.setdefault(sid, []).append(pnl)
    _, _, thr0 = cb._compute_ab_throttle_at_open(
        strategy_id=sid, confidence="HIGH", macro_bias="NEUTRAL"
    )
    assert thr0 == cb._AB_THROTTLE_FACTOR

    # Virtual full-size wins feed history → throttle releases after 3 wins
    for _ in range(3):
        cb._record_ab_history_from_virtual_throttle(
            strategy_id=sid,
            confidence="HIGH",
            macro_bias="NEUTRAL",
            outcome="WIN",
            pnl_dollars_full=20.0,
        )
    _, _, thr1 = cb._compute_ab_throttle_at_open(
        strategy_id=sid, confidence="HIGH", macro_bias="NEUTRAL"
    )
    assert thr1 == 1.0


def test_ab_throttle_skip_row_zero_capital() -> None:
    taken = {
        "outcome": "WIN",
        "pnl_dollars": 80.0,
        "max_risk_dollars": 40.0,
        "strategy_id": "T01",
        "skipped": False,
    }
    skip = cb._ab_throttle_skip_row_from_taken(
        taken, skip_reason="[AB_THROTTLE_SKIP] ab_throttle=0.1800 — skip"
    )
    assert skip["skipped"] is True
    assert skip["outcome"] == "SKIPPED"
    assert skip["pnl_dollars"] == 0.0
    assert skip["virtual_outcome"] == "WIN"
    assert skip["virtual_pnl_r"] == 2.0
    assert skip["ab_throttle_skip"] is True


def test_live_apply_ab_skip_keeps_risk() -> None:
    live._reset_live_sizing_health_histories()
    live._LIVE_SIZING_HEALTH_REBUILT = True
    sid = "T01"
    for pnl in (-8.0, -4.0, -2.0):
        live._LIVE_STRAT_PNL_HISTORY.setdefault(sid, []).append(pnl)
    plan = live.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id=sid,
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=100.0,
        risk_pct=0.01,
        ai={"confidence": "HIGH", "macro_bias": "NEUTRAL"},
        log_fields={"confidence": "HIGH", "macro_bias": "NEUTRAL"},
    )
    fields = live.apply_ab_sizing_throttle(plan)
    assert fields["ab_skip"] is True
    assert fields["sizing_health_throttle"] == live._AB_THROTTLE_FACTOR
    assert plan.risk_usd == 100.0  # full size kept
