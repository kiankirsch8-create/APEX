"""
Entry-time feature / outcome-label snapshots for offline "will this trade run?" models.

``features`` — values known at ENTRY only (no lookahead).
``labels`` — outcome fields, kept strictly segregated.

Mixing a label into features is the classic lookahead failure; the completion
assertion rejects any key that appears in both maps.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from entry_scores import ENTRY_SCORE_KEYS
from utils import DATA_DIR, log

# Scalar entry-time fields (scores from PR I are appended via ENTRY_SCORE_KEYS).
ENTRY_FEATURE_SCALAR_KEYS: tuple[str, ...] = (
    "ticker",
    "timeframe",
    "strategy_id",
    "direction",
    "entry_atr",
    "entry_atr_vs_avg",
    "entry_candle_body",
    "trend_strength",
    "trend_size_mult",
    "macro_score",
    "macro_sentiment",
    "macro_rate_diff",
    "macro_size_multiplier",
    "macro_bias",
    "macro_trend",
    "confidence",
    "confidence_pre_upgrade",
    "strategy_confluence_count",
    "conviction_score",
    "st_layer2_score",
    "st_boost_tier",
    "period_mode",
    "market_phase",
    "volatility_regime",
    "weekly_candle_age",
    "zone_position_pct",
    "zone_label",
    "strat_health_last3",
    "strat_health_last10",
    "strat_health_n",
    "st_health_last3",
    "st_health_last5",
    "st_health_n",
    "sys_winrate_last20",
    "sys_winrate_last50",
    "sys_followthrough_last20",
    "sys_followthrough_last50",
    "sys_never_profit_rate_20",
    "ab_throttle",
    "ab_mult_a",
    "ab_mult_b",
    "risk_pct_of_price",
    "vol_scale_applied",
    "event_days_to_next",
    "event_days_since_last",
    "event_entry_window",
    "session",
    "entry_hour_utc",
    "session_day",
)

ENTRY_FEATURE_KEYS: tuple[str, ...] = ENTRY_FEATURE_SCALAR_KEYS + tuple(ENTRY_SCORE_KEYS)

ENTRY_LABEL_KEYS: tuple[str, ...] = (
    "nights_held",
    "pnl_r_net",
    "outcome",
    "exit_reason",
    "hit_tp1",
    "hit_tp2",
    "hit_tp3",
    "peak_profit_dollars",
    "candles_to_exit",
    "event_held_through",
)

_FEATURE_KEY_SET = frozenset(ENTRY_FEATURE_KEYS)
_LABEL_KEY_SET = frozenset(ENTRY_LABEL_KEYS)

_write_lock = threading.Lock()


def assert_feature_label_keys_disjoint() -> None:
    """Catalog-level guard: no key may live in both features and labels."""
    overlap = _FEATURE_KEY_SET & _LABEL_KEY_SET
    if overlap:
        raise AssertionError(
            f"entry feature/label key overlap (lookahead risk): {sorted(overlap)}"
        )


# Fail fast at import if catalogs ever collide.
assert_feature_label_keys_disjoint()


def entry_features_path(job_id: str) -> Path:
    jid = str(job_id or "").strip() or "unknown"
    return Path(DATA_DIR) / f"entry_features_{jid}.jsonl"


def build_entry_features(row: Mapping[str, Any]) -> dict[str, Any]:
    """Pull entry-time scalars + flattened PR-I scores from a completed trade row."""
    features: dict[str, Any] = {}
    for k in ENTRY_FEATURE_SCALAR_KEYS:
        features[k] = row.get(k)
    scores = row.get("scores")
    if isinstance(scores, Mapping):
        for k in ENTRY_SCORE_KEYS:
            features[k] = scores.get(k)
    else:
        for k in ENTRY_SCORE_KEYS:
            # Prefer nested scores map; fall back to top-level if ever flattened.
            features[k] = row.get(k)
    return features


def build_entry_labels(row: Mapping[str, Any]) -> dict[str, Any]:
    """Outcome-only fields — never feed these into a model feature matrix."""
    return {k: row.get(k) for k in ENTRY_LABEL_KEYS}


def assert_maps_disjoint(features: Mapping[str, Any], labels: Mapping[str, Any]) -> None:
    overlap = set(features.keys()) & set(labels.keys())
    if overlap:
        raise AssertionError(
            f"features/labels maps share keys (lookahead risk): {sorted(overlap)}"
        )


def append_entry_feature_record(
    row: Mapping[str, Any],
    *,
    job_id: str,
) -> dict[str, Any] | None:
    """
    Append one JSONL record to ``entry_features_{job_id}.jsonl``.

    Record shape (top-level date/job_id are bookkeeping only — not model inputs):
      {date, job_id, features: {...}, labels: {...}}
    """
    if not isinstance(row, Mapping):
        return None
    if row.get("skipped") or row.get("skip_trade"):
        return None
    if str(row.get("outcome") or "").strip().upper() not in ("WIN", "LOSS"):
        return None

    features = build_entry_features(row)
    labels = build_entry_labels(row)
    assert_maps_disjoint(features, labels)

    record = {
        "date": str(row.get("date") or row.get("entry_date") or "")[:10],
        "job_id": str(job_id or "").strip(),
        "features": features,
        "labels": labels,
    }
    path = entry_features_path(job_id)
    line = json.dumps(record, default=str, separators=(",", ":"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY FEATURES] write failed {path}: {e}", level="warning")
        return None
    return record


def assert_entry_features_file_ok(job_id: str, *, sample_limit: int = 500) -> dict[str, Any]:
    """
    Completion check: catalog disjoint + every sampled JSONL row has disjoint maps
    and only known feature/label keys.
    """
    assert_feature_label_keys_disjoint()
    path = entry_features_path(job_id)
    stats = {
        "path": str(path),
        "rows": 0,
        "checked": 0,
        "ok": True,
    }
    if not path.is_file():
        log(
            f"[ENTRY FEATURES] no file at completion for job {job_id} ({path})",
            level="info",
        )
        return stats

    checked = 0
    rows = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows += 1
            if checked >= sample_limit:
                continue
            obj = json.loads(line)
            feats = obj.get("features") or {}
            labs = obj.get("labels") or {}
            if not isinstance(feats, dict) or not isinstance(labs, dict):
                raise AssertionError(f"entry features row {rows}: features/labels not maps")
            assert_maps_disjoint(feats, labs)
            unknown_f = set(feats.keys()) - _FEATURE_KEY_SET
            unknown_l = set(labs.keys()) - _LABEL_KEY_SET
            if unknown_f or unknown_l:
                raise AssertionError(
                    f"entry features row {rows}: unknown keys "
                    f"features={sorted(unknown_f)} labels={sorted(unknown_l)}"
                )
            # Labels must not leak into features via accidental copy.
            for lk in ENTRY_LABEL_KEYS:
                if lk in feats:
                    raise AssertionError(
                        f"entry features row {rows}: label key {lk!r} found in features"
                    )
            checked += 1

    stats["rows"] = rows
    stats["checked"] = checked
    log(
        f"[ENTRY FEATURES] completion ok job={job_id}: {rows} rows, "
        f"checked {checked}, path={path}",
        level="info",
    )
    return stats
