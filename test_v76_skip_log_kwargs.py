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


def test_scan_cycle_complete_log_overlapping_summary_no_typeerror() -> None:
    """
    summary already has placed/skipped; merging dry_run into one dict then
    unpacking once must not raise (the pre-fix explicit+**summary crash).
    """
    summary = {
        "placed": 2,
        "skipped": 5,
        "cells_checked": 40,
        "cells_signalled": 7,
        "period_mode": "NORMAL",
    }
    complete_fields = dict(summary)
    complete_fields["dry_run"] = True
    with mock.patch.object(v76, "live_log") as mock_log:
        v76.live_log("info", "[SCAN CYCLE] complete", **complete_fields)
    mock_log.assert_called_once_with("info", "[SCAN CYCLE] complete", **complete_fields)
    assert complete_fields["placed"] == 2
    assert complete_fields["skipped"] == 5
    assert complete_fields["dry_run"] is True
    # The broken pattern (explicit kwargs + **summary) must still raise.
    raised = False
    try:
        v76.live_log(
            "info",
            "[SCAN CYCLE] complete",
            placed=summary["placed"],
            skipped=summary["skipped"],
            dry_run=True,
            **summary,
        )
    except TypeError:
        raised = True
    assert raised, "expected TypeError from duplicate placed/skipped kwargs"


if __name__ == "__main__":
    test_skip_blocked_log_with_overlapping_fields_no_typeerror()
    test_build_skip_fields_defaults_guard_mode()
    test_scan_cycle_complete_log_overlapping_summary_no_typeerror()
    print("ok")
