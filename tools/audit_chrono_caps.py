#!/usr/bin/env python3
"""
Audit a chrono_results_*.json (and optional trades/skips sidecars) to determine
whether the run had portfolio-construction caps ON or OFF.

Usage:
  python3 tools/audit_chrono_caps.py results/chrono_results_lastrun01.json
  python3 tools/audit_chrono_caps.py /data/chrono_results_lastrun01.json

Signals:
  - Embedded ``portfolio_construction`` block (stamped by restored production builds)
  - File mtime / completed_at vs PR #99 merge (2026-07-11T14:57:52Z)
  - Skip reasons: "Ticker already traded today...", "Currency cap: ...",
    "DIAGNOSTIC R01 block..."
  - Same-ticker multi-TF opens on the same calendar day (caps-off smoking gun)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PR99_MERGED_UTC = datetime(2026, 7, 11, 14, 57, 52, tzinfo=timezone.utc)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def audit(results_path: Path) -> dict[str, Any]:
    data = _load(results_path)
    sidecars_dir = results_path.parent
    job_id = str(data.get("job_id") or results_path.stem.replace("chrono_results_", ""))
    trades = list(data.get("all_trades") or [])
    skips = list(data.get("skipped") or [])
    if not trades:
        trades = _load_jsonl(sidecars_dir / f"chrono_trades_{job_id}.jsonl")
    if not skips:
        skips = _load_jsonl(sidecars_dir / f"chrono_skips_{job_id}.jsonl")

    mtime = datetime.fromtimestamp(results_path.stat().st_mtime, tz=timezone.utc)
    completed = data.get("completed_at") or data.get("finished_at") or data.get("updated_at")
    completed_dt = None
    if completed:
        try:
            completed_dt = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
            if completed_dt.tzinfo is None:
                completed_dt = completed_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            completed_dt = None

    pc = data.get("portfolio_construction") if isinstance(data.get("portfolio_construction"), dict) else {}

    skip_reasons = Counter(str(s.get("skip_reason") or "") for s in skips if isinstance(s, dict))
    n_ticker_day = sum(1 for r, n in skip_reasons.items() if "already traded today" in r.lower())
    n_ccy = sum(1 for r, n in skip_reasons.items() if "currency cap" in r.lower())
    n_r01_diag = sum(1 for r, n in skip_reasons.items() if "diagnostic r01" in r.lower())
    # Expand counts by frequency
    n_ticker_day = sum(
        n for r, n in skip_reasons.items() if "already traded today" in r.lower()
    )
    n_ccy = sum(n for r, n in skip_reasons.items() if "currency cap" in r.lower())
    n_r01_diag = sum(n for r, n in skip_reasons.items() if "diagnostic r01" in r.lower())

    # Same ticker + same day + multiple timeframes among opens → caps-off signal
    by_day_tkr: dict[tuple[str, str], set[str]] = defaultdict(set)
    for t in trades:
        if not isinstance(t, dict):
            continue
        d = str(t.get("date") or "")[:10]
        tk = str(t.get("ticker") or "").upper()
        tf = str(t.get("timeframe") or "").lower()
        if d and tk and tf:
            by_day_tkr[(d, tk)].add(tf)
    multi_tf_days = {k: sorted(v) for k, v in by_day_tkr.items() if len(v) > 1}

    postdates_pr99 = (completed_dt or mtime) >= PR99_MERGED_UTC

    verdict = "UNKNOWN"
    evidence: list[str] = []
    if pc:
        if pc.get("one_trade_per_ticker_per_day") and pc.get("currency_cap_enabled"):
            verdict = "CAPS_ON"
            evidence.append("embedded portfolio_construction says caps on")
        if pc.get("diagnostic_allow_multi_tf_per_day") is True:
            verdict = "CAPS_OFF_CEILING"
            evidence.append("embedded diagnostic_allow_multi_tf_per_day=True")
    if n_r01_diag > 0:
        verdict = "CAPS_OFF_CEILING"
        evidence.append(f"{n_r01_diag} DIAGNOSTIC R01 skips (PR #99 slot-stealer)")
    if n_ticker_day == 0 and n_ccy == 0 and multi_tf_days:
        if verdict == "UNKNOWN":
            verdict = "LIKELY_CAPS_OFF"
        evidence.append(
            f"{len(multi_tf_days)} same-ticker multi-TF calendar days with no ticker-day/currency skips"
        )
    if n_ticker_day > 0 or n_ccy > 0:
        if verdict not in ("CAPS_OFF_CEILING",):
            verdict = "CAPS_ON"
        evidence.append(f"skip hits: ticker_day={n_ticker_day} currency_cap={n_ccy}")
    if postdates_pr99 and verdict == "UNKNOWN":
        evidence.append(
            f"file timestamp {(completed_dt or mtime).isoformat()} postdates PR #99 "
            f"({PR99_MERGED_UTC.isoformat()}); main has had caps OFF since then — "
            "run likely used ceiling config unless branched off pre-#99"
        )
        verdict = "LIKELY_CAPS_OFF_POST_PR99"

    summary = (data.get("summary") or {}) if isinstance(data.get("summary"), dict) else {}
    return {
        "path": str(results_path),
        "job_id": job_id,
        "start_date": data.get("start_date"),
        "end_date": data.get("end_date"),
        "status": data.get("status"),
        "mtime_utc": mtime.isoformat(),
        "completed_at": completed,
        "postdates_pr99_2026_07_11": postdates_pr99,
        "portfolio_construction": pc or None,
        "n_trades": len(trades),
        "n_skips": len(skips),
        "skip_ticker_day": n_ticker_day,
        "skip_currency_cap": n_ccy,
        "skip_diagnostic_r01": n_r01_diag,
        "same_ticker_multi_tf_days": len(multi_tf_days),
        "same_ticker_multi_tf_examples": dict(list(multi_tf_days.items())[:8]),
        "summary": summary,
        "verdict": verdict,
        "evidence": evidence,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results_json", type=Path)
    args = p.parse_args()
    if not args.results_json.is_file():
        print(f"ERROR: file not found: {args.results_json}", file=sys.stderr)
        print(
            "chrono_results_lastrun01.json is gitignored (results/*) and was not "
            "present in this cloud environment. Copy it from the Railway /data "
            "volume or the machine that ran lastrun01, then re-run this auditor.",
            file=sys.stderr,
        )
        return 2
    out = audit(args.results_json)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
