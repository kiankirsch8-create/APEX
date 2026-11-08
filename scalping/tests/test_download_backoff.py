"""503s and read timeouts back off, then the run continues and can resume."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scalping.config import load_config
from scalping.data.bi5 import cache_relpath
from scalping.data.download import (
    DownloadReport,
    FetchPolicy,
    TransientFetchError,
    backoff_seconds,
    fetch_bi5_http,
    is_retryable,
    run_exit_code,
    sync_symbol,
)
from scalping.data.pipeline import main


class _Unavailable(Exception):
    status_code = 503


class _ReadTimeout(Exception):
    """Name matches requests.ReadTimeout so the classifier treats it as transient."""


def _no_jitter(low: float, high: float) -> float:
    return high


def test_backoff_schedule_is_exponential_and_capped():
    assert backoff_seconds(0, initial=1, max_backoff=60, uniform=_no_jitter) == pytest.approx(1)
    assert backoff_seconds(1, initial=1, max_backoff=60, uniform=_no_jitter) == pytest.approx(2)
    assert backoff_seconds(2, initial=1, max_backoff=60, uniform=_no_jitter) == pytest.approx(4)
    assert backoff_seconds(6, initial=1, max_backoff=60, uniform=_no_jitter) == pytest.approx(60)
    halved = backoff_seconds(3, initial=1, max_backoff=60, uniform=lambda low, high: low)
    assert halved == pytest.approx(4)


def test_retryable_covers_503_timeout_and_max_retry_error():
    assert is_retryable(TransientFetchError("503", status=503))
    assert is_retryable(_Unavailable("503 Service Unavailable"))
    assert is_retryable(_ReadTimeout("read timed out"))

    class MaxRetryError(Exception):
        pass

    assert is_retryable(MaxRetryError("HTTPSConnectionPool: Max retries exceeded"))
    assert not is_retryable(RuntimeError("decode failed"))


def test_503_then_success_retries_the_same_hour(tmp_path):
    hour = datetime(2018, 1, 2, 0, tzinfo=timezone.utc)
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fetcher(symbol, when):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _Unavailable("503")
        return b""

    report = sync_symbol(
        "EURUSD",
        hour,
        hour + timedelta(hours=1),
        tmp_path,
        fetcher=fetcher,
        request_delay=0,
        max_retries=4,
        sleep=sleeps.append,
        uniform=_no_jitter,
    )
    assert report.fetched_hours == 1
    assert report.missed_hours == []
    assert sleeps == [1.0, 2.0]
    assert attempts["n"] == 3
    saved = tmp_path / cache_relpath("EURUSD", hour)
    assert saved.read_bytes() == b""
    assert report.empty_hours == 1


def test_exhausted_503_skips_the_hour_and_continues(tmp_path):
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)  # Monday
    end = start + timedelta(hours=3)
    sleeps: list[float] = []
    calls: list[datetime] = []

    def fetcher(symbol, hour):
        calls.append(hour)
        if hour.hour == 1:
            raise _ReadTimeout("read timed out")
        return b""

    report = sync_symbol(
        "EURUSD",
        start,
        end,
        tmp_path,
        fetcher=fetcher,
        request_delay=0,
        max_retries=3,
        sleep=sleeps.append,
        uniform=_no_jitter,
    )
    assert report.fetched_hours == 2
    assert report.missed_hours == ["2018-01-01T01:00:00Z"]
    assert sleeps == [1.0, 2.0, 4.0]
    # 1 initial try + 3 retries on the bad hour, plus one try each for the others.
    assert sum(1 for hour in calls if hour.hour == 1) == 4
    missed = tmp_path / cache_relpath("EURUSD", start + timedelta(hours=1))
    assert not missed.exists()
    assert (tmp_path / cache_relpath("EURUSD", start)).exists()
    assert (tmp_path / cache_relpath("EURUSD", start + timedelta(hours=2))).exists()


def test_rerun_fills_only_the_previously_missed_hour(tmp_path):
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)

    def failing(symbol, hour):
        if hour.hour == 1:
            raise _Unavailable("503")
        return b""

    first = sync_symbol(
        "EURUSD",
        start,
        end,
        tmp_path,
        fetcher=failing,
        request_delay=0,
        max_retries=1,
        sleep=lambda _seconds: None,
        uniform=_no_jitter,
    )
    assert first.missed_hours == ["2018-01-01T01:00:00Z"]

    second_calls: list[datetime] = []

    def recovered(symbol, hour):
        second_calls.append(hour)
        return b""

    second = sync_symbol(
        "EURUSD",
        start,
        end,
        tmp_path,
        fetcher=recovered,
        request_delay=0,
        max_retries=1,
        sleep=lambda _seconds: None,
    )
    assert [hour.hour for hour in second_calls] == [1]
    assert second.fetched_hours == 1
    assert second.cached_hours == 2
    assert second.missed_hours == []
    saved = tmp_path / cache_relpath("EURUSD", start + timedelta(hours=1))
    assert saved.is_file() and saved.stat().st_size == 0


def test_unexpected_error_does_not_abort_the_run(tmp_path):
    start = datetime(2018, 1, 2, tzinfo=timezone.utc)
    sleeps: list[float] = []

    def fetcher(symbol, hour):
        if hour.hour == 0:
            raise RuntimeError("boom")
        return b""

    report = sync_symbol(
        "EURUSD",
        start,
        start + timedelta(hours=2),
        tmp_path,
        fetcher=fetcher,
        request_delay=0,
        max_retries=5,
        sleep=sleeps.append,
    )
    assert sleeps == []
    assert report.missed_hours == ["2018-01-02T00:00:00Z"]
    assert report.fetched_hours == 1


def test_delay_is_between_requests_and_skipped_for_cache_hits(tmp_path):
    start = datetime(2018, 1, 2, tzinfo=timezone.utc)
    sleeps: list[float] = []

    def fetcher(symbol, hour):
        return b""

    sync_symbol(
        "EURUSD",
        start,
        start + timedelta(hours=3),
        tmp_path,
        fetcher=fetcher,
        request_delay=0.3,
        sleep=sleeps.append,
    )
    assert sleeps == [0.3, 0.3]
    sleeps.clear()
    calls: list[datetime] = []

    def should_not_run(symbol, hour):
        calls.append(hour)
        return b""

    again = sync_symbol(
        "EURUSD",
        start,
        start + timedelta(hours=3),
        tmp_path,
        fetcher=should_not_run,
        request_delay=0.3,
        sleep=sleeps.append,
    )
    assert calls == []
    assert sleeps == []
    assert again.cached_hours == 3
    assert again.fetched_hours == 0


def test_http_503_and_read_timeout_are_transient():
    hour = datetime(2018, 1, 3, 7, tzinfo=timezone.utc)

    class _Response:
        def __init__(self, status, content=b""):
            self.status_code = status
            self.content = content

    class _Session:
        def __init__(self, results):
            self._results = list(results)

        def get(self, url, headers=None, timeout=None):
            item = self._results.pop(0) if len(self._results) > 1 else self._results[0]
            if isinstance(item, Exception):
                raise item
            return item

    import requests

    with pytest.raises(TransientFetchError) as caught:
        fetch_bi5_http("EURUSD", hour, _Session([_Response(503), _Response(503), _Response(503)]))
    assert caught.value.status == 503

    payload = fetch_bi5_http(
        "EURUSD",
        hour,
        _Session([requests.ReadTimeout("read timed out"), _Response(200, b"ok")]),
    )
    assert payload == b"ok"
    assert fetch_bi5_http("EURUSD", hour, _Session([_Response(404)])) == b""


def test_exit_code_is_nonzero_only_when_nothing_was_downloaded():
    missed = DownloadReport(symbol="EURUSD", fetched_hours=0, missed_hours=["2018-01-01T00:00:00Z"])
    partial = DownloadReport(symbol="EURUSD", fetched_hours=4, missed_hours=["2018-01-01T05:00:00Z"])
    cached = DownloadReport(symbol="EURUSD", fetched_hours=0, cached_hours=10)
    assert run_exit_code([missed]) == 1
    assert run_exit_code([partial]) == 0
    assert run_exit_code([cached]) == 0
    assert run_exit_code([missed, partial]) == 0


def test_cli_reports_misses_and_honours_throttle_flags(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_sync(symbol, start, end, data_dir, **kwargs):
        seen["policy"] = kwargs["policy"]
        return DownloadReport(symbol=symbol, fetched_hours=0, missed_hours=["2018-01-02T00:00:00Z"])

    monkeypatch.setattr("scalping.data.pipeline.sync_symbol", fake_sync)
    code = main(
        [
            "download",
            "--pair",
            "EURUSD",
            "--start",
            "2018-01-02",
            "--end",
            "2018-01-03",
            "--data-dir",
            str(tmp_path),
            "--delay",
            "0.5",
            "--max-retries",
            "4",
            "--max-backoff",
            "30",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "missed 2018-01-02T00:00:00Z" in out
    assert "missed 1 hour" in out
    policy = seen["policy"]
    assert isinstance(policy, FetchPolicy)
    assert policy.request_delay == pytest.approx(0.5)
    assert policy.max_retries == 4
    assert policy.max_backoff == pytest.approx(30)


def test_cli_exits_zero_when_some_hours_downloaded(monkeypatch, capsys, tmp_path):
    def fake_sync(symbol, start, end, data_dir, **kwargs):
        return DownloadReport(
            symbol=symbol,
            fetched_hours=3,
            missed_hours=["2018-01-02T04:00:00Z"],
        )

    monkeypatch.setattr("scalping.data.pipeline.sync_symbol", fake_sync)
    code = main(
        [
            "download",
            "--pair",
            "EURUSD",
            "--start",
            "2018-01-02",
            "--end",
            "2018-01-03",
            "--data-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert "missed 1 hour" in capsys.readouterr().out


def test_config_exposes_download_throttle():
    cfg = load_config()
    assert cfg.request_delay == pytest.approx(0.3)
    assert cfg.max_retries == 6
    assert cfg.max_backoff == pytest.approx(60.0)
