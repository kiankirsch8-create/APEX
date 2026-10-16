#!/usr/bin/env python3
"""
Re-size 20 sample trades under PROFILE=ftmo and assert risk_usd ≤ 2.9% of balance.

Also verifies daily_loss_stop_pct and dd_ladder are active (not None/[]).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apex_trader_v76_private as v76


def main() -> int:
    # Force ftmo for this script even if the module default drifts.
    v76.PROFILE = "ftmo"
    v76.CFG = v76.PROFILES["ftmo"]
    cfg = v76.CFG

    assert cfg.get("daily_loss_stop_pct") is not None, "daily_loss_stop_pct must be set"
    assert cfg.get("dd_ladder"), "dd_ladder must be non-empty"
    assert float(cfg["hard_risk_ceiling_pct"]) == 2.9
    assert float(cfg["per_trade_loss_cap_pct"]) == 2.9
    assert float(cfg["daily_loss_stop_pct"]) == 4.0
    assert list(cfg["dd_ladder"]) == [(6.0, 0.5), (8.0, 0.25)]
    assert cfg.get("compound_risk_fraction") is None
    assert cfg.get("apply_live_guardrails") is True

    balance = 10_000.0
    # Sample post-decision risks that would breach 2.9% without the ceiling.
    raw_risks = [
        50.0,
        100.0,
        150.0,
        200.0,
        250.0,
        290.0,
        300.0,
        350.0,
        400.0,
        450.0,
        500.0,
        550.0,  # legacy 5.5%
        600.0,
        700.0,
        800.0,  # private SINGLE_TRADE_MAX 8%
        100.0,
        220.0,
        275.0,
        310.0,
        999.0,
    ]
    assert len(raw_risks) == 20

    v76._LIVE_SIZING_HEALTH_REBUILT = True
    ceiling = balance * 0.029
    print(
        f"PROFILE={v76.PROFILE} balance={balance:.0f} hard_ceiling={ceiling:.2f} "
        f"daily_loss_stop_pct={cfg['daily_loss_stop_pct']} dd_ladder={cfg['dd_ladder']}"
    )
    print(f"{'#':>2}  {'raw':>8}  {'final':>8}  {'pct':>7}  ok")

    st: dict = {
        "peak_equity_by_profile": {"ftmo": balance},
        "guard_activation_date_by_profile": {"ftmo": "2024-01-02"},
        "ftmo_start_balance": balance,
        "starting_balance": balance,
    }
    failures = 0
    for i, raw in enumerate(raw_risks, 1):
        plan = v76.TradePlan(
            sym="EURUSD",
            timeframe="1d",
            tf_key="1d",
            strategy_id="T03",
            direction="LONG",
            entry=1.10,
            stop_loss=1.095,
            risk_usd=float(raw),
            risk_pct=float(raw) / balance,
            ai={"confidence": "HIGH", "macro_bias": "NEUTRAL"},
            log_fields={},
        )
        fields = v76.apply_cfg_guardrails(
            plan,
            st=st,
            equity=balance,
            day_anchor=balance,
            day_realized_pnl=0.0,
            scan_d=date(2024, 6, 3),
        )
        final = float(plan.risk_usd)
        pct = final / balance * 100.0
        ok = final <= ceiling + 1e-6
        if not ok:
            failures += 1
        print(f"{i:2d}  {raw:8.2f}  {final:8.2f}  {pct:6.2f}%  {'Y' if ok else 'N'}")
        assert fields["guard_daily_loss_stop_pct"] == 4.0
        assert fields["guard_dd_ladder_mult"] is not None
        assert fields.get("guard_hard_risk_ceiling_pct") == 2.9

    # Daily loss stop active: -4% of day anchor blocks new orders.
    plan_d = v76.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id="T03",
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=100.0,
        risk_pct=0.01,
        ai={},
        log_fields={},
    )
    f_stop = v76.apply_cfg_guardrails(
        plan_d,
        st=st,
        equity=balance,
        day_anchor=balance,
        day_realized_pnl=-401.0,  # > 4% of 10k
        scan_d=date(2024, 6, 3),
    )
    assert f_stop["guard_daily_stopped"] is True
    assert f_stop["guard_allow_new_orders"] is False
    print("daily_loss_stop_pct active: blocked at -4.01% day PnL")

    # Absolute $500 daily limit also blocks.
    plan_d2 = v76.TradePlan(
        sym="GBPUSD",
        timeframe="1d",
        strategy_id="T03",
        direction="LONG",
        entry=1.25,
        stop_loss=1.24,
        risk_usd=100.0,
        risk_pct=0.01,
        ai={},
        log_fields={},
    )
    f500 = v76.apply_cfg_guardrails(
        plan_d2,
        st=st,
        equity=balance,
        day_anchor=balance,
        day_realized_pnl=-500.0,
        scan_d=date(2024, 6, 3),
    )
    assert f500["guard_daily_stopped"] is True
    print("daily_loss_limit_usd=$500 active")

    # DD ladder active at 6% / 8% drawdown from peak.
    st_dd = {
        "peak_equity_by_profile": {"ftmo": 10_000.0},
        "guard_activation_date_by_profile": {"ftmo": "2024-01-02"},
        "ftmo_start_balance": 10_000.0,
    }
    plan_dd = v76.TradePlan(
        sym="USDJPY",
        timeframe="1d",
        strategy_id="T03",
        direction="LONG",
        entry=150.0,
        stop_loss=149.0,
        risk_usd=200.0,
        risk_pct=0.02,
        ai={},
        log_fields={},
    )
    f6 = v76.apply_cfg_guardrails(
        plan_dd,
        st=st_dd,
        equity=9_300.0,  # 7% DD → first ladder rung
        day_anchor=9_300.0,
        day_realized_pnl=0.0,
        scan_d=date(2024, 6, 3),
    )
    assert float(f6["guard_dd_ladder_mult"]) == 0.5, f6
    print(f"dd_ladder active: 7% DD → mult={f6['guard_dd_ladder_mult']}")

    if failures:
        print(f"FAIL — {failures}/20 trades exceeded 2.9% ceiling")
        return 1
    print("PASS — 20/20 trades ≤ 2.9% of balance; guardrails active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
