"""FTMO demo gate: challenge login blocked until clean demo unlock."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import ftmo_demo_gate as gate
import apex_trader_v76_private as v76


def test_profile_is_ftmo() -> None:
    assert v76.PROFILE == "ftmo"
    assert v76.CFG["daily_loss_stop_pct"] == 4.0
    assert v76.CFG["dd_ladder"] == [(6.0, 0.5), (8.0, 0.25)]
    assert v76.CFG["apply_live_guardrails"] is True


def test_challenge_login_blocked_without_unlock(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gate, "_DEFAULT_UNLOCK", tmp_path / "no_unlock.json")
    monkeypatch.setenv("APEX_FTMO_DEMO_UNLOCK_FILE", str(tmp_path / "no_unlock.json"))
    # Clear any allow flag
    monkeypatch.delenv("APEX_FTMO_ALLOW_LIVE", raising=False)
    try:
        gate.assert_login_allowed(gate.FTMO_CHALLENGE_LOGIN)
        raise AssertionError("expected PermissionError")
    except PermissionError as e:
        assert "511443540" in str(e)
        assert "blocked" in str(e).lower() or "clean" in str(e).lower()


def test_non_challenge_login_allowed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APEX_FTMO_DEMO_UNLOCK_FILE", str(tmp_path / "x.json"))
    gate.assert_login_allowed(999001234)  # demo login — must not raise


def test_acceptance_requires_days_and_guardrails(monkeypatch, tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    unlock = tmp_path / "unlock.json"
    monkeypatch.setenv("APEX_FTMO_DEMO_STATUS_FILE", str(status))
    monkeypatch.setenv("APEX_FTMO_DEMO_UNLOCK_FILE", str(unlock))

    ok, reasons = gate.demo_acceptance()
    assert ok is False
    assert any("trading days" in r for r in reasons)

    # Seed 3 days inside envelope + all guardrail events + SL entries
    for i in range(3):
        gate.record_trading_day(
            date(2024, 6, 3 + i),
            day_pnl_pct=-1.5,
            total_dd_pct=3.0,
        )
    for kind in gate.REQUIRED_GUARDRAIL_EVENTS:
        if kind == "entry_with_sl":
            continue
        gate.record_guardrail_event(kind, detail="forced")
    gate.record_guardrail_event("entry_with_sl", detail="ticket=1")

    ok2, reasons2 = gate.demo_acceptance()
    assert ok2 is True, reasons2
    marked, _ = gate.mark_demo_clean()
    assert marked is True
    assert unlock.is_file()

    monkeypatch.setenv("APEX_FTMO_ALLOW_LIVE", "1")
    gate.assert_login_allowed(gate.FTMO_CHALLENGE_LOGIN)  # unlocked


def test_breach_fails_acceptance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APEX_FTMO_DEMO_STATUS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("APEX_FTMO_DEMO_UNLOCK_FILE", str(tmp_path / "u.json"))
    for i in range(3):
        gate.record_trading_day(date(2024, 6, 3 + i), day_pnl_pct=-1.0, total_dd_pct=2.0)
    gate.record_trading_day(date(2024, 6, 6), day_pnl_pct=-5.5, total_dd_pct=6.0)
    for kind in gate.REQUIRED_GUARDRAIL_EVENTS:
        gate.record_guardrail_event(kind, detail="x")
    ok, reasons = gate.demo_acceptance()
    assert ok is False
    assert any("breach" in r.lower() or "day" in r.lower() for r in reasons)
    marked, _ = gate.mark_demo_clean()
    assert marked is False


def test_allow_live_env_alone_insufficient(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APEX_FTMO_ALLOW_LIVE", "1")
    monkeypatch.setenv("APEX_FTMO_DEMO_UNLOCK_FILE", str(tmp_path / "missing.json"))
    try:
        gate.assert_login_allowed(511443540)
        raise AssertionError("expected PermissionError")
    except PermissionError:
        pass
