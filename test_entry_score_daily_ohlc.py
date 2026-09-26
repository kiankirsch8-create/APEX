"""Regression tests for entry-score daily OHLC cache in continuous_backtester."""
from __future__ import annotations

from datetime import date

import pandas as pd

import continuous_backtester as cb


def _frame_ending(end: date, n: int = 40, start_px: float = 1.10) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=n)
    closes = [start_px + i * 0.001 for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.001 for c in closes],
            "Low": [c * 0.999 for c in closes],
            "Close": closes,
        },
        index=idx,
    )


def test_entry_score_get_daily_ohlc_rejects_stale_frame(monkeypatch) -> None:
    """
    A store frame ending 2024-01-31 must not be returned for as_of 2026-05-01.

    Seeds the module store with an early frame; download returns None so refresh
    cannot extend coverage — function must return None, never the stale slice.
    """
    cb._reset_entry_score_daily_cache()
    sym = "EURUSD"
    stale_end = date(2024, 1, 31)
    as_of = date(2026, 5, 1)
    stale = _frame_ending(stale_end, n=50, start_px=1.08)
    assert stale.index.max().date() == stale_end

    with cb._ENTRY_SCORE_DAILY_LOCK:
        cb._ENTRY_SCORE_DAILY_OHLC[sym] = (stale, stale_end)

    # No chrono peek, no successful re-download.
    monkeypatch.setattr(cb, "_peek_chrono_daily_past_for_zone", lambda *a, **k: None)
    fetch_calls: list[tuple[str, str, str, str]] = []

    def _fake_fetch(yf_ticker, start, end, interval, retries=3):  # noqa: ARG001
        fetch_calls.append((yf_ticker, start, end, interval))
        return None

    monkeypatch.setattr(cb, "safe_yf_fetch", _fake_fetch)

    out = cb._entry_score_get_daily_ohlc(sym, as_of)

    assert out is None, "stale early-run frame must not be returned for a later as_of"
    assert len(fetch_calls) == 1, "must attempt exactly one re-download"
    assert fetch_calls[0][2] == date(2026, 5, 2).isoformat()

    # Marked unavailable — second call must not re-download.
    out2 = cb._entry_score_get_daily_ohlc(sym, as_of)
    assert out2 is None
    assert len(fetch_calls) == 1

    assert (sym, as_of) in cb._ENTRY_SCORE_OHLC_UNAVAILABLE
    assert cb._entry_score_stale_or_missing_pct() == 100.0

    summary = cb._build_entry_scores_summary([])
    assert "entry_score_stale_or_missing_pct" in summary
    assert summary["entry_score_stale_or_missing_pct"] == 100.0


def test_entry_score_get_daily_ohlc_refresh_replaces_stale(monkeypatch) -> None:
    """Re-download that reaches as_of replaces the store and returns fresh bars."""
    cb._reset_entry_score_daily_cache()
    sym = "GBPUSD"
    stale_end = date(2024, 1, 31)
    as_of = date(2026, 5, 1)
    stale = _frame_ending(stale_end, n=50, start_px=1.25)
    fresh = _frame_ending(as_of, n=60, start_px=1.30)

    with cb._ENTRY_SCORE_DAILY_LOCK:
        cb._ENTRY_SCORE_DAILY_OHLC[sym] = (stale, stale_end)

    monkeypatch.setattr(cb, "_peek_chrono_daily_past_for_zone", lambda *a, **k: None)
    monkeypatch.setattr(cb, "safe_yf_fetch", lambda *a, **k: fresh.copy())

    out = cb._entry_score_get_daily_ohlc(sym, as_of)
    assert out is not None
    assert out.index.max().date() == as_of
    assert out.index.max().date() != stale_end
    # Must not be the stale Close path (fresh starts at 1.30).
    assert float(out["Close"].iloc[-1]) >= 1.30

    with cb._ENTRY_SCORE_DAILY_LOCK:
        stored_frame, stored_last = cb._ENTRY_SCORE_DAILY_OHLC[sym]
    assert stored_last == as_of
    assert stored_frame.index.max().date() == as_of
    assert cb._entry_score_stale_or_missing_pct() == 0.0
