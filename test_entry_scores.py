"""Unit tests for write-only entry-time scores (no lookahead, no behaviour change)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

import entry_scores as es


def _synth_ohlc(
    n: int = 200,
    start: date = date(2024, 1, 2),
    start_px: float = 1.10,
    drift: float = 0.0002,
    seed: int = 0,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n)
    closes = [start_px]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1.0 + drift + float(rng.normal(0, 0.003))))
    close = np.asarray(closes, dtype=float)
    high = close * (1.0 + rng.uniform(0.0005, 0.004, size=n))
    low = close * (1.0 - rng.uniform(0.0005, 0.004, size=n))
    open_ = close * (1.0 + rng.normal(0, 0.0005, size=n))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close},
        index=idx,
    )


def _universe_frames() -> dict[str, pd.DataFrame]:
    pairs = [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
        "USDCHF",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
    ]
    out: dict[str, pd.DataFrame] = {}
    for i, p in enumerate(pairs):
        base_px = 1.05 + 0.01 * i if not p.startswith("USD") else 110.0 + i
        if p.endswith("JPY") and p.startswith("USD"):
            base_px = 145.0
        elif p.endswith("JPY"):
            base_px = 160.0 + i
        out[p] = _synth_ohlc(start_px=base_px, seed=10 + i, drift=0.0001 * ((i % 5) - 2))
    return out


def test_entry_score_keys_are_26():
    assert len(es.ENTRY_SCORE_KEYS) == 26
    assert "mc_real_rate_diff" in es.UNAVAILABLE_ENTRY_SCORES
    assert "mc_curve_slope_diff" in es.UNAVAILABLE_ENTRY_SCORES


def test_compute_scores_no_lookahead_and_shape():
    frames = _universe_frames()
    as_of = date(2024, 6, 3)

    def getter(ticker: str, ao: date) -> pd.DataFrame | None:
        df = frames.get(ticker)
        if df is None:
            return None
        day_end = pd.Timestamp(ao) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        return df[df.index <= day_end]

    # Poison future: if scores peek past as_of, this spike would leak into ATR/z.
    for df in frames.values():
        future = df.index > pd.Timestamp(as_of)
        if future.any():
            df.loc[future, "Close"] = df.loc[future, "Close"] * 10.0
            df.loc[future, "High"] = df.loc[future, "High"] * 10.0

    scores = es.compute_entry_scores(
        "EURUSD",
        as_of,
        get_daily_ohlc=getter,
        universe=tuple(frames.keys()),
    )
    assert set(scores.keys()) == set(es.ENTRY_SCORE_KEYS)
    assert scores["mc_real_rate_diff"] is None
    assert scores["mc_curve_slope_diff"] is None

    # Structure + macro that only need this pair's past should populate.
    assert scores["st_dist_to_high_atr"] is not None
    assert scores["st_dist_to_low_atr"] is not None
    assert scores["st_range_compression"] is not None
    assert scores["mc_rate_diff_delta20"] is not None
    assert scores["mc_level_change_agree"] in (0.0, 1.0)
    assert scores["mc_policy_age_days"] is not None
    assert scores["ev_calendar_density"] is not None

    # Cross-sectional ranks in 1..8
    assert scores["cs_ccy_strength_base"] is not None
    assert 1.0 <= scores["cs_ccy_strength_base"] <= 8.0
    assert scores["cs_ccy_strength_quote"] is not None
    assert 1.0 <= scores["cs_ccy_strength_quote"] <= 8.0


def test_attach_and_summary():
    frames = _universe_frames()
    as_of = date(2024, 6, 3)

    def getter(ticker: str, ao: date) -> pd.DataFrame | None:
        df = frames.get(ticker)
        if df is None:
            return None
        day_end = pd.Timestamp(ao) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        return df[df.index <= day_end]

    es.reset_entry_score_stats()
    row = {
        "ticker": "EURUSD",
        "date": as_of.isoformat(),
        "outcome": "WIN",
        "pnl_dollars": 10.0,
    }
    es.attach_entry_scores(row, get_daily_ohlc=getter, universe=tuple(frames.keys()))
    assert "scores" in row
    assert len(row["scores"]) == 26

    summary = es.build_scores_summary([row])
    assert "st_dist_to_high_atr" in summary
    st = summary["st_dist_to_high_atr"]
    assert st["count"] == 1
    assert st["min"] == st["max"] == st["median"]
    assert summary["mc_real_rate_diff"]["count"] == 0
    assert summary["mc_real_rate_diff"]["unavailable"] == 1


def test_scores_ignore_exit_fields():
    """Scores must not read outcome fields even if present on the row."""
    frames = _universe_frames()
    as_of = date(2024, 6, 3)

    def getter(ticker: str, ao: date) -> pd.DataFrame | None:
        df = frames.get(ticker)
        if df is None:
            return None
        day_end = pd.Timestamp(ao) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        return df[df.index <= day_end]

    row = {
        "ticker": "GBPUSD",
        "date": as_of.isoformat(),
        "outcome": "LOSS",
        "exit_price": 999.0,
        "pnl_dollars": -50.0,
        "nights_held": 99,
        "peak_profit_dollars": 999.0,
        "hit_tp3": True,
        "exit_reason": "TP3",
    }
    s1 = es.compute_entry_scores(
        "GBPUSD",
        as_of,
        get_daily_ohlc=getter,
        universe=tuple(frames.keys()),
    )
    # Mutate forbidden fields — recompute from ticker/date only must match.
    s2 = es.compute_entry_scores(
        row["ticker"],
        row["date"],
        get_daily_ohlc=getter,
        universe=tuple(frames.keys()),
    )
    assert s1 == s2


def test_non_forex_returns_all_null():
    def getter(ticker: str, ao: date) -> pd.DataFrame | None:
        return _synth_ohlc()

    scores = es.compute_entry_scores("QQQ", date(2024, 6, 3), get_daily_ohlc=getter)
    assert all(v is None for v in scores.values())
