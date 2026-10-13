"""Trim chrono dead fields; restore shadow_strategy_trades.jsonl from /dev/null symlink."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import continuous_backtester as cb
import shadow_strategies as ss


def test_strip_chrono_dead_trade_fields() -> None:
    row = {
        "ticker": "EURUSD",
        "calendar_action": "CLEAR",
        "session": "n/a",
        "price_zone": "EQUILIBRIUM",
        "market_structure": "UNKNOWN",
        "htf_bias": "UNKNOWN",
        "zone_position_pct": 22.0,
        "zone_label": "DEEP_DISCOUNT",
        "outcome": "WIN",
    }
    out = cb._strip_chrono_dead_trade_fields(row)
    assert "calendar_action" not in out
    assert "session" not in out
    assert "price_zone" not in out
    assert "market_structure" not in out
    assert "htf_bias" not in out
    assert out["zone_position_pct"] == 22.0
    assert out["zone_label"] == "DEEP_DISCOUNT"


def test_ensure_shadow_trades_removes_dev_null_symlink(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "shadow_strategy_trades.jsonl"
    # Simulate the emergency disk fix: symlink → /dev/null
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to("/dev/null")
    assert target.is_symlink()
    monkeypatch.setattr(ss, "SHADOW_TRADES_FILE", target)
    # Reset index so append rebuilds against the new path.
    monkeypatch.setattr(ss, "_shadow_seen_keys", None)
    monkeypatch.setattr(ss, "_shadow_trades_count", 0)

    path = ss.ensure_shadow_trades_file_real()
    assert path == target
    assert not path.is_symlink()
    assert path.is_file()

    n = ss.append_shadow_trade(
        {
            "strategy_id": "SH01",
            "ticker": "EURUSD",
            "timeframe": "1d",
            "date": "2022-01-03",
            "outcome": "WIN",
        }
    )
    assert n == 1
    assert path.is_file() and not path.is_symlink()
    text = path.read_text(encoding="utf-8")
    assert "EURUSD" in text
    assert path.stat().st_size > 0
