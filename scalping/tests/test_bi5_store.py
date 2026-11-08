"""`.bi5` decode and the incremental parquet cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scalping.data.bars import ticks_to_m1
from scalping.data.bi5 import bi5_url, decode_bi5, encode_bi5
from scalping.data.download import iter_hours, sync_symbol
from scalping.data.store import load_bars, load_ticks
from scalping.instruments import get_instrument


def test_bi5_url_month_is_zero_based():
    hour = datetime(2018, 1, 3, 7, tzinfo=timezone.utc)
    url = bi5_url("EURUSD", hour)
    assert url.endswith("/EURUSD/2018/00/03/07h_ticks.bi5")


def test_bi5_roundtrip_keeps_bid_ask_and_volume():
    hour = datetime(2018, 1, 3, 7, tzinfo=timezone.utc)
    payload = encode_bi5(
        [
            (150, 1.10000, 1.10012, 1.5, 2.5),
            (4000, 1.10002, 1.10018, 0.25, 0.75),
        ],
        price_scale=100_000,
    )
    frame = decode_bi5(payload, hour, "EURUSD")
    assert len(frame) == 2
    assert frame.loc[0, "bid"] == pytest.approx(1.10000)
    assert frame.loc[0, "ask"] == pytest.approx(1.10012)
    assert frame.loc[0, "bid_volume"] == pytest.approx(1.5)
    assert frame.loc[1, "ask_volume"] == pytest.approx(0.75)
    assert frame.loc[0, "timestamp"] == pd.Timestamp("2018-01-03T07:00:00.150Z")
    bars = ticks_to_m1(frame)
    assert len(bars) == 1
    assert bars.loc[0, "spread_open"] == pytest.approx(0.00012)
    assert bars.loc[0, "spread_close"] == pytest.approx(0.00016)
    assert bars.loc[0, "spread_mean"] == pytest.approx((0.00012 + 0.00016) / 2)
    assert bars.loc[0, "open"] == pytest.approx((1.10000 + 1.10012) / 2)


def test_empty_bi5_is_an_empty_frame():
    hour = datetime(2018, 1, 3, tzinfo=timezone.utc)
    frame = decode_bi5(b"", hour, "USDJPY")
    assert frame.empty
    assert list(frame.columns) == ["timestamp", "bid", "ask", "bid_volume", "ask_volume"]


def test_cache_never_redownloads_and_skips_saturday(tmp_path):
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)  # Monday
    end = start + timedelta(hours=2)
    calls: list[datetime] = []

    def fetcher(symbol, hour):
        calls.append(hour)
        if hour.hour == 0:
            return encode_bi5([(10, 1.10000, 1.10014, 1.0, 1.0)], get_instrument(symbol).price_scale)
        return b""

    first = sync_symbol("EURUSD", start, end, tmp_path, fetcher=fetcher, request_delay=0)
    assert first.fetched_hours == 2
    assert len(calls) == 2
    second = sync_symbol("EURUSD", start, end, tmp_path, fetcher=fetcher, request_delay=0)
    assert second.fetched_hours == 0
    assert second.cached_hours == 2
    assert len(calls) == 2

    saturday = datetime(2018, 1, 6, tzinfo=timezone.utc)
    before = len(calls)
    sync_symbol("EURUSD", saturday, saturday + timedelta(days=1), tmp_path, fetcher=fetcher, request_delay=0)
    assert len(calls) == before


def test_full_day_is_materialized_once(tmp_path):
    start = datetime(2018, 1, 2, tzinfo=timezone.utc)  # Tuesday
    end = start + timedelta(days=1)

    def fetcher(symbol, hour):
        if hour.hour == 15:
            return encode_bi5(
                [(0, 110.000, 110.004, 3.0, 4.0), (1000, 110.010, 110.016, 1.0, 1.0)],
                get_instrument("USDJPY").price_scale,
            )
        return b""

    report = sync_symbol("USDJPY", start, end, tmp_path, fetcher=fetcher, request_delay=0)
    assert report.fetched_hours == 24
    assert report.tick_days_written == 1
    assert report.bar_days_written == 1
    ticks = load_ticks(tmp_path, "USDJPY", start, end)
    bars = load_bars(tmp_path, "USDJPY", start, end)
    assert len(ticks) == 2
    assert ticks.loc[0, "bid"] == pytest.approx(110.000)
    assert ticks.loc[0, "ask"] == pytest.approx(110.004)
    assert len(bars) == 1
    assert bars.loc[0, "spread_open"] == pytest.approx(0.004)
    assert bars.loc[0, "spread_mean"] == pytest.approx((0.004 + 0.006) / 2)
    # A second sync must not fetch and must not rewrite the day.
    again = sync_symbol("USDJPY", start, end, tmp_path, fetcher=fetcher, request_delay=0)
    assert again.fetched_hours == 0
    assert again.tick_days_written == 0


def test_hour_iterator_skips_saturday():
    start = datetime(2018, 1, 5, 22, tzinfo=timezone.utc)  # Friday
    end = datetime(2018, 1, 8, tzinfo=timezone.utc)  # Monday
    hours = list(iter_hours(start, end, skip_saturday=True))
    assert all(hour.weekday() != 5 for hour in hours)
    assert any(hour.weekday() == 6 for hour in hours)
