"""Disk-full guard and /api/chrono/heartbeat for unattended chrono runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

import continuous_backtester as cb


def test_disk_guard_halts_when_free_space_low(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cb, "data_dir_free_mb", lambda: 50.0)  # below 200 MB
    written: list[dict[str, Any]] = []

    def fake_save(path: Any, data: Any) -> Path:
        written.append(dict(data))
        Path(path).write_text(json.dumps(data), encoding="utf-8")
        return Path(path)

    monkeypatch.setattr(cb, "save_json", fake_save)
    chrono_data: dict[str, Any] = {"status": "running", "capital": 10000}
    path = tmp_path / "chrono_results_x.json"
    ok = cb.chrono_disk_guard_ok(chrono_data, path, job_id="x", min_free_mb=200.0)
    assert ok is False
    assert chrono_data["status"] == "failed"
    assert "low disk" in str(chrono_data.get("error", ""))
    assert written and written[-1]["status"] == "failed"


def test_disk_guard_allows_when_space_ok(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cb, "data_dir_free_mb", lambda: 5_000.0)
    chrono_data: dict[str, Any] = {"status": "running"}
    assert (
        cb.chrono_disk_guard_ok(
            chrono_data, tmp_path / "c.json", job_id="x", min_free_mb=200.0
        )
        is True
    )
    assert chrono_data["status"] == "running"


def test_heartbeat_endpoint_returns_live_fields(tmp_path: Path, monkeypatch: Any) -> None:
    import api

    monkeypatch.setattr(cb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cb, "data_dir_free_mb", lambda: 1234.5)
    jid = "hb01"
    path = tmp_path / f"chrono_results_{jid}.json"
    path.write_text(
        json.dumps(
            {
                "job_id": jid,
                "status": "running",
                "current_date": "2022-04-01",
                "days_processed": 7,
                "capital": 11000,
                "last_write_iso": "2022-04-01T12:00:00Z",
                "all_trades": [
                    {"outcome": "WIN", "pnl_dollars": 10},
                    {"outcome": "LOSS", "pnl_dollars": -5},
                    {"outcome": "SKIPPED", "skipped": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cb, "get_active_chrono", lambda: {"job_id": jid})
    cb.CHRONO_LIVE_STATUS.update(
        {
            "job_id": jid,
            "current_date": "2022-04-01",
            "status": "scanning",
            "days_processed": 7,
            "capital": 11000,
        }
    )
    client = TestClient(api.app)
    resp = client.get("/api/chrono/heartbeat")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == jid
    assert body["current_date"] == "2022-04-01"
    assert body["days_processed"] == 7
    assert body["trades_so_far"] == 2
    assert body["free_disk_mb"] == 1234.5
    assert body["last_write_iso"] == "2022-04-01T12:00:00Z"
    assert body["status"] == "running"
    assert body["min_free_disk_mb"] == cb.CHRONO_MIN_FREE_DISK_MB
