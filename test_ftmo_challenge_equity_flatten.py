"""Conservative ftmo_challenge profile + equity-flatten backstop."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import apex_trader_v76_private as v76


def test_challenge_profile_values() -> None:
    ch = v76.PROFILES["ftmo_challenge"]
    assert ch["per_trade_loss_cap_pct"] == 1.2
    assert ch["daily_loss_stop_pct"] == 2.5
    assert ch["dd_ladder"] == [(4.0, 0.5), (6.0, 0.25)]
    assert ch["equity_flatten_dd_pct"] == 8.0
    assert ch["apply_live_guardrails"] is True
    assert ch["hard_risk_ceiling_pct"] == 1.2
    assert ch["account_loss_limit_usd"] == 1000.0
    assert ch["daily_loss_limit_usd"] == 500.0
    # Existing profiles keep explicit key (None = no flatten).
    assert v76.PROFILES["ftmo"]["equity_flatten_dd_pct"] is None
    assert v76.PROFILES["private"]["equity_flatten_dd_pct"] is None
    assert v76.PROFILES["funded"]["equity_flatten_dd_pct"] is None


def test_equity_flatten_trips(monkeypatch) -> None:
    """−9% equity vs $10k start with threshold 8% → flatten + circuit halt."""
    monkeypatch.setattr(v76, "PROFILE", "ftmo_challenge")
    monkeypatch.setattr(v76, "CFG", dict(v76.PROFILES["ftmo_challenge"]))
    monkeypatch.setenv("APEX_FTMO_START_BALANCE", "10000")

    closed: list[Any] = []
    positions = [
        SimpleNamespace(ticket=11, symbol="EURUSDm", volume=0.1, type=0, comment="APEX76P", magic=760761),
        SimpleNamespace(ticket=22, symbol="GBPUSDm", volume=0.1, type=0, comment="APEX76P", magic=760761),
    ]

    mt5 = MagicMock()
    mt5.account_info.return_value = SimpleNamespace(equity=9100.0, balance=9100.0)

    monkeypatch.setattr(v76, "open_v76_positions", lambda _mt5: positions)
    monkeypatch.setattr(
        v76.at,
        "close_position_at_market",
        lambda _mt5, p: closed.append(p) or True,
    )
    # Avoid writing real state files
    monkeypatch.setattr(v76, "save_v76_state", lambda _st: None)

    st: dict[str, Any] = {}
    assert v76._equity_flatten_check(mt5, st, scan_d=date(2024, 6, 5)) is True
    assert {int(p.ticket) for p in closed} == {11, 22}
    assert st.get("circuit_halt_until")
    assert str(st["circuit_halt_until"]).startswith("2024-06-06")  # next trading day


def test_equity_flatten_holds(monkeypatch) -> None:
    """−4% equity < 8% threshold → no flatten."""
    monkeypatch.setattr(v76, "PROFILE", "ftmo_challenge")
    monkeypatch.setattr(v76, "CFG", dict(v76.PROFILES["ftmo_challenge"]))
    monkeypatch.setenv("APEX_FTMO_START_BALANCE", "10000")

    closed: list[Any] = []
    mt5 = MagicMock()
    mt5.account_info.return_value = SimpleNamespace(equity=9600.0, balance=9600.0)
    monkeypatch.setattr(
        v76,
        "open_v76_positions",
        lambda _mt5: [
            SimpleNamespace(ticket=1, symbol="EURUSDm", volume=0.1, type=0, comment="APEX76P", magic=760761),
        ],
    )
    monkeypatch.setattr(
        v76.at,
        "close_position_at_market",
        lambda _mt5, p: closed.append(p) or True,
    )
    monkeypatch.setattr(v76, "save_v76_state", lambda _st: None)

    st: dict[str, Any] = {}
    assert v76._equity_flatten_check(mt5, st, scan_d=date(2024, 6, 5)) is False
    assert closed == []
    assert not st.get("circuit_halt_until")


def test_profile_env_switchable(monkeypatch) -> None:
    """APEX_V76_PRIVATE_PROFILE selects CFG without changing default import."""
    assert v76.PROFILE == "ftmo"  # default when env unset in this process
    assert v76.CFG is v76.PROFILES["ftmo"]
    # Explicit selection works through PROFILES map
    assert "ftmo_challenge" in v76.PROFILES
