"""APEX Trend Detection Manager v7.3 (FIX 28) — weekly structure + EMA stack."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from macro_manager import IS_BACKTEST

logger = logging.getLogger(__name__)

_TREND_CACHE: dict[str, dict[str, Any]] = {}
_TREND_LOCK = threading.Lock()


def _neutral() -> dict[str, Any]:
    return {
        "trend": "RANGING",
        "strength": 0.0,
        "direction_bias": "NEUTRAL",
        "size_multiplier": 1.0,
        "block_counter_trend": False,
        "ema_stack": "MIXED",
    }


def _yf_close_series(df: pd.DataFrame, *, column: str = "Close") -> pd.Series:
    """Normalize yfinance OHLC (flat or MultiIndex columns) to a float close series."""
    if df is None or getattr(df, "empty", True) or column not in df.columns:
        raise ValueError(f"missing {column}")
    close = df[column].squeeze()
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if hasattr(close, "droplevel") and getattr(close.index, "nlevels", 1) > 1:
        close = close.droplevel(0)
    out = pd.Series(close, dtype=float).dropna()
    if out.empty:
        raise ValueError(f"empty {column}")
    return out


def _classify_weekly_trend(
    up_score: float,
    down_score: float,
    *,
    ema_bull: bool,
    ema_bear: bool,
    ema20: float,
    ema50: float,
) -> dict[str, Any]:
    """Map weekly structure + EMA stack to trend/strength (FIX 28 repair)."""
    ema_stack = "BULLISH" if ema_bull else ("BEARISH" if ema_bear else "MIXED")
    margin = 0.08

    if up_score >= 0.65 and ema_bull:
        strength = round(min(float(up_score), 1.0), 3)
        return {
            "trend": "UPTREND",
            "strength": strength,
            "direction_bias": "LONG",
            "size_multiplier": 1.15 if strength >= 0.75 else 1.05,
            "block_counter_trend": strength >= 0.75,
            "ema_stack": ema_stack,
        }
    if down_score >= 0.65 and ema_bear:
        strength = round(min(float(down_score), 1.0), 3)
        return {
            "trend": "DOWNTREND",
            "strength": strength,
            "direction_bias": "SHORT",
            "size_multiplier": 1.15 if strength >= 0.75 else 1.05,
            "block_counter_trend": strength >= 0.75,
            "ema_stack": ema_stack,
        }

    if up_score >= 0.55 and up_score >= down_score + margin:
        ema_confirm = ema_bull or ema20 > ema50
        strength = round(min(float(up_score) * (1.0 if ema_confirm else 0.85), 0.95), 3)
        return {
            "trend": "UPTREND",
            "strength": max(strength, 0.45),
            "direction_bias": "LONG",
            "size_multiplier": 1.05 if ema_confirm else 1.0,
            "block_counter_trend": False,
            "ema_stack": ema_stack,
        }
    if down_score >= 0.55 and down_score >= up_score + margin:
        ema_confirm = ema_bear or ema20 < ema50
        strength = round(min(float(down_score) * (1.0 if ema_confirm else 0.85), 0.95), 3)
        return {
            "trend": "DOWNTREND",
            "strength": max(strength, 0.45),
            "direction_bias": "SHORT",
            "size_multiplier": 1.05 if ema_confirm else 1.0,
            "block_counter_trend": False,
            "ema_stack": ema_stack,
        }

    dom = max(float(up_score), float(down_score))
    strength = round(max(0.35, dom * 0.70), 3)
    bias = "NEUTRAL"
    if up_score > down_score + 0.05:
        bias = "LONG"
    elif down_score > up_score + 0.05:
        bias = "SHORT"
    return {
        "trend": "RANGING",
        "strength": strength,
        "direction_bias": bias,
        "size_multiplier": 1.0,
        "block_counter_trend": False,
        "ema_stack": ema_stack,
    }


def get_weekly_trend(ticker: str, as_of_date: str | None = None) -> dict[str, Any]:
    """
    Weekly HH/HL vs LH/LL counts + EMA20/50/200 stack vs spot close.
    ``as_of_date`` ISO date tailors the series end in backtest mode.
    """
    try:
        tku = (ticker or "").strip().upper()
        if as_of_date and IS_BACKTEST:
            try:
                end = pd.Timestamp(str(as_of_date)[:10])
            except (TypeError, ValueError):
                end = pd.Timestamp.now(tz=timezone.utc)
        else:
            end = pd.Timestamp.now(tz=timezone.utc)
        start = end - timedelta(weeks=60)
        symbol = f"{tku}=X" if (len(tku) == 6 and tku.isalpha()) else tku

        if IS_BACKTEST:
            from continuous_backtester import safe_yf_fetch

            df = safe_yf_fetch(
                symbol,
                start.date().isoformat(),
                (end.date() + timedelta(days=1)).isoformat(),
                "1wk",
            )
        else:
            import yfinance as yf

            df = yf.download(
                symbol,
                start=start.date(),
                end=(end.date() + timedelta(days=1)).isoformat(),
                interval="1wk",
                progress=False,
                auto_adjust=True,
            )
        if df is None or getattr(df, "empty", True) or len(df) < 12:
            return _neutral()

        close = _yf_close_series(df, column="Close")
        highs = _yf_close_series(df, column="High").tail(12)
        lows = _yf_close_series(df, column="Low").tail(12)
        if len(close) < 12 or len(highs) < 2 or len(lows) < 2:
            return _neutral()

        close = close.loc[: str(end.date())]
        if close.empty:
            return _neutral()

        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        span200 = min(200, len(close))
        ema200 = float(close.ewm(span=span200, adjust=False).mean().iloc[-1])
        current = float(close.iloc[-1])

        hh = hl = lh = ll = 0
        for i in range(1, len(highs)):
            if float(highs.iloc[i]) > float(highs.iloc[i - 1]):
                hh += 1
            if float(lows.iloc[i]) > float(lows.iloc[i - 1]):
                hl += 1
            if float(highs.iloc[i]) < float(highs.iloc[i - 1]):
                lh += 1
            if float(lows.iloc[i]) < float(lows.iloc[i - 1]):
                ll += 1
        n = len(highs) - 1
        up_score = (hh + hl) / (2 * n) if n > 0 else 0.5
        down_score = (lh + ll) / (2 * n) if n > 0 else 0.5

        ema_bull = ema20 > ema50 > ema200 and current > ema200
        ema_bear = ema20 < ema50 < ema200 and current < ema200

        return _classify_weekly_trend(
            up_score,
            down_score,
            ema_bull=ema_bull,
            ema_bear=ema_bear,
            ema20=ema20,
            ema50=ema50,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_manager error %s: %s", ticker, e)
        return _neutral()


def get_trend_cached(ticker: str, as_of_date: str | None = None) -> dict[str, Any]:
    """One fetch per ticker per ISO week."""
    if as_of_date:
        wk = pd.Timestamp(str(as_of_date)[:10]).strftime("%Y-%W")
    else:
        wk = pd.Timestamp.now(tz=timezone.utc).strftime("%Y-%W")
    key = f"{(ticker or '').strip().upper()}_{wk}"
    with _TREND_LOCK:
        if key not in _TREND_CACHE:
            _TREND_CACHE[key] = get_weekly_trend(ticker, as_of_date)
        return dict(_TREND_CACHE[key])


def apply_trend_filter(
    ticker: str,
    direction: str,
    strategy_id: str,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """PROCEED / REDUCE / BLOCK from weekly trend vs trade direction (FIX 28)."""
    d = (direction or "").strip().upper()
    sid = (strategy_id or "").strip().upper()
    trend = get_trend_cached(ticker, as_of_date)
    bias = str(trend.get("direction_bias") or "NEUTRAL")

    # FIX A — three strategies bleed when the weekly trend is a confirmed UPTREND.
    # Historically −0.128%/trade at 31% WR in UPTREND. Block them there only.
    _UPTREND_MISFIT = (
        "M02_MACD_ZERO_CROSS",
        "B01_RANGE_BREAKOUT",
        "B10_WEEKLY_RANGE_BREAK",
    )
    if sid in _UPTREND_MISFIT and str(trend.get("trend")) == "UPTREND":
        return {
            "action": "BLOCK",
            "size_multiplier": 0.0,
            "reason": f"Uptrend misfit blocked: {sid} in weekly UPTREND ({ticker})",
            "trend": trend,
        }

    if sid in ("T01_EMA_PULLBACK", "R01_EXTREME_ZONE_REVERSION"):
        mult = float(trend.get("size_multiplier", 1.0) or 1.0) if bias == d else 1.0
        return {"action": "PROCEED", "size_multiplier": mult, "trend": trend, "reason": ""}

    if bias not in ("NEUTRAL", "") and bias != d:
        if trend.get("block_counter_trend"):
            return {
                "action": "BLOCK",
                "size_multiplier": 0.0,
                "reason": f"Counter-trend blocked: {ticker} weekly={trend.get('trend')}",
                "trend": trend,
            }
        return {
            "action": "REDUCE",
            "size_multiplier": 0.70,
            "reason": f"Counter-trend reduced: {ticker} weekly={trend.get('trend')}",
            "trend": trend,
        }

    if bias == d:
        return {
            "action": "PROCEED",
            "size_multiplier": float(trend.get("size_multiplier", 1.0) or 1.0),
            "trend": trend,
            "reason": "",
        }

    return {"action": "PROCEED", "size_multiplier": 1.0, "trend": trend, "reason": ""}
