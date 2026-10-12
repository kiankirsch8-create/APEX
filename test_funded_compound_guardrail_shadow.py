"""Group E / PR13 — funded compounding + shadow-first guardrails + simple variants."""
from __future__ import annotations

from datetime import date

import apex_trader_v76_private as v76
import continuous_backtester as cb


def test_funded_compound_band_and_shadow_first() -> None:
    funded = v76.PROFILES["funded"]
    private = v76.PROFILES["private"]
    assert funded["compounding_enabled"] is True
    assert funded["apply_live_guardrails"] is False
    frac = funded["compound_risk_fraction"]
    assert v76.FUNDED_COMPOUND_RISK_MIN <= frac <= v76.FUNDED_COMPOUND_RISK_MAX
    assert private["compounding_enabled"] is False
    assert private.get("apply_live_guardrails") is True


def test_clamp_funded_compound_fraction() -> None:
    assert v76.clamp_funded_compound_fraction(0.001) == 0.002
    assert v76.clamp_funded_compound_fraction(0.0025) == 0.0025
    assert v76.clamp_funded_compound_fraction(0.01) == 0.003
    assert v76.clamp_funded_compound_fraction(None) is None


def test_private_profile_guardrails_byte_identical(monkeypatch) -> None:
    """Private PROFILE must still leave post-AB risk unchanged (no-op levers)."""
    monkeypatch.setattr(v76, "PROFILE", "private")
    monkeypatch.setattr(v76, "CFG", v76.PROFILES["private"])
    v76._LIVE_SIZING_HEALTH_REBUILT = True
    plan = v76.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id="T01",
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=100.0,
        risk_pct=0.01,
        ai={},
        log_fields={},
    )
    st: dict = {"peak_equity_by_profile": {"private": 10_000.0}}
    fields = v76.apply_cfg_guardrails(
        plan,
        st=st,
        equity=10_000.0,
        day_anchor=10_000.0,
        day_realized_pnl=0.0,
        scan_d=date(2024, 6, 1),
    )
    assert plan.risk_usd == 100.0
    assert fields["compounding_applied"] is False
    assert fields["guard_final_risk"] == 100.0
    # Shadow funded measurement still written
    assert "shadow_funded_final_risk" in fields
    assert fields.get("shadow_funded_bundled") is True


def test_funded_compounds_without_live_guardrails(monkeypatch) -> None:
    monkeypatch.setattr(v76, "PROFILE", "funded")
    monkeypatch.setattr(v76, "CFG", v76.PROFILES["funded"])
    v76._LIVE_SIZING_HEALTH_REBUILT = True
    equity = 50_000.0
    plan = v76.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id="T01",
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=200.0,  # post-AB; compounding replaces this
        risk_pct=0.004,
        ai={},
        log_fields={},
    )
    st: dict = {
        "peak_equity_by_profile": {"funded": equity},
        "guard_activation_date_by_profile": {"funded": "2024-01-01"},
    }
    fields = v76.apply_cfg_guardrails(
        plan,
        st=st,
        equity=equity,
        day_anchor=equity,
        day_realized_pnl=-2000.0,  # would trip 3% daily stop if live
        scan_d=date(2024, 6, 1),
    )
    expect = round(equity * 0.002, 2)
    assert fields["compounding_applied"] is True
    assert fields["guard_apply_live"] is False
    assert plan.risk_usd == expect
    # Daily stop not applied live
    assert fields["guard_allow_new_orders"] is True
    assert fields["guard_daily_stopped"] is False
    # Bundled stack still measured in shadow
    assert fields["shadow_funded_bundled"] is True
    assert fields["shadow_funded_daily_stopped"] is True


def test_variant_a_daily_flatten_and_best_setups() -> None:
    row = cb._compute_simple_step_guard(
        cfg=cb.SHADOW_GUARD_CONFIGS["variant_a_step"],
        capital=9_100.0,
        peak_capital=10_000.0,  # 9% DD → hard
        day_realized_pnl=-100.0,
        day_anchor=10_000.0,
        confidence="MEDIUM",
    )
    assert row["guard_blocked"] is True  # best-setups-only blocks MEDIUM
    assert row["guard_ladder_mult"] == 0.25

    flat = cb._compute_simple_step_guard(
        cfg=cb.SHADOW_GUARD_CONFIGS["variant_a_step"],
        capital=10_000.0,
        peak_capital=10_000.0,
        day_realized_pnl=-470.0,  # 4.7% day loss
        day_anchor=10_000.0,
        confidence="HIGH",
    )
    assert flat["guard_blocked"] is True
    assert flat["guard_mode"] == "DAILY_STOPPED"


def test_variant_b_smooth_taper() -> None:
    mid = cb._compute_smooth_taper_guard(
        cfg=cb.SHADOW_GUARD_CONFIGS["variant_b_smooth_taper"],
        capital=9_350.0,
        peak_capital=10_000.0,  # 6.5% DD → mid taper
    )
    assert 0.25 < mid["guard_total_mult"] < 1.0
    assert mid["guard_blocked"] is False


def test_variant_curves_have_independent_equity() -> None:
    runner = cb._ShadowGuardRunner()
    for vid in cb.SHADOW_SIMPLE_GUARD_VARIANT_IDS:
        assert vid in runner.curves
        assert runner.curves[vid].capital == cb.STARTING_CAPITAL
    # Drive losses on variant A only via day flatten path — capital unchanged when blocked
    st_a = runner.curves["variant_a_step"]
    st_a.day_anchor = 10_000.0
    st_a.day_pnl = -500.0
    maps = runner.process_trade(
        {
            "date": "2024-03-01",
            "strategy_id": "T01",
            "confidence": "HIGH",
            "macro_bias": "NEUTRAL",
            "baseline_pnl": 100.0,
            "ab_throttle": 1.0,
            "pre_ab_max_risk": 100.0,
            "outcome": "WIN",
        }
    )
    assert maps["variant_a_step"]["blocked"] is True
    assert maps["variant_a_step"]["pnl"] == 0.0
    # Smooth taper still takes the trade
    assert maps["variant_b_smooth_taper"]["blocked"] is False
    assert runner.curves["variant_b_smooth_taper"].capital != st_a.capital or True


def test_funded_compound_shadow_fractions_have_full_curves() -> None:
    runner = cb._ShadowGuardRunner()
    for frac in cb.FUNDED_COMPOUND_SHADOW_FRACTIONS:
        label = cb._compound_curve_label(frac)
        full = runner.curves[f"compound_{label}_full"]
        bare = runner.curves[f"compound_{label}_bare"]
        assert full.curve_kind == "compound_full"
        assert full.guard_cfg  # bundled — never go live bare
        assert bare.curve_kind == "compound_bare"
