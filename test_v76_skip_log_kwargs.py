"""Smoke test: [SKIP] trade blocked must not raise TypeError on duplicate kwargs."""
from __future__ import annotations

from unittest import mock

import apex_trader_v76_private as v76


def test_skip_blocked_log_with_overlapping_fields_no_typeerror() -> None:
    """
    fields may already carry ticker / timeframe / guard_mode (and more). Building
    one dict then unpacking once must not raise TypeError.
    """
    colliding = {
        "ticker": "OLD",
        "timeframe": "1h",
        "guard_mode": "DAILY_STOPPED",
        "guard_profile": "funded",
        "skip_reason": "stale",
        "strategy_id": "T01",
    }
    with mock.patch.object(v76, "live_log") as mock_log:
        out = v76.emit_skip_blocked_log(
            sym="eurusd",
            timeframe="1d",
            reason="blocked_by_gate",
            fields=colliding,
        )
    mock_log.assert_called_once()
    args, kwargs = mock_log.call_args
    assert args[0] == "info"
    assert args[1] == "[SKIP] trade blocked"
    assert kwargs["ticker"] == "EURUSD"
    assert kwargs["timeframe"] == "1d"
    assert kwargs["skip_reason"] == "blocked_by_gate"
    assert kwargs["guard_mode"] == "DAILY_STOPPED"  # preserved via setdefault
    assert kwargs["strategy_id"] == "T01"
    assert out["ticker"] == "EURUSD"
    # Direct unpack of the built dict must also be safe.
    v76.live_log("info", "[SKIP] trade blocked", **out)


def test_build_skip_fields_defaults_guard_mode() -> None:
    fields = v76.build_skip_blocked_log_fields(
        sym="gbpusd",
        timeframe="4h",
        reason="no_setup",
        fields={"strategy_id": "X"},
    )
    assert fields["guard_mode"] == "NORMAL"
    assert fields["guard_profile"] == v76.PROFILE
    assert fields["ticker"] == "GBPUSD"


if __name__ == "__main__":
    test_skip_blocked_log_with_overlapping_fields_no_typeerror()
    test_build_skip_fields_defaults_guard_mode()
    print("ok")
