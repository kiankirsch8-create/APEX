"""Group D / PR8 — no look-ahead when seeding A+B / health histories."""
from __future__ import annotations

from typing import Any

import continuous_backtester as cb


def _row(date: str, sid: str = "S01", outcome: str = "WIN", pnl: float = 10.0, **extra: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "date": date,
        "ticker": "EURUSD",
        "timeframe": "1d",
        "strategy_id": sid,
        "direction": "LONG",
        "outcome": outcome,
        "pnl_dollars": pnl,
        "skipped": False,
        "confidence": "MEDIUM",
        "macro_bias": "STRONG_TAILWIND",
    }
    r.update(extra)
    return r


def test_filter_keeps_only_strictly_before_cutoff() -> None:
    rows = [
        _row("2022-01-01"),
        _row("2022-03-01"),
        _row("2022-03-15"),  # == cutoff → drop
        _row("2025-06-01"),  # future → drop
        {"ticker": "EURUSD", "outcome": "WIN"},  # no date → drop
    ]
    kept = cb.filter_history_rows_before_date(rows, "2022-03-15")
    assert [r["date"] for r in kept] == ["2022-01-01", "2022-03-01"]
    assert all(r["date"] < "2022-03-15" for r in kept)


def test_fresh_start_cutoff_is_start_date() -> None:
    cutoff = cb.chrono_history_seed_cutoff(
        {"daily_pnl": [], "current_date": "2022-01-03", "all_trades": []},
        start_date="2022-01-03",
    )
    # Prefer current_date when present; for brand-new job current_date == start_date.
    assert cutoff == "2022-01-03"
    seeded = cb.filter_history_rows_before_date(
        [_row("2022-01-03"), _row("2025-01-01"), _row("2021-12-31")],
        cutoff,
    )
    assert [r["date"] for r in seeded] == ["2021-12-31"]


def test_resume_cutoff_is_day_after_last_daily_pnl() -> None:
    cutoff = cb.chrono_history_seed_cutoff(
        {
            "daily_pnl": [
                {"date": "2022-03-10", "capital": 10100},
                {"date": "2022-03-11", "capital": 10150},
            ],
            "current_date": "2022-03-11",
        },
        start_date="2022-01-01",
    )
    assert cutoff == "2022-03-12"
    seeded = cb.filter_history_rows_before_date(
        [
            _row("2022-03-11"),
            _row("2022-03-12"),
            _row("2025-01-01"),
        ],
        cutoff,
    )
    assert [r["date"] for r in seeded] == ["2022-03-11"]
    assert all(r["date"] < cutoff for r in seeded)


def test_rebuild_health_ignores_future_when_prefiltered() -> None:
    cb._reset_health_signal_histories()
    cb._reset_st_medium_history()
    past = _row("2022-01-05", sid="T01", pnl=12.0)
    future = _row("2025-08-01", sid="T01", pnl=-99.0)
    kept = cb.filter_history_rows_before_date([past, future], "2022-03-01")
    cb._rebuild_health_signal_histories_from_rows(kept)
    cb._rebuild_st_medium_history_from_rows(kept)
    hist = cb._STRAT_PNL_HISTORY.get("T01") or []
    assert hist == [12.0]
    assert len(cb._ST_MEDIUM_HISTORY) == 1
    assert cb._ST_MEDIUM_HISTORY[0][0] == 12.0
