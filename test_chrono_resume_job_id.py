"""Optional job_id on /api/chrono/start enables resume of incomplete runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient


def test_start_chrono_mints_job_id_when_omitted(monkeypatch: Any) -> None:
    import api
    import continuous_backtester as cb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cb, "get_active_chrono", lambda: None)
    spawned: list[tuple[str, str, str]] = []

    def capture(start: str, end: str, job_id: str) -> None:
        spawned.append((start, end, job_id))

    monkeypatch.setattr(api, "_spawn_chronological_backtest", capture)
    client = TestClient(api.app)
    resp = client.post("/api/chrono/start", params={"start_date": "2020-01-01", "end_date": "2020-01-10"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["resuming"] is False
    assert body["job_id"]
    assert len(spawned) == 1
    assert spawned[0][2] == body["job_id"]


def test_start_chrono_resumes_when_job_id_has_incomplete_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import api
    import continuous_backtester as cb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cb, "get_active_chrono", lambda: None)
    jid = "resume01"
    path = tmp_path / f"chrono_results_{jid}.json"
    path.write_text(
        json.dumps(
            {
                "job_id": jid,
                "status": "running",
                "current_date": "2021-06-15",
                "capital": 12345.67,
                "days_processed": 42,
                "daily_pnl": [{"date": "2021-06-15", "capital": 12345.67}],
            }
        ),
        encoding="utf-8",
    )
    spawned: list[str] = []

    def capture(start: str, end: str, job_id: str) -> None:
        spawned.append(job_id)

    monkeypatch.setattr(api, "_spawn_chronological_backtest", capture)
    client = TestClient(api.app)
    resp = client.post(
        "/api/chrono/start",
        params={
            "start_date": "2020-01-01",
            "end_date": "2025-01-01",
            "job_id": jid,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == jid
    assert body["status"] == "resumed"
    assert body["resuming"] is True
    assert body["resume_capital"] == 12345.67
    assert body["resume_current_date"] == "2021-06-15"
    assert spawned == [jid]


def test_chrono_run_resumes_capital_from_saved_file(tmp_path: Path, monkeypatch: Any) -> None:
    """Existing resume path: incomplete file → continue from saved capital/date."""
    import continuous_backtester as cb

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cb, "CHRONO_RUNNING", False)
    monkeypatch.setattr(cb, "set_active_chrono", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "clear_active_chrono", lambda: None)
    monkeypatch.setattr(cb, "get_active_chrono", lambda: None)
    monkeypatch.setattr(cb, "clear_chrono_stop_flag", lambda *_a, **_k: None)
    monkeypatch.setattr(cb, "chrono_stop_requested", lambda *_a, **_k: True)
    monkeypatch.setattr(cb, "_log_group1_startup_blocks", lambda: None)
    monkeypatch.setattr(cb, "_reset_group1_skip_log_state", lambda: None)
    monkeypatch.setattr(cb, "_reset_shadow_regime_state", lambda **_k: None)
    monkeypatch.setattr(cb, "reset_entry_score_stats", lambda: None)
    monkeypatch.setattr(cb, "log_unavailable_entry_scores", lambda: None)
    monkeypatch.setattr(cb, "_reset_entry_score_daily_cache", lambda: None)
    monkeypatch.setattr(cb, "assert_feature_label_keys_disjoint", lambda: None)
    monkeypatch.setattr(cb, "_rebuild_strategy_trade_counts_from_rows", lambda *_a: None)
    monkeypatch.setattr(cb, "_rebuild_combo_trade_stats_from_rows", lambda *_a: None)
    monkeypatch.setattr(cb, "_rebuild_combo_rolling_trades_from_rows", lambda *_a: None)
    monkeypatch.setattr(cb, "_rebuild_st_medium_history_from_rows", lambda *_a: None)
    monkeypatch.setattr(cb, "_rebuild_health_signal_histories_from_rows", lambda *_a: None)
    monkeypatch.setattr(cb, "_v72_load_strategy_status", lambda **_k: None)
    monkeypatch.setattr(cb, "load_all_results", lambda: [])
    monkeypatch.setattr(cb, "SHADOW_BLOCKED_PAIRS", False)

    jid = "cap42"
    path = tmp_path / f"chrono_results_{jid}.json"
    path.write_text(
        json.dumps(
            {
                "job_id": jid,
                "start_date": "2020-01-01",
                "end_date": "2025-01-01",
                "status": "running",
                "current_date": "2021-03-10",
                "capital": 15500.0,
                "compounding_base_capital": 10000.0,
                "peak_capital": 16000.0,
                "days_processed": 10,
                "daily_pnl": [{"date": "2021-03-10", "capital": 15500.0, "pnl": 50.0}],
                "all_trades": [],
                "skipped": [],
            }
        ),
        encoding="utf-8",
    )

    out = cb.run_chronological_backtest(
        start_date="2020-01-01",
        end_date="2025-01-01",
        job_id=jid,
    )
    # Stop was requested immediately → cancelled, but capital must be the saved one.
    assert float(out.get("capital", 0) or 0) == 15500.0
    assert out.get("capital") != cb.STARTING_CAPITAL
