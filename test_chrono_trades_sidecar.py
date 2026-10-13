"""Append-only chrono trades/skips sidecars (Group B PR6)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import continuous_backtester as cb


def _trade(date: str, ticker: str = "EURUSD", outcome: str = "WIN", pnl: float = 10.0) -> dict[str, Any]:
    return {
        "date": date,
        "ticker": ticker,
        "outcome": outcome,
        "pnl_dollars": pnl,
        "skipped": False,
    }


def _skip(date: str, ticker: str = "GBPUSD") -> dict[str, Any]:
    return {
        "date": date,
        "ticker": ticker,
        "outcome": "SKIPPED",
        "skipped": True,
        "skip_reason": "test",
    }


def test_append_and_load_sidecars(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    job = "side01"
    tpath = cb.chrono_trades_path(job)
    spath = cb.chrono_skips_path(job)
    assert tpath.parent == tmp_path

    n = cb.append_chrono_sidecar(tpath, [_trade("2024-01-02"), _trade("2024-01-03", outcome="LOSS", pnl=-5)])
    assert n == 2
    assert cb.append_chrono_sidecar(spath, [_skip("2024-01-02")]) == 1

    trades = cb.load_chrono_sidecar(tpath)
    skips = cb.load_chrono_sidecar(spath)
    assert len(trades) == 2
    assert trades[0]["ticker"] == "EURUSD"
    assert len(skips) == 1
    assert skips[0]["skipped"] is True

    # Compact JSONL (no indent)
    line = tpath.read_text(encoding="utf-8").splitlines()[0]
    assert "\n  " not in line
    assert json.loads(line)["date"] == "2024-01-02"


def test_migrate_embedded_arrays_and_strip_on_save(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    job = "legacy01"
    chrono_path = cb.chrono_results_path(job)
    embedded = {
        "job_id": job,
        "status": "running",
        "daily_pnl": [{"date": "2024-01-02", "pnl": 10, "capital": 10010}],
        "all_trades": [_trade("2024-01-02"), _trade("2024-01-02", ticker="USDJPY")],
        "skipped": [_skip("2024-01-02")],
        "capital": 10010,
        "days_processed": 1,
    }
    assert cb.save_json(chrono_path, embedded) is not None

    data = cb.load_json(chrono_path)
    cb.migrate_chrono_embedded_arrays(job, data)
    assert data["all_trades"] == []
    assert data["skipped"] == []
    assert cb.chrono_trades_path(job).is_file()
    assert cb.chrono_skips_path(job).is_file()
    assert len(cb.load_chrono_job_trades(job, data)) == 2
    assert len(cb.load_chrono_job_skips(job, data)) == 1

    data["capital"] = 10020
    assert cb.save_chrono_results(chrono_path, data) is not None
    on_disk = cb.load_json(chrono_path)
    assert on_disk["all_trades"] == []
    assert on_disk["skipped"] == []
    assert on_disk["capital"] == 10020
    # Sidecars unchanged by state save
    assert len(cb.load_chrono_job_trades(job, on_disk)) == 2


def test_reconcile_trims_orphan_sidecar_rows(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    job = "orphan01"
    # Completed day 01-02 only; sidecar also has an orphan 01-03 row (failed flush).
    state = {
        "job_id": job,
        "daily_pnl": [{"date": "2024-01-02", "pnl": 1, "capital": 10001}],
        "all_trades": [],
        "skipped": [],
    }
    cb.append_chrono_sidecar(
        cb.chrono_trades_path(job),
        [_trade("2024-01-02"), _trade("2024-01-03")],
    )
    cb.append_chrono_sidecar(
        cb.chrono_skips_path(job),
        [_skip("2024-01-02"), _skip("2024-01-03")],
    )
    trades, skips = cb.reconcile_chrono_sidecars(job, state)
    assert [t["date"] for t in trades] == ["2024-01-02"]
    assert [s["date"] for s in skips] == ["2024-01-02"]
    # Sidecars rewritten
    assert len(cb.load_chrono_sidecar(cb.chrono_trades_path(job))) == 1
    assert len(cb.load_chrono_sidecar(cb.chrono_skips_path(job))) == 1


def test_flush_day_and_load_chrono_job_hydrate(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    job = "flush01"
    state = {
        "job_id": job,
        "status": "running",
        "start_date": "2024-01-02",
        "end_date": "2024-01-05",
        "capital": 10000,
        "daily_pnl": [],
        "all_trades": [],
        "skipped": [],
        "days_processed": 0,
    }
    assert cb.save_chrono_results(cb.chrono_results_path(job), state) is not None

    day_trades = [_trade("2024-01-02"), _trade("2024-01-02", ticker="AUDUSD", outcome="LOSS", pnl=-8)]
    day_skips = [_skip("2024-01-02")]
    cb.flush_chrono_day_sidecars(job, day_trades, day_skips)
    state["daily_pnl"] = [{"date": "2024-01-02", "pnl": 2, "trades": 2, "skipped": 1, "capital": 10002}]
    state["days_processed"] = 1
    state["summary"] = {"total_trades": 2}
    assert cb.save_chrono_results(cb.chrono_results_path(job), state) is not None

    hydrated = cb.load_chrono_job(job, hydrate=True)
    assert hydrated is not None
    assert len(hydrated["all_trades"]) == 2
    assert len(hydrated["skipped"]) == 1
    assert hydrated["capital"] == 10000 or hydrated["days_processed"] == 1

    lean = cb.load_chrono_job(job, hydrate=False)
    assert lean is not None
    assert lean["all_trades"] == []
    assert lean["skipped"] == []

    export = cb.build_chrono_job_export_text(job)
    assert export is not None
    assert "TRADES: 2 actual" in export
    assert "EURUSD" in export


def test_resume_histories_see_sidecar_trades(tmp_path: Path, monkeypatch: Any) -> None:
    """A+B history rebuild inputs come from sidecars after migrate/reconcile."""
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    job = "resume01"
    rows = [
        _trade("2024-01-02", ticker="EURUSD", outcome="WIN", pnl=12),
        _trade("2024-01-03", ticker="EURUSD", outcome="LOSS", pnl=-6),
        _trade("2024-01-03", ticker="GBPUSD", outcome="WIN", pnl=9),
    ]
    for r in rows:
        r["strategy_id"] = "S01"
        r["timeframe"] = "1d"
        r["direction"] = "LONG"
    state = {
        "job_id": job,
        "daily_pnl": [
            {"date": "2024-01-02", "pnl": 12, "capital": 10012},
            {"date": "2024-01-03", "pnl": 3, "capital": 10015},
        ],
        "all_trades": list(rows),  # legacy embedded
        "skipped": [],
    }
    cb.migrate_chrono_embedded_arrays(job, state)
    trades, _skips = cb.reconcile_chrono_sidecars(job, state)
    assert len(trades) == 3
    assert state["all_trades"] == []
    # Second resume must not duplicate
    cb.migrate_chrono_embedded_arrays(job, state)
    trades2, _ = cb.reconcile_chrono_sidecars(job, state)
    assert len(trades2) == 3
