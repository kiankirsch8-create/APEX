"""FastAPI backend for the APEX engine.

Exposes today's morning picks, historical results, and an on-demand analysis
trigger so a Lovable (or any) frontend can consume the engine.

All endpoints are JSON. CORS is wide open so a hosted frontend can connect
without proxy configuration.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from analyzer import analyze_ticker
from scheduler import run_pipeline
from scorer import score_report

load_dotenv()

logger = logging.getLogger("apex.api")

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LATEST_PATH = RESULTS_DIR / "latest.json"

PORT = int(os.getenv("PORT", "8000"))

app = FastAPI(
    title="APEX Stock Discovery Engine",
    description=(
        "Autonomous AI stock discovery engine. Every morning APEX scans the "
        "S&P 500 + Nasdaq 100, runs a deep Claude-powered analysis on the "
        "top 5 opportunities, and exposes the results via this API."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text())


def _all_history_dates() -> List[str]:
    out: List[str] = []
    for p in sorted(RESULTS_DIR.glob("daily_picks_*.json")):
        m = re.match(r"daily_picks_(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if m:
            out.append(m.group(1))
    return sorted(out, reverse=True)


def _file_for_date(date: str) -> Path:
    if not DATE_RE.match(date):
        raise HTTPException(status_code=400,
                            detail="date must be YYYY-MM-DD")
    return RESULTS_DIR / f"daily_picks_{date}.json"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "name": "APEX Stock Discovery Engine",
        "version": "1.0.0",
        "endpoints": [
            "/api/latest",
            "/api/pick/{ticker}",
            "/api/history",
            "/api/history/{date}",
            "/api/analyze/{ticker}",
            "/api/status",
        ],
    }


@app.get("/api/latest")
def get_latest() -> Dict[str, Any]:
    """Today's top 5 picks."""
    try:
        return _read_json(LATEST_PATH)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="No picks have been generated yet. Run scheduler.py --run-now.",
        )


@app.get("/api/pick/{ticker}")
def get_pick(ticker: str) -> Dict[str, Any]:
    """Full APEX report for a single ticker from today's results."""
    try:
        latest = _read_json(LATEST_PATH)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No picks generated yet.")

    target = ticker.upper()
    for pick in latest.get("picks", []):
        if (pick.get("ticker") or "").upper() == target:
            return pick
    raise HTTPException(status_code=404,
                        detail=f"{target} not found in today's picks.")


@app.get("/api/history")
def get_history() -> Dict[str, Any]:
    """List all dates for which we have stored daily picks."""
    return {"dates": _all_history_dates()}


@app.get("/api/history/{date}")
def get_history_date(date: str) -> Dict[str, Any]:
    """Picks for a specific date (YYYY-MM-DD)."""
    path = _file_for_date(date)
    try:
        return _read_json(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404,
                            detail=f"No picks stored for {date}.")


@app.post("/api/analyze/{ticker}")
def post_analyze(ticker: str) -> Dict[str, Any]:
    """Manually trigger a fresh APEX analysis on any ticker."""
    target = ticker.upper().strip()
    if not target or not re.match(r"^[A-Z\.\-]{1,10}$", target):
        raise HTTPException(status_code=400, detail="Invalid ticker.")
    try:
        report = analyze_ticker(target)
        return score_report(report)
    except Exception as exc:
        logger.exception("On-demand analysis failed for %s: %s", target, exc)
        raise HTTPException(status_code=500,
                            detail=f"Analysis failed: {exc}")


@app.post("/api/run-now")
def post_run_now() -> Dict[str, Any]:
    """Execute the full daily pipeline on demand. (Bonus convenience hook.)"""
    try:
        return run_pipeline()
    except Exception as exc:
        logger.exception("Manual pipeline run failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/status")
def get_status() -> Dict[str, Any]:
    """Whether today's analysis has run, when, and how many picks were found."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not LATEST_PATH.exists():
        return {
            "ran_today": False,
            "today": today,
            "last_run_date": None,
            "last_run_at": None,
            "pick_count": 0,
            "history_dates": _all_history_dates(),
        }
    payload = _read_json(LATEST_PATH)
    last_date = payload.get("date")
    return {
        "ran_today": last_date == today,
        "today": today,
        "last_run_date": last_date,
        "last_run_at": payload.get("generated_at"),
        "pick_count": payload.get("count", len(payload.get("picks", []))),
        "history_dates": _all_history_dates(),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run("api:app", host="0.0.0.0", port=PORT, reload=False)
