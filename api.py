"""APEX FastAPI backend.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import analyzer
import screener_big_players
import screener_small_caps
from scorer import calculate, enforce_portfolio_cap
from scheduler import run_daily_apex
from utils import CONFIG_DIR, RESULTS_DIR, load_json, save_json, today_str, utcnow_iso

app = FastAPI(
    title="APEX Stock Discovery Engine",
    version="1.0.0",
    description=(
        "Autonomous AI-powered stock discovery engine. The machine finds the "
        "highest-probability explosive opportunities every morning across "
        "speculative small caps and undervalued mega caps."
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
# Models
# ---------------------------------------------------------------------------
class BudgetIn(BaseModel):
    total_budget_usd: float = Field(..., gt=0, description="User's total investment budget in USD")


class AnalyzeIn(BaseModel):
    section: str | None = Field(default=None, description="SMALL_CAP or BIG_PLAYER (optional, auto-detected if omitted)")
    total_budget_usd: float | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Latest / single pick / history
# ---------------------------------------------------------------------------
@app.get("/api/latest")
async def get_latest() -> dict:
    data = load_json(RESULTS_DIR / "latest.json")
    if not data:
        raise HTTPException(status_code=404, detail="No APEX scan has run yet. Trigger one via /api/run.")
    return data


@app.get("/api/pick/{ticker}")
async def get_pick(ticker: str) -> dict:
    data = load_json(RESULTS_DIR / "latest.json")
    if not data:
        raise HTTPException(status_code=404, detail="No latest results available.")
    ticker = ticker.upper()
    for r in data.get("all_picks", []) or []:
        if (r.get("ticker") or "").upper() == ticker:
            return r
    raise HTTPException(status_code=404, detail=f"{ticker} not in today's picks.")


@app.get("/api/history")
async def list_history() -> dict:
    files = sorted(RESULTS_DIR.glob("daily_picks_*.json"))
    dates = []
    for f in files:
        stem = f.stem.replace("daily_picks_", "")
        if len(stem) == 10:
            dates.append(stem)
    return {"dates": dates, "count": len(dates)}


@app.get("/api/history/{date}")
async def get_history(date: str) -> dict:
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from e
    data = load_json(RESULTS_DIR / f"daily_picks_{date}.json")
    if not data:
        raise HTTPException(status_code=404, detail=f"No results for {date}.")
    return data


# ---------------------------------------------------------------------------
# On-demand analysis
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

    # Build screener-style market data so analyzer has full context
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
    """Run the screener's scoring on a single ticker so the analyzer gets
    the same triggered_signals it would in the daily pipeline."""
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
    data = load_json(RESULTS_DIR / "latest.json")
    today = today_str()
    today_complete = bool(data and data.get("date") == today)
    next_run = _next_seven_am_iso()
    return {
        "today_run_complete": today_complete,
        "last_run_at": (data or {}).get("generated_at"),
        "total_picks_today": (data or {}).get("total_picks", 0) if today_complete else 0,
        "small_cap_count": (data or {}).get("small_cap_count", 0) if today_complete else 0,
        "big_player_count": (data or {}).get("big_player_count", 0) if today_complete else 0,
        "next_run_at": next_run,
        "current_budget_usd": _stored_budget(),
    }


def _next_seven_am_iso() -> str:
    now = datetime.now()
    nxt = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= nxt:
        nxt = nxt + timedelta(days=1)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# Manual run trigger
# ---------------------------------------------------------------------------
@app.post("/api/run")
async def trigger_run(total_budget_usd: float | None = Query(default=None)) -> dict:
    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    payload = await run_daily_apex(total_budget_usd=budget)
    return payload


# ---------------------------------------------------------------------------
# Budget management — persisted in config/budget.json
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
        "version": "1.0.0",
        "endpoints": [
            "GET /health",
            "GET /api/latest",
            "GET /api/pick/{ticker}",
            "GET /api/history",
            "GET /api/history/{date}",
            "POST /api/analyze/{ticker}?section=SMALL_CAP|BIG_PLAYER",
            "GET /api/status",
            "POST /api/run",
            "GET /api/budget",
            "POST /api/budget",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
