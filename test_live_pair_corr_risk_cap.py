"""
Demo: EURUSD-long on 1w / 1d / 4h in one cycle — combined risk capped at 3%.

Mirrors continuous_backtester pcap_3pct / pcap_3pct_corr for the live engine.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import apex_trader_v76_private as v76
import continuous_backtester as cb


def _plan(tf: str, risk: float, *, sym: str = "EURUSD", direction: str = "LONG") -> v76.TradePlan:
    return v76.TradePlan(
        sym=sym,
        timeframe=tf,
        tf_key=tf,
        strategy_id="T03",
        direction=direction,
        entry=1.10,
        stop_loss=1.095,
        tp1=1.11,
        tp2=1.12,
        tp3=1.13,
        risk_usd=float(risk),
        risk_pct=float(risk) / 10_000.0,
        ai={"confidence": "HIGH", "macro_bias": "NEUTRAL", "max_risk_dollars": float(risk)},
        log_fields={"confidence": "HIGH", "macro_bias": "NEUTRAL"},
    )


def test_reuses_backtester_correlation_grouping() -> None:
    assert "EURUSD" in cb.CORRELATION_GROUPS["EUR_CROSSES"]
    assert v76._bt_get_currencies("EURUSD") == cb.get_currencies("EURUSD") == ["EUR", "USD"]
    assert "EUR_CROSSES" in v76._bt_correlation_group_ids("EURUSD")
    assert v76.LIVE_MAX_OPEN_RISK_PCT == 3.0


def test_eurusd_long_1w_1d_4h_combined_capped_at_3pct(monkeypatch) -> None:
    """
    Acceptance: attempt EURUSD-long on 1w, 1d, 4h in one cycle; combined risk
    ≤ 3% of balance; excess skipped or sized down with pair_risk_cap.
    """
    balance = 10_000.0
    cap = balance * 0.03  # $300
    v76.clear_cycle_open_risk_book()
    monkeypatch.setattr(v76, "LIVE_PAIR_RISK_CAP_ENABLED", True)
    monkeypatch.setattr(v76, "LIVE_MAX_OPEN_RISK_PCT", 3.0)
    monkeypatch.setattr(v76, "DRY_RUN", True)
    monkeypatch.setattr(v76, "append_decision_log", lambda row: None)
    monkeypatch.setattr(v76, "live_log", lambda *a, **k: None)
    monkeypatch.setattr(v76, "save_v76_state", lambda s: None)
    monkeypatch.setattr(v76, "load_v76_state", lambda: {
        "last_balance": balance,
        "last_equity": balance,
        "day_anchor": balance,
        "day_realized_pnl": 0.0,
        "peak_equity_by_profile": {"private": balance},
        "guard_activation_date_by_profile": {"private": "2024-01-02"},
    })
    monkeypatch.setattr(v76, "_LIVE_SIZING_HEALTH_REBUILT", True)
    # No MT5 opens — only in-cycle book matters for this demo.
    monkeypatch.setattr(v76, "_open_risk_book_entries", lambda mt5: list(v76._CYCLE_OPEN_RISK_BOOK))

    # Disable A+B / CFG side effects that would change risk.
    monkeypatch.setattr(
        v76,
        "apply_ab_sizing_throttle",
        lambda plan: {
            "sizing_health_throttle": 1.0,
            "ab_skip": False,
            "sizing_health_base_risk": plan.risk_usd,
            "sizing_health_final_risk": plan.risk_usd,
        },
    )
    monkeypatch.setattr(
        v76,
        "apply_cfg_guardrails",
        lambda plan, **kw: {
            "guard_mode": "NORMAL",
            "guard_allow_new_orders": True,
            "guard_final_risk": plan.risk_usd,
            "guard_post_ab_risk": plan.risk_usd,
        },
    )

    mt5 = MagicMock()
    mt5.symbol_info_tick.return_value = SimpleNamespace(ask=1.10, bid=1.0995)
    st = v76.load_v76_state()

    # Each TF wants $200 (2%) — without the cap, 3 × $200 = $600 = 6%.
    results = []
    risks = []
    for tf in ("1w", "1d", "4h"):
        plan = _plan(tf, 200.0)
        res = v76.order_send_v76(
            mt5,
            "EURUSD",
            plan,
            st=st,
            equity=balance,
            day_anchor=balance,
            scan_d=date(2024, 6, 3),
        )
        results.append(res)
        risks.append(float(plan.risk_usd))

    # First takes full $200; second takes remaining $100; third skipped.
    assert results[0].get("ok") is True
    assert risks[0] == 200.0
    assert results[0].get("pair_risk_cap_action") in ("allow", None) or risks[0] <= 200.0

    assert results[1].get("ok") is True
    assert risks[1] == 100.0  # downsized to fill 3% headroom
    assert abs(risks[0] + risks[1] - cap) < 1e-6

    assert results[2].get("ok") is False
    assert results[2].get("error") == "pair_risk_cap"
    assert risks[2] == 0.0

    cycle_total = sum(float(r["risk_usd"]) for r in v76._CYCLE_OPEN_RISK_BOOK)
    assert cycle_total <= cap + 1e-6
    assert cycle_total == 300.0


def test_correlation_group_blocks_shared_eur_stack(monkeypatch) -> None:
    """EURUSD long + EURGBP long share EUR — corr cap binds at 3%."""
    balance = 10_000.0
    v76.clear_cycle_open_risk_book()
    monkeypatch.setattr(v76, "LIVE_PAIR_RISK_CAP_ENABLED", True)
    # Pretend EURUSD long already open for $250.
    v76._CYCLE_OPEN_RISK_BOOK.append(
        {
            "ticker": "EURUSD",
            "direction": "LONG",
            "timeframe": "1d",
            "risk_usd": 250.0,
            "currencies": ["EUR", "USD"],
            "corr_groups": ["EUR_CROSSES"],
            "source": "cycle",
        }
    )
    plan = _plan("1d", 200.0, sym="EURGBP", direction="LONG")
    fields = v76.apply_pair_correlation_risk_cap(plan, balance=balance, mt5=None)
    # Headroom = 300 - 250 = 50 → downsize
    assert fields["pair_risk_cap_action"] == "downsize"
    assert plan.risk_usd == 50.0
    assert fields["pair_corr_open_risk"] == 250.0


def test_pair_cap_skip_when_already_at_limit(monkeypatch) -> None:
    balance = 10_000.0
    v76.clear_cycle_open_risk_book()
    v76._CYCLE_OPEN_RISK_BOOK.append(
        {
            "ticker": "EURUSD",
            "direction": "LONG",
            "timeframe": "1w",
            "risk_usd": 300.0,
            "currencies": ["EUR", "USD"],
            "corr_groups": ["EUR_CROSSES"],
            "source": "cycle",
        }
    )
    plan = _plan("4h", 150.0)
    fields = v76.apply_pair_correlation_risk_cap(plan, balance=balance, mt5=None)
    assert fields["pair_risk_cap_skip"] is True
    assert fields["skip_reason"] == "pair_risk_cap"
    assert plan.risk_usd == 0.0
