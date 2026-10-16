"""FTMO profile: daily −4% → circuit_halt_until; DD ladder −6%/−8% sizing."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import apex_trader_v76_private as v76


def _ftmo(monkeypatch) -> None:
    monkeypatch.setattr(v76, "PROFILE", "ftmo")
    monkeypatch.setattr(v76, "CFG", v76.PROFILES["ftmo"])
    v76._LIVE_SIZING_HEALTH_REBUILT = True


def _plan(risk_usd: float = 200.0) -> v76.TradePlan:
    return v76.TradePlan(
        sym="EURUSD",
        timeframe="1d",
        strategy_id="T01",
        direction="LONG",
        entry=1.1,
        stop_loss=1.095,
        risk_usd=risk_usd,
        risk_pct=0.02,
        ai={},
        log_fields={},
    )


def test_ftmo_profile_levers() -> None:
    ftmo = v76.PROFILES["ftmo"]
    assert v76.PROFILE == "ftmo"
    assert ftmo["daily_loss_stop_pct"] == 4.0
    assert ftmo["dd_ladder"] == [(6.0, 0.5), (8.0, 0.25)]
    assert ftmo["apply_live_guardrails"] is True


def test_next_trading_day_skips_weekend() -> None:
    assert v76._next_trading_day(date(2024, 6, 7)) == date(2024, 6, 10)  # Fri → Mon
    assert v76._next_trading_day(date(2024, 6, 5)) == date(2024, 6, 6)  # Wed → Thu
    until = v76.daily_loss_halt_until(date(2024, 6, 7))
    assert until == datetime(2024, 6, 10, 0, 0, 0)


def test_daily_loss_minus_4pct_sets_circuit_halt(monkeypatch, tmp_path: Path) -> None:
    """Force a −4% day → circuit_halt_until; new entries blocked; no flatten."""
    _ftmo(monkeypatch)
    monkeypatch.setattr(v76, "V76_STATE_FILE", tmp_path / "state.json")
    scan_d = date(2024, 6, 5)  # Wednesday
    anchor = 10_000.0
    day_pnl = -400.0  # exactly −4%
    st: dict = {
        "peak_equity_by_profile": {"ftmo": anchor},
        "day_realized_pnl": day_pnl,
    }
    assert v76._daily_loss_stop_tripped(day_pnl=day_pnl, day_anchor=anchor) is True

    fields = v76.apply_cfg_guardrails(
        _plan(),
        st=st,
        equity=anchor + day_pnl,
        day_anchor=anchor,
        day_realized_pnl=day_pnl,
        scan_d=scan_d,
    )
    assert fields["guard_daily_stopped"] is True
    assert fields["guard_allow_new_orders"] is False
    assert fields["guard_mode"] == "DAILY_STOPPED"
    halt = v76._parse_halt_until(str(st.get("circuit_halt_until") or ""))
    assert halt is not None
    assert halt == v76.daily_loss_halt_until(scan_d)
    # Same-day CB still blocked; next trading day clears.
    halted, _ = v76._circuit_breaker_live(st, scan_d, anchor, day_pnl)
    assert halted is True
    next_d = v76._next_trading_day(scan_d)
    halted_next, _ = v76._circuit_breaker_live(st, next_d, anchor, 0.0)
    assert halted_next is False
    assert not st.get("circuit_halt_until")


def test_circuit_halt_persists_across_reload(monkeypatch, tmp_path: Path) -> None:
    """circuit_halt_until survives process restart via state file reload."""
    _ftmo(monkeypatch)
    state_file = tmp_path / "apex_v76_private_state.json"
    monkeypatch.setattr(v76, "V76_STATE_FILE", state_file)
    scan_d = date(2024, 6, 5)
    st: dict = {}
    until = v76.set_daily_loss_circuit_halt(st, scan_d, persist=True)
    assert state_file.is_file()

    reloaded = v76.load_v76_state()
    assert str(reloaded.get("circuit_halt_until") or "") == until.isoformat()
    halted, reason = v76._circuit_breaker_live(reloaded, scan_d, 10_000.0, -500.0)
    assert halted is True
    assert "HALTED" in reason


def test_dd_ladder_6pct_halves_new_trade_size(monkeypatch, tmp_path: Path) -> None:
    """Force −6% equity DD → dd_ladder 0.5× on new-trade sizing."""
    _ftmo(monkeypatch)
    monkeypatch.setattr(v76, "V76_STATE_FILE", tmp_path / "state.json")
    peak = 10_000.0
    equity = 9_400.0  # −6% from peak
    post_ab = 200.0
    st: dict = {"peak_equity_by_profile": {"ftmo": peak}}
    plan = _plan(risk_usd=post_ab)
    fields = v76.apply_cfg_guardrails(
        plan,
        st=st,
        equity=equity,
        day_anchor=equity,
        day_realized_pnl=0.0,
        scan_d=date(2024, 6, 5),
    )
    assert fields["guard_dd_pct"] >= 6.0
    assert fields["guard_dd_ladder_mult"] == 0.5
    assert fields["guard_dd_ladder_level"] == 1
    assert fields["guard_mode"] == "LADDER_1"
    assert fields["guard_allow_new_orders"] is True  # size down, do not halt
    # 200 × 0.5 = 100; per_trade_loss_cap 2.9% of 9400 may also bind — take min.
    cap = equity * float(v76.CFG["per_trade_loss_cap_pct"]) / 100.0
    expect = round(min(post_ab * 0.5, cap), 2)
    assert plan.risk_usd == expect
    assert fields["guard_final_risk"] == expect


def test_dd_ladder_8pct_quarter_size(monkeypatch, tmp_path: Path) -> None:
    _ftmo(monkeypatch)
    monkeypatch.setattr(v76, "V76_STATE_FILE", tmp_path / "state.json")
    peak = 10_000.0
    equity = 9_200.0  # −8%
    post_ab = 200.0
    st: dict = {"peak_equity_by_profile": {"ftmo": peak}}
    plan = _plan(risk_usd=post_ab)
    fields = v76.apply_cfg_guardrails(
        plan,
        st=st,
        equity=equity,
        day_anchor=equity,
        day_realized_pnl=0.0,
        scan_d=date(2024, 6, 5),
    )
    assert fields["guard_dd_ladder_mult"] == 0.25
    assert fields["guard_dd_ladder_level"] == 2
    assert fields["guard_mode"] == "LADDER_2"
    cap = equity * float(v76.CFG["per_trade_loss_cap_pct"]) / 100.0
    expect = round(min(post_ab * 0.25, cap), 2)
    assert plan.risk_usd == expect


def test_daily_halt_does_not_shorten_longer_cb_halt(monkeypatch) -> None:
    _ftmo(monkeypatch)
    scan_d = date(2024, 6, 5)
    far = datetime.combine(scan_d + timedelta(days=3), datetime.min.time())
    st = {"circuit_halt_until": far.isoformat()}
    out = v76.set_daily_loss_circuit_halt(st, scan_d, persist=False)
    assert out == far
    assert st["circuit_halt_until"] == far.isoformat()
