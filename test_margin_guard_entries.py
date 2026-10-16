"""Demo/sim: margin guard blocks new entries below 400% ML; resumes when recovered."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import apex_trader_v76_private as v76


def test_margin_guard_constants_are_tunable() -> None:
    assert v76.MARGIN_LEVEL_MIN_PCT == 400.0
    assert v76.FREE_MARGIN_MIN_EQUITY_FRAC == 0.25
    assert v76.MAX_OPEN_POSITIONS == 15


def test_margin_guard_blocks_low_margin_level() -> None:
    blocked, reason = v76.margin_guard_blocks_new_entries(
        margin_level=350.0,
        free_margin=4_000.0,
        equity=10_000.0,
        margin=2_500.0,
    )
    assert blocked is True
    assert "margin_level" in reason


def test_margin_guard_blocks_low_free_margin_fraction() -> None:
    blocked, reason = v76.margin_guard_blocks_new_entries(
        margin_level=800.0,
        free_margin=2_000.0,
        equity=10_000.0,
        margin=1_000.0,
    )
    assert blocked is True
    assert "free_margin" in reason


def test_margin_guard_allows_healthy_book() -> None:
    blocked, reason = v76.margin_guard_blocks_new_entries(
        margin_level=550.0,
        free_margin=6_000.0,
        equity=10_000.0,
        margin=1_800.0,
    )
    assert blocked is False
    assert reason == ""


def test_margin_guard_ignores_zero_margin_level_when_flat() -> None:
    """MT5 reports margin_level=0 with no used margin — must not block entries."""
    blocked, reason = v76.margin_guard_blocks_new_entries(
        margin_level=0.0,
        free_margin=10_000.0,
        equity=10_000.0,
        margin=0.0,
    )
    assert blocked is False
    assert reason == ""


def test_scan_aborts_on_margin_guard_then_resumes(monkeypatch) -> None:
    """
    Many positions open: margin_level < 400% aborts new entries; existing positions
    stay open. After margin recovers, the same gate allows entries again.
    """
    st: dict = {"last_balance": 10_000.0, "last_equity": 10_000.0}
    saved: list[dict] = []
    status_calls: list[dict] = []
    logs: list[tuple] = []

    monkeypatch.setattr(v76, "save_v76_state", lambda s: saved.append(dict(s)))
    monkeypatch.setattr(
        v76,
        "publish_live_status",
        lambda mt5, **kw: status_calls.append(dict(kw)),
    )
    monkeypatch.setattr(
        v76,
        "live_log",
        lambda level, msg, **kw: logs.append((level, msg, kw)),
    )

    open_n = [12]  # under MAX_OPEN_POSITIONS so only margin fires

    class _At:
        APEX_MAGIC = 1

        @staticmethod
        def open_apex_positions(_mt5):
            return [object()] * open_n[0]

        @staticmethod
        def resolve_apex_hedged_same_pair(_mt5):
            return None

    monkeypatch.setattr(v76, "at", _At)
    monkeypatch.setattr(v76, "APEX_V76_MAGIC", 760761)

    # Phase 1 — stressed margin (320%) → abort new entries
    mt5_stressed = MagicMock()
    mt5_stressed.account_info.return_value = SimpleNamespace(
        margin_level=320.0,
        margin_free=3_500.0,
        equity=10_000.0,
        margin=3_125.0,
        balance=10_000.0,
    )
    assert v76.abort_new_entries_if_max_positions_or_margin(mt5_stressed, st) == "margin_guard"
    assert status_calls[-1].get("scan_aborted") == "margin_guard"
    assert any("margin guard" in msg for _, msg, _ in logs)
    assert saved, "state must be saved on abort"
    assert open_n[0] == 12  # positions untouched (no close calls)

    # Phase 2 — recovered margin (520%) → allow entries
    status_calls.clear()
    logs.clear()
    mt5_ok = MagicMock()
    mt5_ok.account_info.return_value = SimpleNamespace(
        margin_level=520.0,
        margin_free=5_000.0,
        equity=10_000.0,
        margin=1_923.0,
        balance=10_000.0,
    )
    assert v76.abort_new_entries_if_max_positions_or_margin(mt5_ok, st) is None
    assert not any(c.get("scan_aborted") == "margin_guard" for c in status_calls)
    assert open_n[0] == 12


def test_max_positions_still_aborts_before_margin(monkeypatch) -> None:
    st: dict = {}
    status_calls: list[dict] = []
    monkeypatch.setattr(v76, "save_v76_state", lambda s: None)
    monkeypatch.setattr(
        v76,
        "publish_live_status",
        lambda mt5, **kw: status_calls.append(dict(kw)),
    )
    monkeypatch.setattr(v76, "live_log", lambda *a, **k: None)

    class _At:
        APEX_MAGIC = 1

        @staticmethod
        def open_apex_positions(_mt5):
            return [object()] * 15

        @staticmethod
        def resolve_apex_hedged_same_pair(_mt5):
            return None

    monkeypatch.setattr(v76, "at", _At)
    mt5 = MagicMock()
    # Would also fail margin, but max_positions wins first.
    mt5.account_info.return_value = SimpleNamespace(
        margin_level=200.0,
        margin_free=100.0,
        equity=10_000.0,
        margin=5_000.0,
    )
    assert v76.abort_new_entries_if_max_positions_or_margin(mt5, st) == "max_positions"
    assert status_calls[-1].get("scan_aborted") == "max_positions"
    mt5.account_info.assert_not_called()


def test_source_wires_margin_guard_after_max_positions() -> None:
    import inspect

    src = inspect.getsource(v76.run_full_scan_v76)
    assert "abort_new_entries_if_max_positions_or_margin" in src
