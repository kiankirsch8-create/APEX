"""Crash survival: supervisor restart config, adopt on restart, halt reload."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import apex_supervisor as sup
import apex_trader_v76_private as v76


def test_nssm_and_systemd_restart_always() -> None:
    """Supervisor units must auto-restart within <<60s of exit."""
    bat = Path("install_service.bat").read_text(encoding="utf-8")
    assert "AppExit Default Restart" in bat
    assert "AppThrottle" in bat
    assert "5000" in bat  # 5s throttle

    bat_p = Path("install_service_private.bat").read_text(encoding="utf-8")
    assert "AppExit Default Restart" in bat_p
    assert "AppThrottle" in bat_p
    assert "5000" in bat_p

    unit = Path("deploy/apex-trader-v76.service").read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit

    unit_p = Path("deploy/apex-trader-v76-private.service").read_text(encoding="utf-8")
    assert "Restart=always" in unit_p
    assert "RestartSec=5" in unit_p

    assert sup.SUPERVISOR_RESTART_DELAY_SEC <= 60.0
    assert sup.SUPERVISOR_RESTART_MAX_DELAY_SEC <= 60.0


def test_circuit_halt_survives_process_restart(monkeypatch, tmp_path: Path) -> None:
    """Kill/restart must not clear daily halt — state file reload preserves it."""
    state_file = tmp_path / "apex_v76_private_state.json"
    monkeypatch.setattr(v76, "V76_STATE_FILE", state_file)

    scan_d = date(2024, 6, 5)
    halt_until = datetime.combine(scan_d + timedelta(days=1), datetime.min.time())
    st = {"circuit_halt_until": halt_until.isoformat(), "last_balance": 10_000.0}
    v76.save_v76_state(st)

    # Simulate new process: fresh load from disk.
    reloaded = v76.load_v76_state()
    assert str(reloaded.get("circuit_halt_until") or "") == halt_until.isoformat()

    halted, reason = v76._circuit_breaker_live(reloaded, scan_d, 10_000.0, -500.0)
    assert halted is True
    assert "HALTED" in reason


def test_adopt_open_positions_on_restart(monkeypatch, tmp_path: Path) -> None:
    """After crash, startup re-adopts open magic tickets into ticket meta."""
    meta_file = tmp_path / "tickets.json"
    monkeypatch.setattr(v76, "V76_TICKET_META", meta_file)
    # Empty meta = crash lost in-memory ticket state.
    v76.ticket_meta_v76_save({})

    pos = SimpleNamespace(
        ticket=424242,
        magic=v76.APEX_V76_MAGIC,
        symbol="EURUSD",
        type=0,  # LONG
        price_open=1.1000,
        sl=1.0950,
        tp=0.0,
    )

    class _Mt5:
        def terminal_info(self) -> Any:
            return SimpleNamespace(connected=True)

        def positions_get(self) -> list[Any]:
            return [pos]

    ok = v76._adopt_open_positions(_Mt5())
    assert ok is True
    meta = v76.ticket_meta_v76_load()
    assert "424242" in meta
    assert meta["424242"]["adopted_on_startup"] is True
    assert meta["424242"]["symbol"] == "EURUSD"


def test_adopt_retries_when_terminal_not_ready() -> None:
    class _Mt5Down:
        def terminal_info(self) -> Any:
            return None

        def positions_get(self) -> list[Any]:
            raise AssertionError("should not be called")

    assert v76._adopt_open_positions(_Mt5Down()) is False
    assert v76._adopt_open_positions(None) is False


def test_supervised_restarts_after_crash(monkeypatch) -> None:
    """In-process supervisor re-enters main after an unexpected exception."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def boom_then_ok() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return  # clean exit on second entry

    monkeypatch.setattr(sup.time, "sleep", lambda s: sleeps.append(float(s)))
    logs: list[str] = []

    def _log(level: str, msg: str, **_kw: Any) -> None:
        logs.append(f"{level}:{msg}")

    t0 = __import__("time").perf_counter()
    sup.run_supervised(
        boom_then_ok,
        log_fn=_log,
        restart_delay_sec=0.01,
        max_delay_sec=0.05,
    )
    elapsed = __import__("time").perf_counter() - t0
    assert calls["n"] == 2
    assert sleeps and sleeps[0] <= 60.0
    assert elapsed < 60.0
    assert any("crash" in x.lower() for x in logs)
