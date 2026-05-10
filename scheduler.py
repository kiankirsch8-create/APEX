"""APEX daily pipeline scheduler.

Wires the screener -> analyzer -> scorer pipeline together and runs it every
morning at 07:00 local time. Outputs are persisted to ``results/`` and a
running log is appended to ``logs/apex.log``.

Usage
-----

Manual one-shot run (for testing or on-demand triggers from the API):

    python scheduler.py --run-now

Long-running daemon (the production mode):

    python scheduler.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

import schedule

from analyzer import analyze_top_candidates
from scorer import score_reports
from screener import run_screener

load_dotenv()

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
LOGS_DIR = ROOT / "logs"
RESULTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

LOG_PATH = LOGS_DIR / "apex.log"

# Configure root logger to write to both stdout and the file log.
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
if not root_logger.handlers:
    file_h = logging.FileHandler(LOG_PATH)
    file_h.setFormatter(formatter)
    root_logger.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(formatter)
    root_logger.addHandler(stream_h)

logger = logging.getLogger("apex.scheduler")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _save_results(picks: List[Dict[str, Any]], generated_at: str) -> Dict[str, Any]:
    payload = {
        "date": _today_str(),
        "generated_at": generated_at,
        "count": len(picks),
        "picks": picks,
    }
    daily_path = RESULTS_DIR / f"daily_picks_{payload['date']}.json"
    latest_path = RESULTS_DIR / "latest.json"
    daily_path.write_text(json.dumps(payload, indent=2, default=str))
    latest_path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Saved %d picks to %s and %s",
                len(picks), daily_path.name, latest_path.name)
    return payload


def run_pipeline(top_n: int = 5,
                 tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """End-to-end APEX run: screener → analyzer → scorer → persist."""
    started = datetime.now(timezone.utc)
    logger.info("=== APEX pipeline START at %s ===", started.isoformat())
    try:
        candidates = run_screener(top_n=top_n, tickers=tickers)
        logger.info("Screener returned %d candidates", len(candidates))
    except Exception as exc:
        logger.exception("Screener failed: %s", exc)
        candidates = []

    if not candidates:
        logger.warning("No candidates to analyze; writing empty result file.")
        return _save_results([], generated_at=started.isoformat())

    try:
        reports = analyze_top_candidates(candidates)
    except Exception as exc:
        logger.exception("Analyzer batch failed: %s", exc)
        reports = []

    try:
        scored = score_reports(reports)
    except Exception as exc:
        logger.exception("Scorer batch failed: %s", exc)
        scored = reports

    payload = _save_results(scored, generated_at=started.isoformat())
    finished = datetime.now(timezone.utc)
    duration = (finished - started).total_seconds()
    logger.info("=== APEX pipeline DONE in %.1fs (picks=%d) ===",
                duration, len(scored))
    return payload


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _scheduled_job() -> None:
    try:
        run_pipeline()
    except Exception:  # pragma: no cover
        logger.error("Unhandled error in scheduled job:\n%s", traceback.format_exc())


def start_daemon(run_time: str = "07:00") -> None:
    """Block forever, executing the pipeline daily at ``run_time`` local time."""
    schedule.every().day.at(run_time).do(_scheduled_job)
    logger.info("APEX scheduler armed: daily run at %s local time.", run_time)
    while True:
        schedule.run_pending()
        time.sleep(20)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="APEX scheduler / pipeline runner.")
    p.add_argument("--run-now", action="store_true",
                   help="Execute the pipeline immediately and exit.")
    p.add_argument("--at", default=os.getenv("APEX_RUN_AT", "07:00"),
                   help="Daily run time HH:MM in local timezone (default 07:00).")
    p.add_argument("--top", type=int, default=5,
                   help="Number of top candidates to analyze (default 5).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.run_now:
        run_pipeline(top_n=args.top)
        return 0
    start_daemon(run_time=args.at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
