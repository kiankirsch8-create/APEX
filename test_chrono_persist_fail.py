"""Chrono must halt loudly when save_json cannot persist run state."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import continuous_backtester as cb


def test_persist_chrono_raises_and_marks_failed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "save_json", lambda *_a, **_k: None)
    data: dict[str, Any] = {"job_id": "x", "status": "running", "capital": 10000}
    try:
        cb._persist_chrono(tmp_path / "chrono_results_x.json", data, job_id="x")
        assert False, "expected ChronoPersistFailed"
    except cb.ChronoPersistFailed:
        pass
    assert data["status"] == "failed"
    assert data["error"] == "persist failed (disk?)"
    assert cb.CHRONO_LIVE_STATUS["status"] == "failed"


def test_run_halts_when_persist_fails_on_init(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cb, "chrono_results_path", lambda jid: tmp_path / f"chrono_results_{jid}.json")
    monkeypatch.setattr(cb, "_log_group1_startup_blocks", lambda: None)
    monkeypatch.setattr(cb, "_reset_group1_skip_log_state", lambda: None)
    monkeypatch.setattr(cb, "_reset_shadow_regime_state", lambda **_k: None)
    monkeypatch.setattr(cb, "reset_entry_score_stats", lambda: None)
    monkeypatch.setattr(cb, "log_unavailable_entry_scores", lambda: None)
    monkeypatch.setattr(cb, "_reset_entry_score_daily_cache", lambda: None)
    monkeypatch.setattr(cb, "assert_feature_label_keys_disjoint", lambda: None)
    # Simulate full/read-only disk: every save_json fails.
    monkeypatch.setattr(cb, "save_json", lambda *_a, **_k: None)

    out = cb.run_chronological_backtest(
        start_date="2020-01-01",
        end_date="2020-01-05",
        job_id="diskfull",
    )
    assert out["status"] == "failed"
    assert out["error"] == "persist failed (disk?)"
    # Must not leave CHRONO_RUNNING stuck on.
    assert cb.CHRONO_RUNNING is False


def test_persist_success_records_last_write_iso(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / "chrono_results_ok.json"

    def ok_save(p: Any, data: Any) -> Path:
        Path(p).write_text("{}", encoding="utf-8")
        return Path(p)

    monkeypatch.setattr(cb, "save_json", ok_save)
    data: dict[str, Any] = {"status": "running"}
    cb._persist_chrono(path, data, job_id="ok")
    assert "last_write_iso" in data
    assert data["status"] == "running"
