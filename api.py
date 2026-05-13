"""APEX FastAPI backend.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

import analyzer
<<<<<<< HEAD
import backtest_analyzer
=======
import chart_analyzer_v2
>>>>>>> cursor/chart-analyzer-v2-a506
import chart_vision
import continuous_backtester
import portfolio_advisor
import screener_big_players
import screener_small_caps
from market_data import YFClient
from scorer import calculate, enforce_portfolio_cap
from scheduler import run_daily_apex
from utils import CONFIG_DIR, RESULTS_DIR, env, load_json, log, save_json, today_str, utcnow_iso

# ---------------------------------------------------------------------------
# Lifespan — resume continuous backtester when enabled; graceful thread stop.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if continuous_backtester.is_enabled():
        continuous_backtester.start_continuous_backtest()
        log("[Startup] Continuous backtest loop resumed (backtest_enabled.json)")
    yield
    continuous_backtester.shutdown_continuous_backtest()


app = FastAPI(
    title="APEX Stock Discovery Engine",
    version="1.0.0",
    description=(
        "Autonomous AI-powered stock discovery engine. The machine finds the "
        "highest-probability explosive opportunities every morning across "
        "speculative small caps and undervalued mega caps."
    ),
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup — do NOT run scans here. GET /api/latest always reads results/latest.json
# only. Use POST /api/run-scan (or /api/run) to regenerate persisted picks.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BudgetIn(BaseModel):
    budget: float = Field(..., gt=0, description="Total investment budget (USD)")


class AnalyzeIn(BaseModel):
    section: str | None = Field(default=None, description="SMALL_CAP or BIG_PLAYER (optional, auto-detected if omitted)")
    total_budget_usd: float | None = Field(default=None, gt=0)


class PortfolioAddIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    company_name: str | None = None
    entry_price: float = Field(..., gt=0)
    entry_date: str = Field(..., description="YYYY-MM-DD")
    shares: float = Field(..., gt=0)
    invested_amount: float | None = Field(default=None, description="If omitted, shares * entry_price")
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    notes: str | None = None


class ChartAnalyzeIn(BaseModel):
    """Single chart: ``image_base64``. Multi-timeframe: ``images`` (2+ items)."""

    image_base64: str | None = Field(
        default=None,
        description="One chart as data URL or raw base64 (used when images is absent)",
    )
    images: list[str] | None = Field(
        default=None,
        description="Multiple chart images in order (weekly → daily → 4H → 1H when 4 images)",
    )
    ticker: str = Field(..., min_length=1)
    timeframe: str = Field(default="1D", description="Chart interval for single-image mode")
    timeframes: list[str] | None = Field(
        default=None,
        description="Optional labels for multi mode (default 1W, 1D, 4H, 1H)",
    )

    @model_validator(mode="after")
    def _require_image_input(self) -> ChartAnalyzeIn:
        if self.images:
            for i, img in enumerate(self.images):
                if not img or not str(img).strip():
                    raise ValueError(f"images[{i}] is empty")
            return self
        if self.image_base64 and str(self.image_base64).strip():
            return self
        raise ValueError("Provide image_base64 or a non-empty images list")


<<<<<<< HEAD
class BacktestSingleIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    timeframe: str = Field(default="4h")
    date: str = Field(..., description="Analysis date YYYY-MM-DD (point-in-time)")
    forward_candles: int = Field(default=20, ge=1, le=500)


class BacktestSeriesIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    timeframe: str = Field(default="4h")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    step_days: int = Field(default=7, ge=1, le=365)
    forward_candles: int = Field(default=20, ge=1, le=500)


class BacktestContinuousToggleIn(BaseModel):
    """Enable or disable the daemon continuous backtest loop."""

    enabled: bool
=======
class AnalyzeDataIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    timeframe: str = Field(default="4h", description="1h, 4h, 1d, 1w, etc.")
    years: int = Field(default=2, ge=1, le=30, description="History span (capped per yfinance interval)")
>>>>>>> cursor/chart-analyzer-v2-a506


PORTFOLIO_PATH = RESULTS_DIR / "portfolio.json"


def _float_or_none(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_portfolio() -> dict[str, Any]:
    raw = load_json(PORTFOLIO_PATH, default=None)
    if not isinstance(raw, dict):
        return {"updated_at": utcnow_iso(), "positions": []}
    poss = raw.get("positions")
    if not isinstance(poss, list):
        poss = []
    return {"updated_at": raw.get("updated_at") or utcnow_iso(), "positions": poss}


async def _enrich_portfolio_row(yfc: YFClient, p: dict[str, Any]) -> dict[str, Any]:
    t = (p.get("ticker") or "").strip().upper()
    cur: float | None = None
    if t:
        snap = await yfc.snapshot(t)
        if snap:
            cur = _float_or_none((snap.get("day") or {}).get("c"))
    shares = float(p.get("shares") or 0)
    inv = float(p.get("invested_amount") or 0)
    current_value = round(shares * cur, 2) if cur is not None else None
    gain_loss_dollars = round(current_value - inv, 2) if current_value is not None else None
    gain_loss_percentage = (
        round(gain_loss_dollars / inv * 100, 2) if gain_loss_dollars is not None and inv > 0 else None
    )
    base = dict(p)
    base.update(
        {
            "ticker": t,
            "current_price": cur,
            "current_value": current_value,
            "gain_loss_dollars": gain_loss_dollars,
            "gain_loss_percentage": gain_loss_percentage,
        }
    )
    return base


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
    """Return persisted scan results from ``results/latest.json`` only (never runs a scan)."""
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


@app.get("/api/history/picks")
async def history_picks() -> dict:
    """All saved daily scans with live prices vs the price at recommendation time."""
    files = sorted(RESULTS_DIR.glob("daily_picks_*.json"))
    file_rows: list[tuple[str, dict[str, Any]]] = []
    all_tickers: set[str] = set()
    for f in files:
        stem = f.stem.replace("daily_picks_", "")
        if len(stem) != 10:
            continue
        data = load_json(f)
        if not isinstance(data, dict):
            continue
        picks = data.get("all_picks") or []
        if not picks:
            picks = list(data.get("small_cap_picks") or []) + list(data.get("big_player_picks") or [])
        for p in picks:
            t = (p.get("ticker") or "").strip().upper()
            if t:
                all_tickers.add(t)
        file_rows.append((stem, data))

    prices: dict[str, float | None] = {}
    if all_tickers:
        async with YFClient() as yfc:

            async def _px(sym: str) -> tuple[str, float | None]:
                snap = await yfc.snapshot(sym)
                if not snap:
                    return sym, None
                return sym, _float_or_none((snap.get("day") or {}).get("c"))

            pairs = await asyncio.gather(*[_px(t) for t in sorted(all_tickers)])
            prices = dict(pairs)

    fetched_at = utcnow_iso()
    scans: list[dict[str, Any]] = []
    for stem, data in file_rows:
        picks = data.get("all_picks") or []
        if not picks:
            picks = list(data.get("small_cap_picks") or []) + list(data.get("big_player_picks") or [])
        entries: list[dict[str, Any]] = []
        for p in picks:
            t = (p.get("ticker") or "").strip().upper()
            if not t:
                continue
            rec = _float_or_none(p.get("current_price"))
            cur = prices.get(t)
            change_pct: float | None = None
            if rec is not None and rec > 0 and cur is not None:
                change_pct = round((cur - rec) / rec * 100, 2)
            entries.append(
                {
                    "ticker": t,
                    "company_name": p.get("company_name"),
                    "apex_rating": p.get("apex_rating"),
                    "composite_score": p.get("composite_score"),
                    "final_probability_percentage": p.get("final_probability_percentage")
                    or p.get("probability_percentage"),
                    "recommended_price": rec,
                    "current_price": cur,
                    "change_vs_recommended_pct": change_pct,
                }
            )
        scans.append({"date": stem, "picks": entries, "total_picks": len(entries)})

    return {
        "scans": scans,
        "latest_prices_fetched_at": fetched_at,
        "scan_count": len(scans),
    }


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
    from market_data import YFClient, build_indicator_pack

    async with YFClient() as yfc:
        details = await yfc.ticker_details(ticker)
        snap = await yfc.snapshot(ticker)
        aggs = await yfc.aggs(ticker, days=300)
        financials = await yfc.financials(ticker, limit=8)

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
        "budget": _stored_budget(),
        "current_budget_usd": _stored_budget(),
    }


def _next_seven_am_iso() -> str:
    now = datetime.now()
    nxt = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if now >= nxt:
        nxt = nxt + timedelta(days=1)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# Manual run triggers
# ---------------------------------------------------------------------------
@app.post("/api/run")
async def trigger_run(total_budget_usd: float | None = Query(default=None)) -> dict:
    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    payload = await run_daily_apex(total_budget_usd=budget)
    return payload


@app.api_route("/api/run-scan", methods=["GET", "POST"])
async def run_scan(total_budget_usd: float | None = Query(default=None)) -> dict:
    """Trigger the full APEX scheduler pipeline on demand.

    Behaviour:
    1. Runs both screeners + analyzer + scorer end-to-end via
       ``run_daily_apex`` (the same code path the 07:00 daily job uses).
    2. Persists the results to ``results/latest.json`` and
       ``results/daily_picks_<DATE>.json`` on the local filesystem (Railway
       container disk).
    3. Returns the freshly-built payload as JSON so the caller can render
       the picks immediately without a second round-trip to ``/api/latest``.

    Accepts both ``GET`` and ``POST`` so it can be triggered easily from a
    browser tab while testing on Railway.
    """
    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    payload = await run_daily_apex(total_budget_usd=budget)
    return payload


# ---------------------------------------------------------------------------
# Budget — persisted in results/budget.json (survives deploys with results volume)
# ---------------------------------------------------------------------------
BUDGET_PATH = RESULTS_DIR / "budget.json"


def _migrate_legacy_budget_file() -> None:
    if BUDGET_PATH.exists():
        return
    legacy = load_json(CONFIG_DIR / "budget.json", default={})
    if isinstance(legacy, dict) and legacy.get("total_budget_usd") is not None:
        try:
            v = float(legacy["total_budget_usd"])
        except (TypeError, ValueError):
            return
        save_json(
            BUDGET_PATH,
            {
                "budget": v,
                "updated_at": legacy.get("updated_at") or utcnow_iso(),
                "migrated_from": "config/budget.json",
            },
        )


@app.get("/api/budget")
async def get_budget() -> dict:
    return {"budget": _stored_budget()}


@app.post("/api/budget")
async def set_budget(payload: BudgetIn) -> dict:
    save_json(BUDGET_PATH, {"budget": float(payload.budget), "updated_at": utcnow_iso()})
    return {"budget": float(payload.budget), "saved": True}


def _stored_budget() -> float:
    _migrate_legacy_budget_file()
    cfg = load_json(BUDGET_PATH, default={})
    if isinstance(cfg, dict):
        if cfg.get("budget") is not None:
            try:
                return float(cfg["budget"])
            except (TypeError, ValueError):
                pass
        if cfg.get("total_budget_usd") is not None:
            try:
                return float(cfg["total_budget_usd"])
            except (TypeError, ValueError):
                pass
    return float(os.environ.get("DEFAULT_BUDGET_USD", 10_000))


def _budget_updated_at() -> str | None:
    cfg = load_json(BUDGET_PATH, default={})
    return cfg.get("updated_at") if isinstance(cfg, dict) else None


def _chart_data_sync(ticker: str) -> dict[str, Any]:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="90d")
        if hist is None or hist.empty:
            return {
                "ticker": ticker,
                "historical": [],
                "predicted_trend": [],
                "trend_direction": "NEUTRAL",
                "trend_strength": 0.0,
            }
        historical: list[dict[str, Any]] = []
        for idx, row in hist.iterrows():
            if hasattr(idx, "strftime"):
                ds = idx.strftime("%Y-%m-%d")
            else:
                ds = str(idx)[:10]
            historical.append({"date": ds, "close": round(float(row["Close"]), 2)})
        closes = [h["close"] for h in historical]
        if len(closes) >= 2:
            slope = (closes[-1] - closes[0]) / len(closes)
            last_date = datetime.strptime(historical[-1]["date"], "%Y-%m-%d")
            predicted: list[dict[str, Any]] = []
            for i in range(1, 31):
                pred_date = last_date + timedelta(days=i)
                pred_price = round(closes[-1] + slope * i, 2)
                predicted.append({"date": str(pred_date.date()), "predicted_close": pred_price})
            trend = "UP" if slope > 0 else "DOWN"
            base = closes[0] if closes[0] else 1e-6
            strength = min(100.0, abs(slope / base * 100 * 30))
        else:
            predicted = []
            trend = "NEUTRAL"
            strength = 0.0
        return {
            "ticker": ticker,
            "historical": historical,
            "predicted_trend": predicted,
            "trend_direction": trend,
            "trend_strength": round(strength, 1),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ticker": ticker,
            "historical": [],
            "predicted_trend": [],
            "trend_direction": "UNKNOWN",
            "trend_strength": 0.0,
            "error": str(e),
        }


@app.get("/api/chart/{ticker}")
async def get_chart(ticker: str) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="Invalid ticker")
    return await asyncio.to_thread(_chart_data_sync, sym)


@app.get("/api/portfolio-advice")
async def portfolio_advice_get(budget: float | None = Query(default=None)) -> dict[str, Any]:
    b = float(budget) if budget is not None else _stored_budget()
    try:
        return await portfolio_advisor.get_cached_or_regenerate(b)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/api/portfolio-advice/generate")
async def portfolio_advice_generate(payload: BudgetIn) -> dict[str, Any]:
    try:
        return await portfolio_advisor.generate_portfolio_advice(float(payload.budget))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Portfolio — persisted in results/portfolio.json
# ---------------------------------------------------------------------------
@app.get("/api/portfolio")
async def portfolio_get() -> dict:
    data = _load_portfolio()
    positions_in = list(data.get("positions") or [])
    async with YFClient() as yfc:
        enriched = await asyncio.gather(*[_enrich_portfolio_row(yfc, p) for p in positions_in])
    return {
        "updated_at": data.get("updated_at"),
        "positions": list(enriched),
        "position_count": len(enriched),
    }


@app.post("/api/portfolio/add")
async def portfolio_add(body: PortfolioAddIn) -> dict:
    try:
        datetime.strptime(body.entry_date.strip(), "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="entry_date must be YYYY-MM-DD") from e

    ticker = body.ticker.strip().upper()
    invested = body.invested_amount
    if invested is None:
        invested = round(float(body.shares) * float(body.entry_price), 2)
    elif invested <= 0:
        raise HTTPException(status_code=400, detail="invested_amount must be positive when provided")

    company_name = (body.company_name or "").strip()
    if not company_name:
        async with YFClient() as yfc:
            det = await yfc.ticker_details(ticker)
            company_name = ((det or {}).get("name") or ticker).strip()

    entry: dict[str, Any] = {
        "ticker": ticker,
        "company_name": company_name,
        "entry_price": float(body.entry_price),
        "entry_date": body.entry_date.strip(),
        "shares": float(body.shares),
        "invested_amount": float(invested),
        "stop_loss": body.stop_loss,
        "take_profit_1": body.take_profit_1,
        "take_profit_2": body.take_profit_2,
        "notes": (body.notes or "").strip() or None,
    }

    data = _load_portfolio()
    positions = [p for p in (data.get("positions") or []) if (p.get("ticker") or "").upper() != ticker]
    positions.append(entry)
    data["positions"] = positions
    data["updated_at"] = utcnow_iso()
    save_json(PORTFOLIO_PATH, data)

    async with YFClient() as yfc:
        live = await _enrich_portfolio_row(yfc, entry)
    return {"ok": True, "position": live}


@app.delete("/api/portfolio/remove/{ticker}")
async def portfolio_remove(ticker: str) -> dict:
    sym = ticker.strip().upper()
    data = _load_portfolio()
    before = list(data.get("positions") or [])
    kept = [p for p in before if (p.get("ticker") or "").upper() != sym]
    if len(kept) == len(before):
        raise HTTPException(status_code=404, detail=f"{sym} not in portfolio.")
    data["positions"] = kept
    data["updated_at"] = utcnow_iso()
    save_json(PORTFOLIO_PATH, data)
    return {"ok": True, "removed": sym}


# ---------------------------------------------------------------------------
# Chart vision
# ---------------------------------------------------------------------------
@app.post("/api/analyze-chart")
async def analyze_chart_endpoint(payload: ChartAnalyzeIn) -> dict[str, Any]:
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        if payload.images is not None and len(payload.images) > 1:
            tfs = payload.timeframes or ["1W", "1D", "4H", "1H"]
            return await asyncio.to_thread(
                chart_vision.analyze_multi_chart,
                payload.images,
                payload.ticker,
                tfs,
            )
        single_img = (
            payload.images[0]
            if payload.images is not None and len(payload.images) == 1
            else (payload.image_base64 or "")
        )
        return await asyncio.to_thread(
            chart_vision.analyze_chart,
            single_img,
            payload.ticker,
            payload.timeframe,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


# ---------------------------------------------------------------------------
<<<<<<< HEAD
# Historical backtests (yfinance + pandas-ta + Claude; results persisted)
# ---------------------------------------------------------------------------
@app.post("/api/backtest/single")
async def backtest_single(body: BacktestSingleIn) -> dict[str, Any]:
    """Point-in-time analysis vs next ``forward_candles`` bars; appends to ``results/backtest_history.json``."""
=======
# Data-driven chart / SMC analysis (yfinance + pandas-ta, no image)
# ---------------------------------------------------------------------------
@app.post("/api/analyze-data")
async def analyze_data_post(body: AnalyzeDataIn) -> dict[str, Any]:
>>>>>>> cursor/chart-analyzer-v2-a506
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
<<<<<<< HEAD
            backtest_analyzer.analyze_at_date,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.date.strip(),
            body.forward_candles,
=======
            chart_analyzer_v2.analyze_ticker_full,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.years,
>>>>>>> cursor/chart-analyzer-v2-a506
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


<<<<<<< HEAD
@app.post("/api/backtest/series")
async def backtest_series(body: BacktestSeriesIn) -> dict[str, Any]:
    """
    Grid of backtests from ``start_date`` to ``end_date`` (one Claude call per step).

    **Warning:** wide date ranges can take many minutes and many API calls (e.g. tens of minutes).
    Each successful date is appended to ``results/backtest_history.json``.
    """
=======
@app.get("/api/analyze-data/{ticker}")
async def analyze_data_get(
    ticker: str,
    timeframe: str = Query("4h", description="1h, 4h, 1d, 1w, etc."),
    years: int = Query(2, ge=1, le=30),
) -> dict[str, Any]:
>>>>>>> cursor/chart-analyzer-v2-a506
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
<<<<<<< HEAD
            backtest_analyzer.run_backtest_series,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.start_date.strip(),
            body.end_date.strip(),
            body.step_days,
            body.forward_candles,
=======
            chart_analyzer_v2.analyze_ticker_full,
            ticker.strip(),
            timeframe.strip(),
            years,
>>>>>>> cursor/chart-analyzer-v2-a506
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


<<<<<<< HEAD
@app.get("/api/backtest/history")
async def backtest_history() -> dict[str, Any]:
    """All entries saved in ``results/backtest_history.json`` (singles + series runs)."""
    return backtest_analyzer.get_backtest_history()


@app.get("/api/backtest/stats")
async def backtest_stats() -> dict[str, Any]:
    """Rolling stats from ``results/backtest_stats.json`` (continuous backtester)."""
    return continuous_backtester.get_stats()


@app.get("/api/backtest/results")
async def backtest_results(limit: int = Query(50, ge=1, le=5000)) -> dict[str, Any]:
    """Last ``limit`` rows from ``results/backtest_results.json``."""
    return {"limit": limit, "results": continuous_backtester.get_results_slice(limit)}


@app.get("/api/backtest/state")
async def backtest_state() -> dict[str, Any]:
    """Daemon state: current ticker/date, status, counters (``backtest_state.json``)."""
    return continuous_backtester.get_state()


@app.get("/api/backtest/enabled")
async def backtest_enabled() -> dict[str, Any]:
    return {"enabled": continuous_backtester.is_enabled()}


@app.post("/api/backtest/toggle")
async def backtest_toggle(body: BacktestContinuousToggleIn) -> dict[str, Any]:
    """Turn the autonomous continuous backtest loop on or off."""
    if body.enabled:
        started = continuous_backtester.start_continuous_backtest()
        return {"enabled": True, "started": started}
    continuous_backtester.stop_continuous_backtest()
    return {"enabled": False, "stopped": True}


@app.post("/api/backtest/improve-now")
async def backtest_improve_now() -> dict[str, Any]:
    """Kick off a self-improvement Claude pass over accumulated results (background thread)."""
    return continuous_backtester.trigger_improvement_now()


@app.get("/api/backtest/learned")
async def backtest_learned() -> dict[str, Any]:
    """Learned weights / rules from the last improvement cycle (``learned_weights.json``)."""
    return continuous_backtester.get_learned()


@app.get("/api/backtest/improving")
async def backtest_improving() -> dict[str, Any]:
    """Whether an improvement job is currently running (``improving.json``)."""
    return continuous_backtester.get_improving_state()


=======
>>>>>>> cursor/chart-analyzer-v2-a506
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
            "GET /api/history/picks",
            "GET /api/history/{date}",
            "POST /api/analyze/{ticker}?section=SMALL_CAP|BIG_PLAYER",
            "GET /api/portfolio",
            "POST /api/portfolio/add",
            "DELETE /api/portfolio/remove/{ticker}",
            "POST /api/analyze-chart",
<<<<<<< HEAD
            "POST /api/backtest/single",
            "POST /api/backtest/series",
            "GET /api/backtest/history",
            "GET /api/backtest/stats",
            "GET /api/backtest/results",
            "GET /api/backtest/state",
            "GET /api/backtest/enabled",
            "POST /api/backtest/toggle",
            "POST /api/backtest/improve-now",
            "GET /api/backtest/learned",
            "GET /api/backtest/improving",
=======
            "POST /api/analyze-data",
            "GET /api/analyze-data/{ticker}",
>>>>>>> cursor/chart-analyzer-v2-a506
            "GET /api/status",
            "POST /api/run",
            "GET|POST /api/run-scan",
            "GET /api/budget",
            "POST /api/budget",
            "GET /api/chart/{ticker}",
            "GET /api/portfolio-advice",
            "POST /api/portfolio-advice/generate",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)
