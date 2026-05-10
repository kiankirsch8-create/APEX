"""APEX FastAPI backend — in-memory results store.

Results are kept in a Python global (`LATEST_RESULTS`) and a small bounded
in-memory history dict. We do NOT read or write `results/latest.json` or
`results/daily_picks_<DATE>.json` from this module — Railway resets the
container filesystem on every deploy, so any persistence here would be
lost. Instead, the server runs a fresh scan in the background on startup,
and any client can hit POST `/api/run-scan` to refresh the in-memory data
on demand.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import analyzer
import screener_big_players
import screener_small_caps
from scorer import calculate
from scheduler import run_daily_apex
from utils import CONFIG_DIR, load_json, log, save_json, today_str, utcnow_iso

app = FastAPI(
    title="APEX Stock Discovery Engine",
    version="1.1.0",
    description=(
        "Autonomous AI-powered stock discovery engine. The machine finds the "
        "highest-probability explosive opportunities every morning across "
        "speculative small caps and undervalued mega caps. Results are kept "
        "in memory so the service is fully Railway-compatible."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# In-memory results store — Railway-friendly. The filesystem is reset on
# every deploy, so we never persist results to disk from the API layer.
# ---------------------------------------------------------------------------
LATEST_RESULTS: dict = {}
RESULTS_HISTORY: dict[str, dict] = {}      # date_str -> payload, bounded
_HISTORY_MAX_ENTRIES = 30
_SCAN_LOCK = asyncio.Lock()
_SCAN_IN_PROGRESS = False


async def _run_and_store_scan(budget: float) -> dict:
    """Run one full APEX scan and update the in-memory globals.

    Serialised by ``_SCAN_LOCK`` — concurrent callers wait for the running
    scan and then run their own, never trampling each other's writes.
    """
    global LATEST_RESULTS, _SCAN_IN_PROGRESS
    async with _SCAN_LOCK:
        _SCAN_IN_PROGRESS = True
        try:
            payload = await run_daily_apex(total_budget_usd=budget, persist=False)
        finally:
            _SCAN_IN_PROGRESS = False
        LATEST_RESULTS = payload
        date_str = payload.get("date") or today_str()
        RESULTS_HISTORY[date_str] = payload
        # Bound history to the most-recent N entries (FIFO by date string)
        if len(RESULTS_HISTORY) > _HISTORY_MAX_ENTRIES:
            for stale in sorted(RESULTS_HISTORY.keys())[: -_HISTORY_MAX_ENTRIES]:
                RESULTS_HISTORY.pop(stale, None)
        return payload


# ---------------------------------------------------------------------------
# Startup — kick off the first scan in a background task so the HTTP
# server starts accepting requests immediately. Results are typically ready
# within ~2 minutes; until then ``/api/latest`` returns 503.
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_scan() -> None:
    log("[Startup] Spawning initial APEX scan in background")

    async def _initial() -> None:
        try:
            await _run_and_store_scan(_stored_budget())
            log(
                f"[Startup] Initial scan complete — "
                f"{LATEST_RESULTS.get('total_picks', 0)} picks in memory"
            )
        except Exception as e:  # noqa: BLE001 — never let a scan failure break boot
            log(f"[Startup] Initial APEX scan failed: {e}", "error")

    asyncio.create_task(_initial())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BudgetIn(BaseModel):
    total_budget_usd: float = Field(..., gt=0, description="User's total investment budget in USD")


class AnalyzeIn(BaseModel):
    section: str | None = Field(default=None, description="SMALL_CAP or BIG_PLAYER (optional)")
    total_budget_usd: float | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Latest / single pick / history (all in-memory)
# ---------------------------------------------------------------------------
@app.get("/api/latest")
async def get_latest() -> dict:
    if not LATEST_RESULTS:
        raise HTTPException(
            status_code=503,
            detail=(
                "APEX is still warming up — the first scan after deploy is "
                "running in the background. Try again in ~2 minutes, or trigger "
                "a fresh scan with POST /api/run-scan."
            ),
        )
    return LATEST_RESULTS


@app.get("/api/pick/{ticker}")
async def get_pick(ticker: str) -> dict:
    if not LATEST_RESULTS:
        raise HTTPException(status_code=503, detail="No results available yet — initial scan still running.")
    ticker = ticker.upper()
    for r in LATEST_RESULTS.get("all_picks", []) or []:
        if (r.get("ticker") or "").upper() == ticker:
            return r
    raise HTTPException(status_code=404, detail=f"{ticker} not in today's picks.")


@app.get("/api/history")
async def list_history() -> dict:
    dates = sorted(RESULTS_HISTORY.keys())
    return {"dates": dates, "count": len(dates), "in_memory_only": True}


@app.get("/api/history/{date}")
async def get_history(date: str) -> dict:
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from e
    data = RESULTS_HISTORY.get(date)
    if not data:
        raise HTTPException(status_code=404, detail=f"No in-memory results for {date}.")
    return data


# ---------------------------------------------------------------------------
# On-demand single-ticker analysis (does not touch LATEST_RESULTS)
# ---------------------------------------------------------------------------
@app.post("/api/analyze/{ticker}")
async def analyze_ticker(
    ticker: str,
    section: str = Query(default="SMALL_CAP", description="SMALL_CAP or BIG_PLAYER"),
    total_budget_usd: float | None = Query(default=None),
) -> dict:
    section = section.upper()
    if section not in {"SMALL_CAP", "BIG_PLAYER"}:
        raise HTTPException(status_code=400, detail="section must be SMALL_CAP or BIG_PLAYER")

    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    ticker = ticker.upper()
    market_data = await _build_market_data(ticker, section)
    triggered = market_data.get("triggered_signals", [])

    report = await analyzer.analyze_stock(
        ticker=ticker,
        section=section,
        triggered_signals=triggered,
        market_data=market_data,
        total_budget_usd=budget,
    )
    return calculate(report)


async def _build_market_data(ticker: str, section: str) -> dict:
    """Fetch + score a single ticker the same way the daily screeners do."""
    from market_data import PolygonClient, build_indicator_pack

    async with PolygonClient() as poly:
        details = await poly.ticker_details(ticker)
        snap = await poly.snapshot(ticker)
        aggs = await poly.aggs(ticker, days=300)
        financials = await poly.financials(ticker, limit=8)

    if not details:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found at data provider.")

    indicators = build_indicator_pack(aggs)
    row = {"T": ticker, "c": indicators.get("current_price"), "v": indicators.get("volume")}

    if section == "SMALL_CAP":
        signals, score = screener_small_caps._score_small_cap(row, indicators, details, snap, financials)
    else:
        signals, score = screener_big_players._score_big_player(row, indicators, details, snap, financials)

    return {
        "ticker": ticker,
        "section": section,
        "details": details,
        "snapshot": snap,
        "aggs": aggs,
        "indicators": indicators,
        "financials": financials,
        "triggered_signals": signals,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict:
    today = today_str()
    today_complete = bool(LATEST_RESULTS and LATEST_RESULTS.get("date") == today)
    return {
        "scan_in_progress": _SCAN_IN_PROGRESS,
        "results_loaded": bool(LATEST_RESULTS),
        "today_run_complete": today_complete,
        "last_run_at": LATEST_RESULTS.get("generated_at"),
        "total_picks_today": LATEST_RESULTS.get("total_picks", 0) if today_complete else 0,
        "small_cap_count": LATEST_RESULTS.get("small_cap_count", 0) if today_complete else 0,
        "big_player_count": LATEST_RESULTS.get("big_player_count", 0) if today_complete else 0,
        "history_dates_in_memory": sorted(RESULTS_HISTORY.keys()),
        "next_run_at": _next_seven_am_iso(),
        "current_budget_usd": _stored_budget(),
        "storage": "in-memory (Railway-safe)",
    }


def _next_seven_am_iso() -> str:
    now = datetime.now()
    nxt = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= nxt:
        nxt = nxt + timedelta(days=1)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# Manual scan triggers — both update LATEST_RESULTS in memory
# ---------------------------------------------------------------------------
@app.post("/api/run-scan")
async def run_scan(total_budget_usd: float | None = Query(default=None)) -> dict:
    """Trigger a real, blocking APEX scan and update LATEST_RESULTS in memory.

    The full pipeline runs synchronously inside this request — both
    screeners + analyzer + scorer end-to-end — and the freshly-built
    payload is returned as JSON. No filesystem writes.
    """
    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    payload = await _run_and_store_scan(budget)
    return payload


@app.post("/api/run")
async def trigger_run(total_budget_usd: float | None = Query(default=None)) -> dict:
    """Backwards-compatible alias for ``/api/run-scan``."""
    return await run_scan(total_budget_usd)


# ---------------------------------------------------------------------------
# Budget management — config/budget.json. This is user configuration, not
# results data, so it stays on disk inside the container (Railway will reset
# it on deploy; clients can re-POST it as needed).
# ---------------------------------------------------------------------------
@app.get("/api/budget")
async def get_budget() -> dict:
    return {"total_budget_usd": _stored_budget(), "updated_at": _budget_updated_at()}


@app.post("/api/budget")
async def set_budget(payload: BudgetIn) -> dict:
    save_json(
        CONFIG_DIR / "budget.json",
        {"total_budget_usd": payload.total_budget_usd, "updated_at": utcnow_iso()},
    )
    return {"total_budget_usd": payload.total_budget_usd, "updated_at": utcnow_iso()}


def _stored_budget() -> float:
    cfg = load_json(CONFIG_DIR / "budget.json")
    if isinstance(cfg, dict) and cfg.get("total_budget_usd"):
        try:
            return float(cfg["total_budget_usd"])
        except (TypeError, ValueError):
            pass
    return float(os.environ.get("DEFAULT_BUDGET_USD", 10_000))


def _budget_updated_at() -> str | None:
    cfg = load_json(CONFIG_DIR / "budget.json")
    return cfg.get("updated_at") if isinstance(cfg, dict) else None


# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> dict:
    return {
        "service": "APEX Stock Discovery Engine",
        "version": "1.1.0",
        "storage": "in-memory (Railway-safe)",
        "endpoints": [
            "GET /health",
            "GET /api/latest",
            "GET /api/pick/{ticker}",
            "GET /api/history",
            "GET /api/history/{date}",
            "POST /api/analyze/{ticker}?section=SMALL_CAP|BIG_PLAYER",
            "GET /api/status",
            "POST /api/run-scan",
            "POST /api/run  (alias of /api/run-scan)",
            "GET /api/budget",
            "POST /api/budget",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
