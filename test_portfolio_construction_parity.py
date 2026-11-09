"""
PORTFOLIO-parity: live construction path opens the same book the backtester would.

Feeds one scan-day of multi-signal decisions into:
  - portfolio_construction.filter_portfolio_entries (production v7.0 rules —
    the body of chrono TRADED_TICKERS_TODAY + _chrono_currency_cap_blocks)
  - the live engine helpers (portfolio_construction_blocks + record_portfolio_entry)

Asserts the SET of positions actually opened is identical.

Also reproduces the FTMO demo incident: AUDJPY/CADJPY/CHFJPY/GBPJPY shorts +
USDCHF/EURCHF longs across 1w/1d/4h — before = 18 stacked; after = capped subset.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import apex_trader as at
import apex_trader_v76_private as v76
import continuous_backtester as cb
import portfolio_construction as pc


def _incident_decisions() -> list[dict]:
    """Correlated JPY shorts + CHF longs across 1w/1d/4h (incident shape)."""
    rows: list[dict] = []
    shorts = ("AUDJPY", "CADJPY", "CHFJPY", "GBPJPY")
    longs = ("USDCHF", "EURCHF")
    for tf in ("1w", "1d", "4h"):
        for tkr in shorts:
            rows.append(
                {
                    "ticker": tkr,
                    "timeframe": tf,
                    "direction": "SHORT",
                    "enter": True,
                }
            )
        for tkr in longs:
            rows.append(
                {
                    "ticker": tkr,
                    "timeframe": tf,
                    "direction": "LONG",
                    "enter": True,
                }
            )
    return rows


def _chrono_ordered(decisions: list[dict]) -> list[dict]:
    return pc.sort_decisions_chrono_order(
        decisions,
        ticker_order=list(cb.CHRONO_TICKERS),
    )


# Incident accepted set under v7.0 rules (chrono phase order):
# 1w major: USDCHF, EURCHF (CHF=2), GBPJPY, AUDJPY (JPY=2);
#           CADJPY/CHFJPY currency-blocked (NOT marked traded-today).
# 1d major: all remaining blocked by ticker-day or currency cap.
# 4h pool:  CADJPY + CHFJPY open (separate pool; not yet traded today).
INCIDENT_ACCEPTED = {
    ("USDCHF", "1w"),
    ("EURCHF", "1w"),
    ("GBPJPY", "1w"),
    ("AUDJPY", "1w"),
    ("CADJPY", "4h"),
    ("CHFJPY", "4h"),
}


def test_incident_before_vs_after_counts() -> None:
    """Without rules → 18; with v7.0 rules → 6 (chrono-priority capped subset)."""
    raw = _incident_decisions()
    assert len(raw) == 18  # 6 tickers × 3 TFs — the over-concentrated book

    ordered = _chrono_ordered(raw)
    result = pc.filter_portfolio_entries(ordered)
    assert result.accepted_keys == INCIDENT_ACCEPTED
    assert len(result.accepted) == 6
    assert len(result.rejected) == 12


def test_backtester_and_live_construction_identical_on_incident() -> None:
    """Same ordered decisions → identical accepted set via shared rules + live helpers."""
    ordered = _chrono_ordered(_incident_decisions())

    # Backtester construction path (production semantics — not the diagnostic bypass).
    bt_book = pc.PortfolioBookState()
    bt = pc.filter_portfolio_entries(ordered, book=bt_book)

    # Live construction path — same helpers the scan loop calls.
    live_book = pc.PortfolioBookState()
    live_accepted: list[dict] = []
    live_rejected: list[dict] = []
    for row in ordered:
        reason = v76.portfolio_construction_blocks(
            row["ticker"], row["timeframe"], live_book
        )
        if reason:
            live_rejected.append({**row, "skip_reason": reason, "accepted": False})
            continue
        v76.record_portfolio_entry(live_book, row["ticker"], row["timeframe"])
        live_accepted.append({**row, "accepted": True})

    bt_keys = {
        (r["ticker"], r["timeframe"]) for r in bt.accepted
    }
    live_keys = {
        (str(r["ticker"]).upper(), str(r["timeframe"]).lower()) for r in live_accepted
    }
    assert bt_keys == live_keys == INCIDENT_ACCEPTED
    assert live_book.traded_tickers_today == bt_book.traded_tickers_today
    assert live_book.open_currency_count.get("JPY") == 2
    assert live_book.open_currency_count.get("CHF") == 2
    assert live_book.open_currency_count_4h.get("JPY") == 2


def test_ticker_day_dedupe_across_timeframes() -> None:
    decisions = [
        {"ticker": "EURUSD", "timeframe": "1w", "direction": "LONG"},
        {"ticker": "EURUSD", "timeframe": "1d", "direction": "LONG"},
        {"ticker": "EURUSD", "timeframe": "4h", "direction": "LONG"},
    ]
    result = pc.filter_portfolio_entries(decisions)
    assert result.accepted_keys == {("EURUSD", "1w")}
    assert all(
        r["skip_reason"] == pc.SKIP_TICKER_TRADED_TODAY for r in result.rejected
    )


def test_currency_cap_separate_4h_pool() -> None:
    """Major pool full on JPY does not block a fresh 4h JPY entry (separate pool).

    Without ticker-day dedupe (already-traded would otherwise block), only the
    currency pools matter — mirrors chrono when multi-TF-per-day is allowed but
    currency caps remain.
    """
    decisions = [
        {"ticker": "AUDJPY", "timeframe": "1w", "direction": "SHORT"},
        {"ticker": "CADJPY", "timeframe": "1w", "direction": "SHORT"},
        {"ticker": "CHFJPY", "timeframe": "1w", "direction": "SHORT"},  # blocked
        {"ticker": "GBPJPY", "timeframe": "4h", "direction": "SHORT"},  # 4h pool OK
    ]
    result = pc.filter_portfolio_entries(
        decisions,
        enforce_ticker_day_dedupe=False,
    )
    assert ("AUDJPY", "1w") in result.accepted_keys
    assert ("CADJPY", "1w") in result.accepted_keys
    assert ("CHFJPY", "1w") not in result.accepted_keys
    assert ("GBPJPY", "4h") in result.accepted_keys
    assert any("Currency cap" in str(r.get("skip_reason", "")) for r in result.rejected)


def test_exotic_usd_exempt_from_currency_cap() -> None:
    """FIX 15: USD side of exotic pairs does not consume the USD cap."""
    decisions = [
        {"ticker": "EURUSD", "timeframe": "1w", "direction": "LONG"},
        {"ticker": "GBPUSD", "timeframe": "1w", "direction": "LONG"},
        {"ticker": "USDMXN", "timeframe": "1w", "direction": "LONG"},  # USD exempt
    ]
    result = pc.filter_portfolio_entries(decisions)
    assert ("USDMXN", "1w") in result.accepted_keys


def test_state_persists_ticker_day_and_resets_on_rollover() -> None:
    st = {
        "portfolio_utc_date": "2026-10-28",
        "traded_tickers_today": ["AUDJPY", "USDCHF"],
    }
    # Same UTC day → keep traded set
    book = pc.PortfolioBookState.from_state_dict(
        {**st, "portfolio_utc_date": "2026-10-29", "traded_tickers_today": ["AUDJPY"]},
        utc_today=date(2026, 10, 29),
        open_rows=[],
    )
    assert book.traded_tickers_today == {"AUDJPY"}

    # Rollover → clear
    rolled = pc.PortfolioBookState.from_state_dict(
        st,
        utc_today=date(2026, 10, 29),
        open_rows=[],
    )
    assert rolled.traded_tickers_today == set()
    assert rolled.utc_date == "2026-10-29"


def test_open_positions_seed_currency_pools() -> None:
    """Overnight open swings consume currency slots (live open+today counting)."""
    book = pc.PortfolioBookState.from_state_dict(
        {},
        utc_today=date(2026, 10, 29),
        open_rows=[
            {"ticker": "AUDJPY", "timeframe": "1w"},
            {"ticker": "CADJPY", "timeframe": "1d"},
        ],
    )
    assert book.open_currency_count.get("JPY") == 2
    reason = v76.portfolio_construction_blocks("CHFJPY", "1w", book)
    assert reason is not None
    assert "Currency cap" in reason


def test_open_v76_positions_uses_magic_and_comment(monkeypatch) -> None:
    """Comment-primary match (MCP) with magic fallback (legacy IPC)."""
    positions = [
        SimpleNamespace(magic=760761, comment="APEX76P", ticket=1, symbol="AUDJPYm"),
        SimpleNamespace(magic=760761, comment="APEX", ticket=2, symbol="USDCHFm"),  # magic fallback
        SimpleNamespace(magic=0, comment="APEX76P", ticket=3, symbol="EURUSDm"),  # MCP: comment only
        SimpleNamespace(magic=760761, comment="APEX76P", ticket=4, symbol="CADJPYm"),
        SimpleNamespace(magic=0, comment="OTHER", ticket=5, symbol="NZDUSDm"),  # neither
    ]
    mt5 = MagicMock()
    mt5.positions_get.return_value = positions

    # Direct apex_trader parametrized call — comment OR magic
    found = at.open_apex_positions(mt5, magic=760761, comment="APEX76P")
    assert {int(p.ticket) for p in found} == {1, 2, 3, 4}

    # Live helper
    monkeypatch.setattr(v76, "APEX_V76_MAGIC", 760761)
    monkeypatch.setattr(v76, "ORDER_COMMENT_V76", "APEX76P")
    found2 = v76.open_v76_positions(mt5)
    assert {int(p.ticket) for p in found2} == {1, 2, 3, 4}

    # Legacy default: comment "APEX" OR magic APEX_MAGIC — only ticket 2 (comment APEX)
    # matches via comment; none have APEX_MAGIC.
    legacy = at.open_apex_positions(mt5)
    assert {int(p.ticket) for p in legacy} == {2}


def test_bt_currency_cap_body_matches_shared_helper() -> None:
    """Shared helper mirrors the (currently dead) chrono currency-cap body."""
    # Temporarily exercise production logic via shared module; confirm EXOTIC + cap=2.
    major = {"JPY": 2, "CHF": 1}
    pool4h: dict[str, int] = {}
    blocked, ccy = pc.currency_cap_blocks(
        "CADJPY",
        use_4h_pool=False,
        open_currency_count=major,
        open_currency_count_4h=pool4h,
    )
    assert blocked and ccy == "JPY"
    blocked2, _ = pc.currency_cap_blocks(
        "CADJPY",
        use_4h_pool=True,
        open_currency_count=major,
        open_currency_count_4h=pool4h,
    )
    assert not blocked2  # separate 4h pool empty


def test_get_currencies_matches_backtester() -> None:
    assert pc.get_currencies("AUDJPY") == cb.get_currencies("AUDJPY") == ["AUD", "JPY"]
    assert pc.get_currencies("QQQ") == cb.get_currencies("QQQ") == []
    assert pc.EXOTIC_PAIRS == cb.EXOTIC_PAIRS
