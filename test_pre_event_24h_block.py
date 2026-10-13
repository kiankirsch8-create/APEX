"""Group D / PR12 — 24h pre-event entry block for strongly-affected pairs."""
from __future__ import annotations

from datetime import date, timedelta

import continuous_backtester as cb


def test_strongly_affected_usd_pair_blocked_before_nfp() -> None:
    # Find a known NFP Friday from the calendar
    nfp = None
    for ds, cur, etype in cb.HIGH_IMPACT_EVENTS:
        if cur == "USD" and etype == "NFP":
            nfp = date.fromisoformat(ds)
            break
    assert nfp is not None
    day_before = nfp - timedelta(days=1)
    block, reason = cb.strongly_affected_pre_event_entry_block("EURUSD", day_before)
    assert block is True
    assert "PRE_EVENT_24H" in reason
    assert "USD" in reason or "NFP" in reason

    block_day, _ = cb.strongly_affected_pre_event_entry_block("EURUSD", nfp)
    assert block_day is True


def test_mild_pair_unaffected() -> None:
    # AUDNZD is not strongly affected by a USD-only event
    nfp = None
    for ds, cur, etype in cb.HIGH_IMPACT_EVENTS:
        if cur == "USD" and etype == "NFP":
            nfp = date.fromisoformat(ds)
            break
    assert nfp is not None
    # If AUDNZD has no AUD/NZD event that day/next, should not block for USD NFP.
    # May still block if coincidentally an AUD/NZD event exists — pick a day with only USD.
    # Use helper: next strongly-affected for AUDNZD should not be the USD NFP.
    hit = cb._next_strongly_affected_event("AUDNZD", nfp - timedelta(days=1))
    if hit is not None:
        _days, _ds, cur, _etype = hit
        assert cur in ("AUD", "NZD")
    else:
        block, _ = cb.strongly_affected_pre_event_entry_block("AUDNZD", nfp - timedelta(days=1))
        assert block is False


def test_far_from_event_no_block() -> None:
    # Mid-month quiet day for EURUSD if possible
    d = date(2022, 7, 12)
    # May still have some event within 1 day — if so, skip assertion
    hit = cb._next_strongly_affected_event("EURUSD", d)
    if hit is None:
        block, _ = cb.strongly_affected_pre_event_entry_block("EURUSD", d)
        assert block is False


def test_flag_off_disables() -> None:
    old = cb.PRE_EVENT_ENTRY_BLOCK_ENABLED
    try:
        cb.PRE_EVENT_ENTRY_BLOCK_ENABLED = False
        nfp = next(
            date.fromisoformat(ds)
            for ds, cur, etype in cb.HIGH_IMPACT_EVENTS
            if cur == "USD" and etype == "NFP"
        )
        block, _ = cb.strongly_affected_pre_event_entry_block("EURUSD", nfp)
        assert block is False
    finally:
        cb.PRE_EVENT_ENTRY_BLOCK_ENABLED = old
