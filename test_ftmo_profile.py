"""ftmo profile: chrono-like confidence sizing + FTMO guardrails."""
from __future__ import annotations

from datetime import date

import apex_trader_v76_private as v76
import continuous_backtester as cb


def test_ftmo_profile_matches_validated_chrono_risk_table() -> None:
    ftmo = v76.PROFILES["ftmo"]
    assert v76.PROFILE == "ftmo"
    assert ftmo["compound_risk_fraction"] is None
    assert ftmo["compounding_enabled"] is False
    assert ftmo["warmup_days"] == 0
    assert ftmo["apply_live_guardrails"] is True
    assert ftmo["daily_loss_stop_pct"] == 4.0
    assert ftmo["dd_ladder"] == [(6.0, 0.5), (8.0, 0.25)]
    assert ftmo["per_trade_loss_cap_pct"] == 2.9
    assert ftmo["hard_risk_ceiling_pct"] == 2.9
    assert ftmo["account_loss_limit_usd"] == 1000.0
    assert ftmo["daily_loss_limit_usd"] == 500.0
    # Validated chrono RISK_BY_CONFIDENCE (not funded 0.2%).
    assert cb.RISK_BY_CONFIDENCE == {"HIGH": 0.020, "MEDIUM": 0.010, "LOW": 0.005}
    assert cb.ACCOUNT_TYPE == "private" or cb.ACCOUNT_TYPE in ("private", "funded")


def test_ftmo_hard_ceiling_clips_above_2_9pct(monkeypatch) -> None:
    monkeypatch.setattr(v76, "PROFILE", "ftmo")
    monkeypatch.setattr(v76, "CFG", v76.PROFILES["ftmo"])
    v76._LIVE_SIZING_HEALTH_REBUILT = True
    balance = 10_000.0
    plan = v76.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id="T03",
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=550.0,  # would be 5.5%
        risk_pct=0.055,
        ai={},
        log_fields={},
    )
    fields = v76.apply_cfg_guardrails(
        plan,
        st={
            "peak_equity_by_profile": {"ftmo": balance},
            "guard_activation_date_by_profile": {"ftmo": "2024-01-02"},
            "ftmo_start_balance": balance,
        },
        equity=balance,
        day_anchor=balance,
        day_realized_pnl=0.0,
        scan_d=date(2024, 6, 1),
    )
    assert plan.risk_usd == 290.0
    assert fields["guard_hard_risk_ceiling_pct"] == 2.9
    assert fields["guard_daily_loss_stop_pct"] == 4.0
    assert fields["guard_dd_ladder_mult"] == 1.0
