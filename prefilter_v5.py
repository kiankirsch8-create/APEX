"""APEX v5.0 python pre-filter (subset of 68 strategies with computable rules)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd


def python_prefilter(
    ticker: str,
    timeframe: str,
    price: float,
    ind: dict[str, Any],
    zone_pct: float,
    *,
    analysis_date: str | None = None,
    past: pd.DataFrame | None = None,
) -> tuple[bool, list[tuple[str, str, int]], str]:
    """
    Check strategies with pure Python. Zero API cost.
    Returns (qualifies, strategies, reason).
    Each qualifying strategy: (strategy_id, direction, score)
    """
    tf = timeframe.lower().strip()
    sym_u = (ticker or "").strip().upper()
    qualifying: list[tuple[str, str, int]] = []

    ema20 = float(ind.get("ema20", price) or price)
    ema50 = float(ind.get("ema50", price) or price)
    ema200 = float(ind.get("ema200", price) or price)
    rsi = float(ind.get("rsi", 50) or 50)
    adx = float(ind.get("adx", 20) or 20)
    atr = float(ind.get("atr", price * 0.01) or (price * 0.01))
    macd_hist = float(ind.get("macd_hist", 0) or 0)
    macd_line = float(ind.get("macd_line", 0) or 0)
    macd_sig = float(ind.get("macd_signal", 0) or 0)
    _ = macd_sig
    bb_width = float(ind.get("bb_width", 3.0) or 3.0)
    bb_upper = float(ind.get("bb_upper", price * 1.02) or (price * 1.02))
    bb_lower = float(ind.get("bb_lower", price * 0.98) or (price * 0.98))
    swing_highs = ind.get("swing_highs", []) or []
    swing_lows = ind.get("swing_lows", []) or []

    today = date.today()
    if analysis_date:
        try:
            scan_date = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            scan_date = today
    else:
        scan_date = today
    days_ago = (today - scan_date).days

    if tf in ("15m", "30m") and days_ago > 55:
        return False, [], "15M/30M data unavailable beyond 55 days"

    ema_bull = ema20 > ema200 and ema50 > ema200
    ema_bear = ema20 < ema200 and ema50 < ema200
    dist_ema20 = abs(price - ema20)
    dist_ema50 = abs(price - ema50)
    within_2atr = dist_ema20 <= atr * 2.5 or dist_ema50 <= atr * 2.5
    rsi_room = 25 <= rsi <= 75
    adx_trend = adx >= 12
    ema20_cross_above_50 = ema20 > ema50
    _ = ema20_cross_above_50
    price_above_bb = price > bb_upper
    price_below_bb = price < bb_lower

    if tf in ("4h", "1d", "1w"):
        t01_signals = int(
            sum([bool(ema_bull or ema_bear), within_2atr, rsi_room, adx_trend]),
        )
        if t01_signals >= 2:
            if ema_bull and zone_pct < 50:
                qualifying.append(("T01_EMA_PULLBACK", "LONG", t01_signals))
            if ema_bear and zone_pct > 50:
                qualifying.append(("T01_EMA_PULLBACK", "SHORT", t01_signals))
            if zone_pct < 20:
                qualifying.append(("T01_EMA_PULLBACK", "LONG", t01_signals))
            if zone_pct > 80:
                qualifying.append(("T01_EMA_PULLBACK", "SHORT", t01_signals))

        if ema20 > ema50 and adx > 20 and zone_pct < 60:
            qualifying.append(("T02_EMA_CROSSOVER", "LONG", 2))
        if ema20 < ema50 and adx > 20 and zone_pct > 40:
            qualifying.append(("T02_EMA_CROSSOVER", "SHORT", 2))

        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            hh = swing_highs[-1] > swing_highs[-2]
            hl = swing_lows[-1] > swing_lows[-2]
            ll = swing_lows[-1] < swing_lows[-2]
            lh = swing_highs[-1] < swing_highs[-2]
            if hh and hl and within_2atr:
                qualifying.append(("T03_HH_HL_CONTINUATION", "LONG", 3))
            if ll and lh and within_2atr:
                qualifying.append(("T03_HH_HL_CONTINUATION", "SHORT", 3))

        if tf in ("4h", "1d"):
            if adx > 25 and ema_bull:
                qualifying.append(("T04_ADX_TREND_ENTRY", "LONG", 2))
            if adx > 25 and ema_bear:
                qualifying.append(("T04_ADX_TREND_ENTRY", "SHORT", 2))

        if tf in ("1d", "1w"):
            if ema20 > ema50 > ema200 and within_2atr:
                qualifying.append(("T07_MA_RIBBON_ALIGNMENT", "LONG", 3))
            if ema20 < ema50 < ema200 and within_2atr:
                qualifying.append(("T07_MA_RIBBON_ALIGNMENT", "SHORT", 3))

        dist_200 = abs(price - ema200) / max(atr, 0.0001)
        if dist_200 < 1.5 and adx > 15:
            if price > ema200 and ema_bull:
                qualifying.append(("T10_200EMA_BOUNCE", "LONG", 2))
            if price < ema200 and ema_bear:
                qualifying.append(("T10_200EMA_BOUNCE", "SHORT", 2))

    if tf in ("1d", "1w"):
        r01_long = int(sum([zone_pct <= 20, rsi <= 40, adx <= 45]))
        r01_short = int(sum([zone_pct >= 80, rsi >= 60, adx <= 45]))
        if r01_long >= 2:
            qualifying.append(("R01_EXTREME_ZONE_REVERSION", "LONG", r01_long))
        if r01_short >= 2:
            qualifying.append(("R01_EXTREME_ZONE_REVERSION", "SHORT", r01_short))

    if tf in ("4h", "1d"):
        if zone_pct < 30 and macd_hist > 0 and rsi < 45:
            qualifying.append(("R02_RSI_DIVERGENCE", "LONG", 2))
        if zone_pct > 70 and macd_hist < 0 and rsi > 55:
            qualifying.append(("R02_RSI_DIVERGENCE", "SHORT", 2))

        if price_below_bb and rsi < 35:
            qualifying.append(("R03_BB_EXTREME_TOUCH", "LONG", 2))
        if price_above_bb and rsi > 65:
            qualifying.append(("R03_BB_EXTREME_TOUCH", "SHORT", 2))

        if rsi < 25 and zone_pct < 30:
            qualifying.append(("R04_STOCHASTIC_EXTREME", "LONG", 2))
        if rsi > 75 and zone_pct > 70:
            qualifying.append(("R04_STOCHASTIC_EXTREME", "SHORT", 2))

        if rsi < 20 and zone_pct < 25:
            qualifying.append(("R05_CCI_EXTREME", "LONG", 2))
        if rsi > 80 and zone_pct > 75:
            qualifying.append(("R05_CCI_EXTREME", "SHORT", 2))

    if tf in ("1d", "1w"):
        if zone_pct < 5 and rsi < 35:
            qualifying.append(("R08_MONTHLY_LEVEL_REJECTION", "LONG", 3))
        if zone_pct > 95 and rsi > 65:
            qualifying.append(("R08_MONTHLY_LEVEL_REJECTION", "SHORT", 3))

    if tf == "1d":
        if bb_width < 3.0 and adx < 30:
            if price_above_bb:
                qualifying.append(("B01_RANGE_BREAKOUT", "LONG", 2))
            if price_below_bb:
                qualifying.append(("B01_RANGE_BREAKOUT", "SHORT", 2))

    if tf in ("1d", "4h"):
        if bb_width < 2.0 and adx < 20:
            qualifying.append(("B02_VOLATILITY_COMPRESSION", "BOTH", 2))

    if tf in ("4h", "1d"):
        if bb_width < 2.5 and adx < 25 and 30 < zone_pct < 70:
            qualifying.append(("B06_TRIANGLE_BREAKOUT", "BOTH", 2))

    if tf in ("4h", "1d", "1w"):
        if bb_width < 1.5 and adx < 15:
            qualifying.append(("B07_INSIDE_BAR_BREAKOUT", "BOTH", 2))

    if tf in ("4h", "1d"):
        for level in list(swing_highs) + list(swing_lows):
            if abs(price - level) / max(level, 0.0001) < 0.005:
                if adx > 15:
                    qualifying.append(("B08_KEY_LEVEL_RETEST", "BOTH", 2))
                break

        if 45 < rsi < 55 and adx > 20:
            if ema_bull:
                qualifying.append(("B09_RSI_MOMENTUM_BREAK", "LONG", 2))
            if ema_bear:
                qualifying.append(("B09_RSI_MOMENTUM_BREAK", "SHORT", 2))

        if zone_pct < 30 and macd_hist > 0 and rsi < 45:
            qualifying.append(("M01_MACD_DIVERGENCE", "LONG", 2))
        if zone_pct > 70 and macd_hist < 0 and rsi > 55:
            qualifying.append(("M01_MACD_DIVERGENCE", "SHORT", 2))

        if macd_line > 0 and macd_hist > 0 and ema_bull:
            qualifying.append(("M02_MACD_ZERO_CROSS", "LONG", 2))
        if macd_line < 0 and macd_hist < 0 and ema_bear:
            qualifying.append(("M02_MACD_ZERO_CROSS", "SHORT", 2))

    if tf in ("4h", "1d", "1w"):
        if rsi > 50 and rsi < 70 and ema_bull and within_2atr:
            qualifying.append(("M03_RSI_MOMENTUM_CONTINUATION", "LONG", 2))
        if rsi < 50 and rsi > 30 and ema_bear and within_2atr:
            qualifying.append(("M03_RSI_MOMENTUM_CONTINUATION", "SHORT", 2))

    if tf in ("4h", "1d"):
        if adx > 30 and bb_width > 3.0:
            if ema_bull:
                qualifying.append(("M06_PRICE_ACCELERATION", "LONG", 2))
            if ema_bear:
                qualifying.append(("M06_PRICE_ACCELERATION", "SHORT", 2))

    if tf in ("4h", "1d", "1w"):
        all_swings = list(swing_highs) + list(swing_lows)
        for level in all_swings:
            nearby = [s for s in all_swings if abs(s - level) / max(level, 0.0001) < 0.005]
            if len(nearby) >= 2:
                if abs(price - level) / max(level, 0.0001) < 0.004:
                    qualifying.append(("SMC01_SR_FLIP", "BOTH", 2))
                    break

    if tf in ("4h", "1d"):
        for i, h in enumerate(swing_highs[:-1]):
            for h2 in swing_highs[i + 1 :]:
                if abs(h - h2) / max(h, 0.0001) < 0.003:
                    if price > max(h, h2) and rsi > 60:
                        qualifying.append(("SMC05_EQUAL_HL_HUNT", "SHORT", 2))
                    break
        for i, lo in enumerate(swing_lows[:-1]):
            for l2 in swing_lows[i + 1 :]:
                if abs(lo - l2) / max(lo, 0.0001) < 0.003:
                    if price < min(lo, l2) and rsi < 40:
                        qualifying.append(("SMC05_EQUAL_HL_HUNT", "LONG", 2))
                    break

    if tf in ("4h", "1d", "1w"):
        if zone_pct < 15 and rsi < 40:
            qualifying.append(("SMC09_PREMIUM_DISCOUNT_MSS", "LONG", 3))
        if zone_pct > 85 and rsi > 60:
            qualifying.append(("SMC09_PREMIUM_DISCOUNT_MSS", "SHORT", 3))

    if tf in ("4h", "1d"):
        if len(swing_lows) >= 2 and len(swing_highs) >= 2:
            if swing_lows[-1] > swing_lows[-2] and price > swing_highs[-1]:
                qualifying.append(("SMC10_CHOCH", "LONG", 3))
            if swing_highs[-1] < swing_highs[-2] and price < swing_lows[-1]:
                qualifying.append(("SMC10_CHOCH", "SHORT", 3))

    if tf in ("4h", "1d"):
        if bb_width > 4.0 and adx > 25:
            if ema_bull:
                qualifying.append(("V02_ATR_EXPANSION_ENTRY", "LONG", 2))
            if ema_bear:
                qualifying.append(("V02_ATR_EXPANSION_ENTRY", "SHORT", 2))

        if bb_width < 1.5 and adx < 15:
            qualifying.append(("V03_SQUEEZE_BREAKOUT", "BOTH", 3))

    if tf in ("1d", "1w"):
        if bb_width > 5.0 and zone_pct < 20 and rsi < 40:
            qualifying.append(("V06_VOLATILITY_MEAN_REVERSION", "LONG", 2))
        if bb_width > 5.0 and zone_pct > 80 and rsi > 60:
            qualifying.append(("V06_VOLATILITY_MEAN_REVERSION", "SHORT", 2))

    if tf in ("15m", "30m") and days_ago <= 55:
        if rsi <= 30:
            qualifying.append(("I01_VWAP_DEVIATION_SCALP", "LONG", 2))
        if rsi >= 70:
            qualifying.append(("I01_VWAP_DEVIATION_SCALP", "SHORT", 2))

        for level in list(swing_highs) + list(swing_lows):
            if abs(price - level) / max(level, 0.0001) < 0.002:
                qualifying.append(("I02_HTF_LEVEL_REJECTION", "BOTH", 2))
                break

        if adx > 15 and (price_above_bb or price_below_bb):
            qualifying.append(("B05_OPENING_RANGE_BREAKOUT", "BOTH", 2))

        if rsi <= 35 or rsi >= 65:
            qualifying.append(("M08_NEWS_MOMENTUM", "BOTH", 2))

        now_utc = datetime.now(timezone.utc)
        if 7 <= now_utc.hour < 10:
            london_pairs = ["EURUSD", "GBPUSD", "EURGBP", "GBPJPY", "EURJPY"]
            if sym_u in london_pairs and adx > 12:
                qualifying.append(("B03_LONDON_OPEN_BREAKOUT", "BOTH", 2))

        if 12 <= now_utc.hour < 15:
            ny_pairs = ["EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDMXN"]
            if sym_u in ny_pairs and adx > 15:
                qualifying.append(("B04_NY_OPEN_BREAKOUT", "BOTH", 2))

    seen: set[str] = set()
    unique: list[tuple[str, str, int]] = []
    for s in qualifying:
        key = f"{s[0]}_{s[1]}"
        if key not in seen:
            seen.add(key)
            unique.append(s)

    if not unique:
        return False, [], "No strategy conditions met"

    return True, unique, f"{len(unique)} strategies qualify"
