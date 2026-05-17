"""APEX FastAPI backend.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

import analyzer
import backtest_analyzer
import chart_analyzer_v2
import chart_vision
import continuous_backtester
import funded_simulator
import intraday_backtester
import news_stream
import portfolio_advisor
import screener_big_players
import screener_small_caps
from market_data import YFClient
from scorer import calculate, enforce_portfolio_cap
from scheduler import run_daily_apex
from utils import CONFIG_DIR, DATA_DIR, RESULTS_DIR, env, load_json, log, save_json, today_str, utcnow_iso

# ---------------------------------------------------------------------------
# Lifespan — graceful shutdown of continuous backtester worker thread.
# Startup resume is handled by ``on_startup_resume_backtester`` below.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    continuous_backtester.shutdown_continuous_backtest()
    try:
        intraday_backtester.stop_intraday_daemon()
    except Exception:  # noqa: BLE001
        pass


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


@app.on_event("startup")
async def on_startup_resume_backtester() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "daily_picks").mkdir(parents=True, exist_ok=True)
        log(f"[Startup] Persist directory: {DATA_DIR}")
        continuous_backtester.log_learned_startup_preview()
        if continuous_backtester.is_enabled():
            continuous_backtester.start_continuous_backtest()
            log("[Startup] Backtester auto-resumed")
        else:
            log("[Startup] Backtester is disabled")
        if os.getenv("BENZINGA_API_KEY"):
            try:
                news_stream.start_news_stream_thread()
                log("[Startup] Benzinga news stream started")
            except Exception as e:  # noqa: BLE001
                log(f"[Startup] Benzinga stream not started: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"[Startup] Backtester error: {e}")


# ---------------------------------------------------------------------------
# Startup — do NOT run scans here. GET /api/latest reads persisted latest.json
# under RESULTS_DIR (e.g. /data on Railway with a volume). Use POST /api/run-scan
# (or /api/run) to regenerate persisted picks.
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


class IntradayToggleIn(BaseModel):
    """Enable or disable the intraday backtest daemon loop."""

    enabled: bool


class AnalyzeDataIn(BaseModel):
    ticker: str = Field(..., min_length=1)
    timeframe: str = Field(default="4h", description="1h, 4h, 1d, 1w, etc.")
    years: int = Field(default=2, ge=1, le=30, description="History span (capped per yfinance interval)")


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
    """Return persisted scan results from ``latest.json`` under RESULTS_DIR only (never runs a scan)."""
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
    2. Persists the results to ``latest.json`` and ``daily_picks_<DATE>.json``
       under RESULTS_DIR (e.g. Railway volume at ``/data``).
    3. Returns the freshly-built payload as JSON so the caller can render
       the picks immediately without a second round-trip to ``/api/latest``.

    Accepts both ``GET`` and ``POST`` so it can be triggered easily from a
    browser tab while testing on Railway.
    """
    budget = float(total_budget_usd) if total_budget_usd else _stored_budget()
    payload = await run_daily_apex(total_budget_usd=budget)
    return payload


# ---------------------------------------------------------------------------
# Budget — persisted as budget.json under RESULTS_DIR (e.g. /data on Railway)
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
# Portfolio — persisted as portfolio.json under RESULTS_DIR
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
# Data-driven chart / SMC analysis (yfinance + pandas-ta, no image)
# ---------------------------------------------------------------------------
@app.post("/api/analyze-data")
async def analyze_data_post(body: AnalyzeDataIn) -> dict[str, Any]:
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
            chart_analyzer_v2.analyze_ticker_full,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.years,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/analyze-data/{ticker}")
async def analyze_data_get(
    ticker: str,
    timeframe: str = Query("4h", description="1h, 4h, 1d, 1w, etc."),
    years: int = Query(2, ge=1, le=30),
) -> dict[str, Any]:
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
            chart_analyzer_v2.analyze_ticker_full,
            ticker.strip(),
            timeframe.strip(),
            years,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Historical backtests (yfinance + pandas-ta + Claude; results persisted)
# ---------------------------------------------------------------------------
@app.post("/api/backtest/single")
async def backtest_single(body: BacktestSingleIn) -> dict[str, Any]:
    """Point-in-time analysis vs next ``forward_candles`` bars; appends to ``backtest_history.json`` under RESULTS_DIR."""
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
            backtest_analyzer.analyze_at_date,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.date.strip(),
            body.forward_candles,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/api/backtest/series")
async def backtest_series(body: BacktestSeriesIn) -> dict[str, Any]:
    """
    Grid of backtests from ``start_date`` to ``end_date`` (one Claude call per step).

    **Warning:** wide date ranges can take many minutes and many API calls (e.g. tens of minutes).
    Each successful date is appended to ``backtest_history.json`` under RESULTS_DIR.
    """
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    try:
        return await asyncio.to_thread(
            backtest_analyzer.run_backtest_series,
            body.ticker.strip(),
            body.timeframe.strip(),
            body.start_date.strip(),
            body.end_date.strip(),
            body.step_days,
            body.forward_candles,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/backtest/history")
async def backtest_history() -> dict[str, Any]:
    """All entries saved in ``backtest_history.json`` under RESULTS_DIR (singles + series runs)."""
    return backtest_analyzer.get_backtest_history()


@app.get("/api/backtest/health")
async def backtest_health():
    try:
        from continuous_backtester import is_enabled

        available = True
        enabled = is_enabled()
    except Exception as e:
        available = False
        enabled = False
    return {
        "available": available,
        "enabled": enabled,
        "status": "ok",
    }


@app.get("/api/backtest/stats")
async def backtest_stats() -> dict[str, Any]:
    """Rolling stats from ``backtest_stats.json`` on the data volume."""
    try:
        data = load_json(continuous_backtester.STATS_FILE, default=None)
        if not data:
            return {
                "total_trades": 0,
                "win_rate_pct": 0,
                "total_pnl_dollars": 0,
                "final_capital": 10000,
                "starting_capital": 10000,
                "winning_trades": 0,
                "losing_trades": 0,
                "capital_curve": [],
                "recent_trades": [],
                "signal_performance": {},
                "timeframe_performance": {},
            }
        return data
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.get("/api/backtest/results")
async def backtest_results(limit: int = Query(50, ge=1, le=5000)) -> dict[str, Any]:
    """Backtest rows from ``backtest_results.json``, newest by date first."""
    try:
        data = load_json(continuous_backtester.RESULTS_FILE, default=None)
        if not data:
            return {"limit": limit, "total": 0, "results": []}
        if not isinstance(data, list):
            return {"limit": limit, "total": 0, "results": []}
        sorted_data = sorted(data, key=lambda x: str(x.get("date", "")), reverse=True)
        return {
            "limit": limit,
            "total": len(sorted_data),
            "results": sorted_data[:limit],
        }
    except Exception as e:  # noqa: BLE001
        return {"limit": limit, "total": 0, "results": [], "error": str(e)}


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
    """Run self-improvement on accumulated results in a worker thread (blocks that thread only)."""
    try:
        results = load_json(continuous_backtester.RESULTS_FILE, default=[]) or []
        if not isinstance(results, list):
            results = []
        trades = [r for r in results if isinstance(r, dict) and not r.get("skipped")]
        completed = [
            r
            for r in results
            if isinstance(r, dict) and not r.get("skipped") and r.get("outcome") in ("WIN", "LOSS")
        ]
        log(
            f"[improve-now] snapshot_rows={len(results)} non_skipped={len(trades)} "
            f"completed_win_loss={len(completed)}",
            level="info",
        )
        if len(completed) < 10:
            return {"error": f"Need 10+ completed WIN/LOSS trades, have {len(completed)}"}

        learned = await asyncio.to_thread(
            lambda: continuous_backtester.run_improvement_cycle(list(results)),
        )
        if learned is None:
            return {
                "error": "Improvement did not complete (see logs and GET /api/backtest/improve-debug)",
            }
        return learned
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.post("/api/backtest/reset-learned")
async def backtest_reset_learned() -> dict[str, Any]:
    """Clear ``learned_weights.json`` to default empty rules (removes overly strict improvement output)."""
    continuous_backtester.reset_learned_rules()
    return {"reset": True, "message": "Learned rules cleared"}


@app.post("/api/backtest/reset-stats")
async def backtest_reset_stats() -> dict[str, Any]:
    """Clear continuous backtest results, stats JSON, and learned weights on ``DATA_DIR``."""
    try:
        continuous_backtester.reset_backtest_stats_files()
        return {"reset": True, "message": "All stats cleared"}
    except Exception as e:  # noqa: BLE001
        log(f"[API] reset-stats error: {e}", level="warning")
        return {"reset": False, "error": str(e)}


@app.get("/api/backtest/improve-debug")
async def backtest_improve_debug() -> dict[str, Any]:
    """Last improvement parse/API failure payload from ``improve_debug.json``."""
    return continuous_backtester.get_improve_debug()


@app.get("/api/backtest/learned/history")
async def backtest_learned_history() -> list[dict[str, Any]]:
    """Reserved for future multi-cycle archives; currently empty (see ``learned_weights.json``)."""
    return continuous_backtester.get_learned_history()


@app.get("/api/backtest/learned")
async def backtest_learned() -> dict[str, Any]:
    """Improvement weights from ``learned_weights.json`` on the data volume."""
    try:
        data = load_json(continuous_backtester.LEARNED_FILE, default=None)
        if data and isinstance(data, dict):
            return data
        return {
            "analysis_summary": "",
            "new_rules": [],
            "reliable_signals": [],
            "unreliable_signals": [],
            "main_loss_reasons": [],
            "recommendation": "",
            "expected_improvement": "",
            "total_trades_analyzed": 0,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.get("/api/backtest/improving")
async def backtest_improving() -> dict[str, Any]:
    """Whether an improvement job is currently running (``improving.json``)."""
    return continuous_backtester.get_improving_state()


def _spawn_chronological_backtest(start_date: str, end_date: str, job_id: str) -> None:
    """Fire-and-forget worker so the HTTP handler returns immediately."""
    threading.Thread(
        target=continuous_backtester.run_chronological_backtest,
        args=(start_date, end_date, job_id),
        daemon=True,
        name=f"chrono_{job_id}",
    ).start()


@app.post("/api/chrono/start")
async def start_chrono_backtest(
    background_tasks: BackgroundTasks,
    start_date: str = Query(default=continuous_backtester.CHRONO_START_DATE),
    end_date: str = Query(default=continuous_backtester.CHRONO_END_DATE),
) -> dict[str, Any]:
    """Start a walk-forward chronological backtest (runs in a background thread)."""
    if not env("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured.")
    prev_active = continuous_backtester.get_active_chrono()
    prev_job_id: str | None = None
    if isinstance(prev_active, dict):
        raw = prev_active.get("job_id")
        if raw is not None and str(raw).strip():
            prev_job_id = str(raw).strip()
            continuous_backtester.request_chrono_stop(prev_job_id)
            log(f"[Chrono API] Requested stop for previous job {prev_job_id} before starting new run")

    job_id = str(uuid.uuid4())[:8]
    background_tasks.add_task(
        _spawn_chronological_backtest,
        start_date.strip(),
        end_date.strip(),
        job_id,
    )
    out: dict[str, Any] = {
        "job_id": job_id,
        "status": "started",
        "start_date": start_date,
        "end_date": end_date,
    }
    if prev_job_id:
        out["previous_job_stop_requested"] = prev_job_id
    return out


@app.get("/api/chrono/live")
async def get_chrono_live() -> dict[str, Any]:
    """Current scan cursor for the chrono engine (poll ~3s in UIs)."""
    return continuous_backtester.CHRONO_LIVE_STATUS.copy()


@app.post("/api/chrono/{job_id}/stop")
async def stop_chrono_backtest(job_id: str) -> dict[str, Any]:
    continuous_backtester.request_chrono_stop(job_id.strip())
    return {"job_id": job_id.strip(), "status": "stop_requested"}


@app.get("/api/chrono/active")
async def get_active_chrono_job() -> dict[str, Any]:
    """Return the persisted active chrono job (if any) plus latest progress from disk."""
    active = continuous_backtester.get_active_chrono()
    live = continuous_backtester.CHRONO_LIVE_STATUS.copy()
    if not active:
        return {"active": False, "live_status": live}
    job_id = str(active.get("job_id", "") or "").strip()
    if not job_id:
        return {"active": False, "live_status": live}
    chrono_file = continuous_backtester.chrono_results_path(job_id)
    if chrono_file.is_file():
        data = load_json(chrono_file, default=None)
        if isinstance(data, dict):
            return {
                "active": True,
                "job_id": job_id,
                "current_date": data.get("current_date"),
                "capital": data.get("capital"),
                "status": data.get("status"),
                "days_processed": data.get("days_processed", 0),
                "daily_pnl": data.get("daily_pnl", []),
                "live_status": live,
            }
    return {"active": True, "job_id": job_id, "current_date": None, "live_status": live}


@app.get("/api/chrono/{job_id}/daily")
async def get_chrono_daily(job_id: str) -> dict[str, Any]:
    """Daily P&L series and summary for equity-curve charts."""
    chrono_path = continuous_backtester.chrono_results_path(job_id)
    if not chrono_path.is_file():
        return {"error": "Job not found"}
    data = load_json(chrono_path, default=None)
    if not isinstance(data, dict):
        return {"error": "Job not found"}
    return {
        "job_id": job_id,
        "current_date": data.get("current_date"),
        "daily_pnl": data.get("daily_pnl", []),
        "summary": data.get("summary", {}),
        "capital": data.get("capital"),
        "status": data.get("status"),
        "days_processed": data.get("days_processed", 0),
        "live_status": continuous_backtester.CHRONO_LIVE_STATUS.copy(),
    }


@app.get("/api/chrono/{job_id}/trades")
async def get_chrono_trades(
    job_id: str,
    limit: int = Query(default=500, ge=1, le=10_000),
) -> dict[str, Any]:
    """Chrono job trades and skips only (not mixed with rolling /api/results)."""
    jid = job_id.strip()
    chrono_path = continuous_backtester.chrono_results_path(jid)
    if not chrono_path.is_file():
        return {"error": "Job not found", "results": []}
    data = load_json(chrono_path, default=None)
    if not isinstance(data, dict):
        return {"error": "Job not found", "results": []}
    trades = list(data.get("all_trades") or []) + list(data.get("skipped") or [])
    trades.sort(key=lambda x: (str(x.get("date", "")), str(x.get("ticker", ""))))
    return {
        "total": len(trades),
        "results": trades[:limit],
        "job_id": jid,
        "start_date": data.get("start_date"),
        "current_date": data.get("current_date"),
        "capital": data.get("capital"),
    }


@app.get("/api/chrono/{job_id}")
async def get_chrono_results(job_id: str) -> dict[str, Any]:
    """Full chronological job payload from the persisted JSON file."""
    chrono_path = continuous_backtester.chrono_results_path(job_id)
    if not chrono_path.is_file():
        return {"error": "Job not found"}
    data = load_json(chrono_path, default=None)
    if not isinstance(data, dict):
        return {"error": "Job not found"}
    return data


@app.get("/api/chrono")
async def list_chrono_jobs() -> dict[str, Any]:
    """List all ``chrono_results_*.json`` jobs under ``DATA_DIR``."""
    jobs: list[dict[str, Any]] = []
    for p in sorted(DATA_DIR.glob("chrono_results_*.json")):
        try:
            data = load_json(p, default=None)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        jobs.append(
            {
                "job_id": data.get("job_id"),
                "status": data.get("status"),
                "start_date": data.get("start_date"),
                "end_date": data.get("end_date"),
                "current_date": data.get("current_date"),
                "capital": data.get("capital"),
                "days_processed": data.get("days_processed", 0),
                "total_trades": len(data.get("all_trades", []) or []),
            }
        )
    jobs.sort(key=lambda x: str(x.get("job_id", "")), reverse=True)
    return {"jobs": jobs}


@app.get("/api/config/sessions")
async def get_session_config_route() -> dict[str, Any]:
    """Session toggles for the rolling backtester (stored on ``DATA_DIR``)."""
    return continuous_backtester.load_session_config()


@app.post("/api/config/sessions")
async def set_session_config_route(config: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Update session toggles; keys: ``asia``, ``london``, ``new_york``, optional ``off_hours``."""
    continuous_backtester.save_session_config(config)
    return {"status": "saved", "config": continuous_backtester.load_session_config()}


@app.get("/api/results")
async def get_results(
    limit: int = Query(500, ge=1, le=20000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paginated rolling backtest rows (Lovable trade list)."""
    all_results = list(continuous_backtester.load_all_results())
    all_results.sort(key=lambda x: str(x.get("date", "") or ""), reverse=True)
    total = len(all_results)
    page = all_results[offset : offset + limit]
    return {"total": total, "limit": limit, "offset": offset, "results": page}


@app.get("/api/patterns")
async def get_patterns() -> dict[str, Any]:
    """Aggregate (ticker, session, strategy, timeframe) buckets with min 5 trades."""
    from collections import defaultdict

    all_results = continuous_backtester.load_all_results()
    completed = [r for r in all_results if r.get("outcome") in ("WIN", "LOSS")]
    if len(completed) < 5:
        return {"patterns": []}

    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in completed:
        ticker = str(t.get("ticker", "") or "")
        session = str(t.get("session", "unknown") or "unknown")
        strategy = str(t.get("strategy_id", "") or "UNKNOWN")
        tf = str(t.get("timeframe", "") or "")
        buckets[(ticker, session, strategy, tf)].append(t)

    strategies = continuous_backtester.STRATEGIES
    patterns: list[dict[str, Any]] = []
    for (ticker, session, strategy, tf), trades in buckets.items():
        if len(trades) < 5:
            continue
        wins = [x for x in trades if x.get("outcome") == "WIN"]
        wr = round(len(wins) / len(trades) * 100, 1)
        avg_pnl = round(
            sum(float(x.get("pnl_dollars", 0) or 0) for x in trades) / len(trades),
            2,
        )
        if len(trades) >= 15 and wr >= 55:
            confidence = "HIGH"
        elif len(trades) >= 8 and wr >= 45:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        srow = strategies.get(strategy) if isinstance(strategies, dict) else None
        strategy_name = (
            str(srow.get("name", strategy)) if isinstance(srow, dict) else str(strategy)
        )
        session_label = {"asia": "Asia", "london": "London", "new_york": "New York"}.get(
            session,
            session,
        )
        patterns.append(
            {
                "pattern": f"{ticker} on {session_label} session ({tf.upper()}) with {strategy_name}",
                "ticker": ticker,
                "session": session,
                "strategy": strategy,
                "timeframe": tf,
                "trades": len(trades),
                "wins": len(wins),
                "win_rate": wr,
                "avg_pnl": avg_pnl,
                "confidence": confidence,
            }
        )

    patterns.sort(
        key=lambda x: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["confidence"]], -x["win_rate"]),
    )
    return {"patterns": patterns[:30]}


def _spawn_funded_sim(
    job_id: str,
    start_date: str,
    starting_balance: float,
    profit_target_pct: float,
    daily_loss_pct: float,
    max_drawdown_pct: float,
    trailing_drawdown: bool,
    num_simulations: int,
) -> None:
    threading.Thread(
        target=funded_simulator.run_funded_simulation,
        kwargs={
            "job_id": job_id,
            "start_date": start_date,
            "starting_balance": starting_balance,
            "profit_target_pct": profit_target_pct,
            "daily_loss_pct": daily_loss_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "trailing_drawdown": trailing_drawdown,
            "num_simulations": num_simulations,
        },
        daemon=True,
        name=f"funded_{job_id}",
    ).start()


@app.get("/api/funded/configs")
async def get_funded_configs() -> dict[str, Any]:
    return {"configs": funded_simulator.FIRM_CONFIGS}


@app.post("/api/funded/simulate")
async def start_funded_simulation(
    background_tasks: BackgroundTasks,
    start_date: str = Query(default="2023-01-01"),
    firm: str = Query(default="stella_one_step"),
    starting_balance: float = Query(default=10000.0),
    profit_target_pct: float = Query(default=10.0),
    daily_loss_pct: float = Query(default=4.0),
    max_drawdown_pct: float = Query(default=8.0),
    trailing_drawdown: bool = Query(default=True),
    num_simulations: int = Query(default=1, ge=1, le=500),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())[:8]
    pt, dl, md, tr = profit_target_pct, daily_loss_pct, max_drawdown_pct, trailing_drawdown
    if firm in funded_simulator.FIRM_CONFIGS:
        cfg = funded_simulator.FIRM_CONFIGS[firm]
        pt = float(cfg["profit_target_pct"])
        dl = float(cfg["daily_loss_pct"])
        md = float(cfg["max_drawdown_pct"])
        tr = bool(cfg["trailing_drawdown"])
    background_tasks.add_task(
        _spawn_funded_sim,
        job_id,
        start_date.strip(),
        float(starting_balance),
        float(pt),
        float(dl),
        float(md),
        bool(tr),
        int(num_simulations),
    )
    return {"job_id": job_id, "status": "started"}


@app.get("/api/funded/{job_id}")
async def get_funded_result(job_id: str) -> dict[str, Any]:
    data = funded_simulator.load_funded(job_id)
    if not data:
        return {"error": "Job not found"}
    return data


@app.get("/api/funded")
async def list_funded_jobs() -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    for fp in sorted(funded_simulator.FUNDED_RESULTS_DIR.glob("funded_*.json")):
        try:
            data = load_json(fp, default=None)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        jobs.append(
            {
                "job_id": data.get("job_id"),
                "status": data.get("status"),
                "num_sims": data.get("num_simulations"),
                "sims_done": data.get("sims_complete", 0),
                "pass_rate": (data.get("summary") or {}).get("pass_rate_pct"),
            }
        )
    jobs.sort(key=lambda x: str(x.get("job_id", "")), reverse=True)
    return {"jobs": jobs}


@app.post("/api/funded/{job_id}/stop")
async def stop_funded_job(job_id: str) -> dict[str, Any]:
    flag = funded_simulator.funded_stop_flag_path(job_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stop", encoding="utf-8")
    return {"status": "stop_requested", "job_id": job_id}


# ---------------------------------------------------------------------------
# Intraday backtester (15m / 30m) + Benzinga news alerts
# ---------------------------------------------------------------------------


@app.get("/api/intraday/results")
async def get_intraday_results(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    try:
        if not intraday_backtester.RESULTS_FILE.exists():
            return {"results": [], "total": 0, "all_including_skips": 0}
        results = load_json(intraday_backtester.RESULTS_FILE, default=[])
        if not isinstance(results, list):
            return {"results": [], "total": 0, "all_including_skips": 0, "error": "invalid results file"}
        trades = [r for r in results if isinstance(r, dict) and not r.get("skipped", True)]
        return {
            "total": len(trades),
            "results": trades[-limit:],
            "all_including_skips": len(results),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.get("/api/intraday/stats")
async def get_intraday_stats() -> dict[str, Any]:
    try:
        if not intraday_backtester.RESULTS_FILE.exists():
            return {
                "total_trades": 0,
                "total_skipped": 0,
                "skip_rate_pct": 0.0,
                "win_rate_pct": 0.0,
                "total_pnl_dollars": 0.0,
                "strategy_performance": {},
                "timeframe_performance": {},
            }
        all_results = load_json(intraday_backtester.RESULTS_FILE, default=[])
        if not isinstance(all_results, list):
            return {"error": "invalid results file"}
        total_all = len(all_results)
        trades = [r for r in all_results if isinstance(r, dict) and not r.get("skipped", True)]
        skipped_n = total_all - len(trades)
        skip_rate = round(skipped_n / max(1, total_all) * 100, 1)

        if not trades:
            return {
                "total_trades": 0,
                "total_skipped": skipped_n,
                "skip_rate_pct": skip_rate,
                "win_rate_pct": 0.0,
                "total_pnl_dollars": 0.0,
                "strategy_performance": {},
                "timeframe_performance": {},
            }

        wins = [t for t in trades if t.get("outcome") == "WIN"]
        total_pnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in trades)

        canonical = {
            "S14_OPENING_RANGE": "S14_OPENING_RANGE_BREAKOUT",
            "S16_HTF_REJECTION": "S16_HTF_LEVEL_REJECTION",
        }

        def norm_sid(raw: Any) -> str:
            s = str(raw or "").strip().upper()
            return canonical.get(s, s)

        strategy_ids = (
            "S13_NEWS_MOMENTUM",
            "S14_OPENING_RANGE_BREAKOUT",
            "S15_VWAP_DEVIATION",
            "S16_HTF_LEVEL_REJECTION",
        )
        strategy_stats: dict[str, Any] = {}
        for sid in strategy_ids:
            s_trades = [t for t in trades if norm_sid(t.get("strategy_id")) == sid]
            if not s_trades:
                continue
            s_wins = [t for t in s_trades if t.get("outcome") == "WIN"]
            strategy_stats[sid] = {
                "total": len(s_trades),
                "wins": len(s_wins),
                "win_rate": round(len(s_wins) / len(s_trades) * 100, 1),
                "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in s_trades), 2),
            }

        timeframe_stats: dict[str, Any] = {}
        for tf in ("15m", "30m"):
            tf_trades = [t for t in trades if str(t.get("timeframe", "")).lower() == tf]
            if not tf_trades:
                continue
            tf_wins = [t for t in tf_trades if t.get("outcome") == "WIN"]
            timeframe_stats[tf] = {
                "total": len(tf_trades),
                "wins": len(tf_wins),
                "win_rate": round(len(tf_wins) / len(tf_trades) * 100, 1),
                "pnl": round(sum(float(t.get("pnl_dollars", 0) or 0) for t in tf_trades), 2),
            }

        return {
            "total_trades": len(trades),
            "total_skipped": skipped_n,
            "skip_rate_pct": skip_rate,
            "win_rate_pct": round(len(wins) / max(1, len(trades)) * 100, 1),
            "total_pnl_dollars": round(total_pnl, 2),
            "strategy_performance": strategy_stats,
            "timeframe_performance": timeframe_stats,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.get("/api/intraday/enabled")
async def intraday_enabled() -> dict[str, Any]:
    return {"enabled": intraday_backtester.is_enabled()}


@app.post("/api/intraday/toggle")
async def intraday_toggle(body: IntradayToggleIn) -> dict[str, Any]:
    intraday_backtester.set_enabled(body.enabled)
    if body.enabled:
        intraday_backtester.ensure_intraday_daemon()
        return {"status": "enabled", "enabled": True}
    return {"status": "disabled", "enabled": False}


@app.get("/api/news/alerts")
async def get_news_alerts(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    try:
        if not news_stream.ALERT_FILE.exists():
            return {"alerts": [], "total": 0}
        alerts = load_json(news_stream.ALERT_FILE, default=[])
        if not isinstance(alerts, list):
            return {"alerts": [], "total": 0, "error": "invalid alerts file"}
        return {"alerts": alerts[-limit:], "total": len(alerts)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


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
            "POST /api/analyze-data",
            "GET /api/analyze-data/{ticker}",
            "POST /api/backtest/single",
            "POST /api/backtest/series",
            "GET /api/backtest/history",
            "GET /api/backtest/health",
            "GET /api/backtest/stats",
            "GET /api/backtest/results",
            "GET /api/backtest/state",
            "GET /api/backtest/enabled",
            "POST /api/backtest/toggle",
            "POST /api/backtest/improve-now",
            "POST /api/backtest/reset-learned",
            "POST /api/backtest/reset-stats",
            "GET /api/backtest/improve-debug",
            "GET /api/backtest/learned",
            "GET /api/backtest/learned/history",
            "GET /api/backtest/improving",
            "GET /api/intraday/results",
            "GET /api/intraday/stats",
            "GET /api/intraday/enabled",
            "POST /api/intraday/toggle",
            "GET /api/news/alerts",
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
