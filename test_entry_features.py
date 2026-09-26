"""Tests for entry-time features / labels JSONL snapshots."""
from __future__ import annotations

import json
from pathlib import Path

import entry_features as ef
import entry_scores as es


def _sample_row(**overrides):
    row = {
        "date": "2024-06-03",
        "ticker": "EURUSD",
        "timeframe": "1d",
        "strategy_id": "T01_EMA_PULLBACK",
        "direction": "LONG",
        "entry_atr": 0.005,
        "entry_atr_vs_avg": 1.1,
        "entry_candle_body": 0.001,
        "trend_strength": 0.6,
        "trend_size_mult": 1.0,
        "macro_score": 0.2,
        "macro_sentiment": 0.0,
        "macro_rate_diff": 1.5,
        "macro_size_multiplier": 1.0,
        "macro_bias": "TAILWIND",
        "macro_trend": "UP",
        "confidence": "MEDIUM",
        "confidence_pre_upgrade": "LOW",
        "strategy_confluence_count": 2,
        "conviction_score": 5,
        "st_layer2_score": 3,
        "st_boost_tier": "NONE",
        "period_mode": "NEUTRAL",
        "market_phase": "TRENDING_UP",
        "volatility_regime": "NORMAL_VOL",
        "weekly_candle_age": 1,
        "zone_position_pct": 35.0,
        "zone_label": "DISCOUNT",
        "strat_health_last3": 10.0,
        "strat_health_last10": 20.0,
        "strat_health_n": 12,
        "st_health_last3": 5.0,
        "st_health_last5": 8.0,
        "st_health_n": 7,
        "sys_winrate_last20": 0.55,
        "sys_winrate_last50": 0.52,
        "sys_followthrough_last20": 1.2,
        "sys_followthrough_last50": 1.1,
        "sys_never_profit_rate_20": 0.1,
        "ab_throttle": 1.0,
        "ab_mult_a": 1.0,
        "ab_mult_b": 1.0,
        "risk_pct_of_price": 0.5,
        "vol_scale_applied": 1.0,
        "event_days_to_next": 4,
        "event_days_since_last": 2,
        "event_entry_window": "POST_3D",
        "session": "LONDON",
        "entry_hour_utc": 8,
        "session_day": "Monday",
        "scores": {k: 0.0 for k in es.ENTRY_SCORE_KEYS},
        # labels
        "nights_held": 12.0,
        "pnl_r_net": 1.5,
        "outcome": "WIN",
        "exit_reason": "TRAIL_STOP",
        "hit_tp1": True,
        "hit_tp2": False,
        "hit_tp3": False,
        "peak_profit_dollars": 200.0,
        "candles_to_exit": 12,
        "event_held_through": 1,
        "pnl_dollars": 150.0,
    }
    row["scores"]["mc_real_rate_diff"] = None
    row["scores"]["mc_curve_slope_diff"] = None
    row.update(overrides)
    return row


def test_feature_label_keys_disjoint():
    ef.assert_feature_label_keys_disjoint()
    assert not (set(ef.ENTRY_FEATURE_KEYS) & set(ef.ENTRY_LABEL_KEYS))
    # Classic lookahead fields must be labels only.
    for leak in ("nights_held", "outcome", "pnl_r_net", "peak_profit_dollars", "event_held_through"):
        assert leak in ef.ENTRY_LABEL_KEYS
        assert leak not in ef.ENTRY_FEATURE_KEYS


def test_features_include_all_26_scores():
    feats = ef.build_entry_features(_sample_row())
    for k in es.ENTRY_SCORE_KEYS:
        assert k in feats
    assert len(es.ENTRY_SCORE_KEYS) == 26
    assert "cs_ccy_strength_base" in feats


def test_labels_exclude_entry_only_fields():
    labs = ef.build_entry_labels(_sample_row())
    assert set(labs.keys()) == set(ef.ENTRY_LABEL_KEYS)
    assert "ticker" not in labs
    assert "macro_bias" not in labs
    assert labs["outcome"] == "WIN"
    assert labs["nights_held"] == 12.0


def test_append_jsonl_and_completion_assert(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "DATA_DIR", tmp_path)
    job = "testjob01"
    path = ef.entry_features_path(job)
    assert path.parent == Path(tmp_path)

    r1 = _sample_row()
    r2 = _sample_row(outcome="LOSS", nights_held=2.0, pnl_r_net=-1.0, direction="SHORT")
    assert ef.append_entry_feature_record(r1, job_id=job) is not None
    assert ef.append_entry_feature_record(r2, job_id=job) is not None
    # Skips ignored
    assert ef.append_entry_feature_record(_sample_row(skipped=True), job_id=job) is None

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert "features" in rec and "labels" in rec
    assert set(rec["features"].keys()) == set(ef.ENTRY_FEATURE_KEYS)
    assert set(rec["labels"].keys()) == set(ef.ENTRY_LABEL_KEYS)
    # No overlap inside the written record
    assert not (set(rec["features"]) & set(rec["labels"]))
    assert "nights_held" not in rec["features"]
    assert "outcome" not in rec["features"]

    stats = ef.assert_entry_features_file_ok(job)
    assert stats["rows"] == 2
    assert stats["checked"] == 2


def test_completion_rejects_label_leak_in_features(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "DATA_DIR", tmp_path)
    job = "badjob"
    path = ef.entry_features_path(job)
    path.write_text(
        json.dumps(
            {
                "date": "2024-01-01",
                "job_id": job,
                "features": {"ticker": "EURUSD", "nights_held": 5},  # LEAK
                "labels": {"nights_held": 5, "outcome": "WIN"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        ef.assert_entry_features_file_ok(job)
        assert False, "expected AssertionError"
    except AssertionError as e:
        assert "overlap" in str(e).lower() or "nights_held" in str(e)
