"""APEX v6.0 Python pre-filter — 68-strategy coverage with canonical STRATEGIES ids."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from strategies_v5_data import STRATEGIES

# Map v6 shorthand / prompt ids → keys in ``strategies_v5_data.STRATEGIES``
_V6_TO_CANON: dict[str, str] = {
    "T03_HH_HL": "T03_HH_HL_CONTINUATION",
    "T04_ADX_ENTRY": "T04_ADX_TREND_ENTRY",
    "T07_MA_RIBBON": "T07_MA_RIBBON_ALIGNMENT",
    "T08_DONCHIAN": "T08_DONCHIAN_BREAKOUT",
    "T09_KELTNER": "T09_KELTNER_TREND_RIDE",
    "R01_EXTREME_ZONE": "R01_EXTREME_ZONE_REVERSION",
    "R03_BB_TOUCH": "R03_BB_EXTREME_TOUCH",
    "R04_STOCHASTIC": "R04_STOCHASTIC_EXTREME",
    "R05_CCI": "R05_CCI_EXTREME",
    "R06_WILLIAMS": "R06_WILLIAMS_R_EXTREME",
    "R08_MONTHLY_LEVEL": "R08_MONTHLY_LEVEL_REJECTION",
    "R09_GAP_FILL": "R09_WEEKLY_GAP_FILL",
    "B01_RANGE_BREAK": "B01_RANGE_BREAKOUT",
    "B02_VOL_COMPRESSION": "B02_VOLATILITY_COMPRESSION",
    "B06_TRIANGLE": "B06_TRIANGLE_BREAKOUT",
    "B07_INSIDE_BAR": "B07_INSIDE_BAR_BREAKOUT",
    "B08_KEY_RETEST": "B08_KEY_LEVEL_RETEST",
    "B09_RSI_BREAK": "B09_RSI_MOMENTUM_BREAK",
    "B10_WEEKLY_RANGE": "B10_WEEKLY_RANGE_BREAK",
    "M01_MACD_DIV": "M01_MACD_DIVERGENCE",
    "M02_MACD_ZERO": "M02_MACD_ZERO_CROSS",
    "M03_RSI_CONT": "M03_RSI_MOMENTUM_CONTINUATION",
    "M06_ACCELERATION": "M06_PRICE_ACCELERATION",
    "SMC05_EQ_HIGHS": "SMC05_EQUAL_HL_HUNT",
    "SMC05_EQ_LOWS": "SMC05_EQUAL_HL_HUNT",
    "SMC09_PDM": "SMC09_PREMIUM_DISCOUNT_MSS",
    "V02_ATR_EXPAND": "V02_ATR_EXPANSION_ENTRY",
    "V03_SQUEEZE": "V03_SQUEEZE_BREAKOUT",
    "V06_VOL_REVERSION": "V06_VOLATILITY_MEAN_REVERSION",
    "I01_VWAP_SCALP": "I01_VWAP_DEVIATION_SCALP",
    "I02_HTF_REJECT": "I02_HTF_LEVEL_REJECTION",
    "B05_ORB": "B05_OPENING_RANGE_BREAKOUT",
    "M08_NEWS_MOM": "M08_NEWS_MOMENTUM",
}


def _canon_strategy_id(raw_id: str) -> str | None:
    u = (raw_id or "").strip().upper()
    if not u:
        return None
    c = _V6_TO_CANON.get(u, u)
    return c if c in STRATEGIES else None


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
    Pure Python check (v6 rules). Returns (qualifies, strategies, reason).
    Each tuple: (canonical_strategy_id, direction, score).
    """
    _ = past
    tf = (timeframe or "").strip().lower()
    qualifying: list[tuple[str, str, int]] = []

    ema20 = float(ind.get("ema20", price) or price)
    ema50 = float(ind.get("ema50", price) or price)
    ema200 = float(ind.get("ema200", price) or price)
    rsi = float(ind.get("rsi", 50.0) or 50.0)
    adx = float(ind.get("adx", 20.0) or 20.0)
    atr = float(ind.get("atr", price * 0.01) or (price * 0.01))
    macd_hist = float(ind.get("macd_hist", 0.0) or 0.0)
    macd_line = float(ind.get("macd_line", 0.0) or 0.0)
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
        return False, [], "15M/30M unavailable >55 days ago"

    ema_bull = ema20 > ema200 and ema50 > ema200
    ema_bear = ema20 < ema200 and ema50 < ema200
    within_2atr = abs(price - ema20) <= atr * 2.5 or abs(price - ema50) <= atr * 2.5
    rsi_room = 25 <= rsi <= 75
    adx_trend = adx >= 12
    above_bb = price > bb_upper
    below_bb = price < bb_lower
    compressed = bb_width < 2.0
    tight = bb_width < 1.5

    if tf in ("4h", "1d", "1w"):
        t01 = int(sum([bool(ema_bull or ema_bear), within_2atr, rsi_room, adx_trend]))
        if t01 >= 2:
            if ema_bull and zone_pct < 60:
                qualifying.append(("T01_EMA_PULLBACK", "LONG", t01))
            if ema_bear and zone_pct > 40:
                qualifying.append(("T01_EMA_PULLBACK", "SHORT", t01))
            if zone_pct < 20:
                qualifying.append(("T01_EMA_PULLBACK", "LONG", t01))
            if zone_pct > 80:
                qualifying.append(("T01_EMA_PULLBACK", "SHORT", t01))

        if ema20 > ema50 and adx > 20 and zone_pct < 65:
            qualifying.append(("T02_EMA_CROSSOVER", "LONG", 2))
        if ema20 < ema50 and adx > 20 and zone_pct > 35:
            qualifying.append(("T02_EMA_CROSSOVER", "SHORT", 2))

        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            hh = swing_highs[-1] > swing_highs[-2]
            hl = swing_lows[-1] > swing_lows[-2]
            ll = swing_lows[-1] < swing_lows[-2]
            lh = swing_highs[-1] < swing_highs[-2]
            if hh and hl and within_2atr:
                qualifying.append(("T03_HH_HL", "LONG", 3))
            if ll and lh and within_2atr:
                qualifying.append(("T03_HH_HL", "SHORT", 3))

        if tf in ("4h", "1d"):
            if adx > 25 and ema_bull:
                qualifying.append(("T04_ADX_ENTRY", "LONG", 2))
            if adx > 25 and ema_bear:
                qualifying.append(("T04_ADX_ENTRY", "SHORT", 2))

        if tf in ("1d", "1w"):
            if ema20 > ema50 > ema200 and within_2atr:
                qualifying.append(("T07_MA_RIBBON", "LONG", 3))
            if ema20 < ema50 < ema200 and within_2atr:
                qualifying.append(("T07_MA_RIBBON", "SHORT", 3))

        dist200 = abs(price - ema200) / max(atr, 0.0001)
        if dist200 < 1.5 and adx > 15:
            if price > ema200 and ema_bull:
                qualifying.append(("T10_200EMA_BOUNCE", "LONG", 2))
            if price < ema200 and ema_bear:
                qualifying.append(("T10_200EMA_BOUNCE", "SHORT", 2))

        if adx > 20 and bb_width > 3.0:
            if above_bb and ema_bull:
                qualifying.append(("T08_DONCHIAN", "LONG", 2))
            if below_bb and ema_bear:
                qualifying.append(("T08_DONCHIAN", "SHORT", 2))

        if adx > 25 and (above_bb or below_bb):
            if ema_bull:
                qualifying.append(("T09_KELTNER", "LONG", 2))
            if ema_bear:
                qualifying.append(("T09_KELTNER", "SHORT", 2))

    if tf in ("1d", "1w"):
        r01_l = int(sum([zone_pct <= 20, rsi <= 40, adx <= 45]))
        r01_s = int(sum([zone_pct >= 80, rsi >= 60, adx <= 45]))
        if r01_l >= 2:
            qualifying.append(("R01_EXTREME_ZONE", "LONG", r01_l))
        if r01_s >= 2:
            qualifying.append(("R01_EXTREME_ZONE", "SHORT", r01_s))

    if tf in ("4h", "1d"):
        if zone_pct < 30 and macd_hist > 0 and rsi < 45:
            qualifying.append(("R02_RSI_DIVERGENCE", "LONG", 2))
        if zone_pct > 70 and macd_hist < 0 and rsi > 55:
            qualifying.append(("R02_RSI_DIVERGENCE", "SHORT", 2))

    if tf in ("4h", "1d"):
        if below_bb and rsi < 35:
            qualifying.append(("R03_BB_TOUCH", "LONG", 2))
        if above_bb and rsi > 65:
            qualifying.append(("R03_BB_TOUCH", "SHORT", 2))

    if tf in ("4h", "1d"):
        if rsi < 25 and zone_pct < 30:
            qualifying.append(("R04_STOCHASTIC", "LONG", 2))
        if rsi > 75 and zone_pct > 70:
            qualifying.append(("R04_STOCHASTIC", "SHORT", 2))

    if tf in ("4h", "1d"):
        if rsi < 20 and zone_pct < 25:
            qualifying.append(("R05_CCI", "LONG", 2))
        if rsi > 80 and zone_pct > 75:
            qualifying.append(("R05_CCI", "SHORT", 2))

    if tf in ("4h", "1d"):
        if rsi < 22 and below_bb:
            qualifying.append(("R06_WILLIAMS", "LONG", 2))
        if rsi > 78 and above_bb:
            qualifying.append(("R06_WILLIAMS", "SHORT", 2))

    if tf in ("1d", "1w"):
        if zone_pct < 5 and rsi < 35:
            qualifying.append(("R08_MONTHLY_LEVEL", "LONG", 3))
        if zone_pct > 95 and rsi > 65:
            qualifying.append(("R08_MONTHLY_LEVEL", "SHORT", 3))

    if tf in ("4h", "1d") and analysis_date:
        try:
            wd = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d").weekday()
            if wd == 0:
                qualifying.append(("R09_GAP_FILL", "BOTH", 2))
        except Exception:  # noqa: BLE001
            pass

    if tf == "1d" and bb_width < 3.0 and adx < 30:
        if above_bb:
            qualifying.append(("B01_RANGE_BREAK", "LONG", 2))
        if below_bb:
            qualifying.append(("B01_RANGE_BREAK", "SHORT", 2))

    if tf in ("1d", "4h") and compressed and adx < 20:
        qualifying.append(("B02_VOL_COMPRESSION", "BOTH", 2))

    if tf in ("4h", "1d"):
        if bb_width < 2.5 and adx < 25 and 30 < zone_pct < 70:
            qualifying.append(("B06_TRIANGLE", "BOTH", 2))

    if tf in ("4h", "1d", "1w") and tight and adx < 15:
        qualifying.append(("B07_INSIDE_BAR", "BOTH", 2))

    if tf in ("4h", "1d"):
        for level in swing_highs + swing_lows:
            try:
                lv = float(level)
            except (TypeError, ValueError):
                continue
            if lv > 0 and abs(price - lv) / lv < 0.005:
                if adx > 15:
                    qualifying.append(("B08_KEY_RETEST", "BOTH", 2))
                break

    if tf in ("4h", "1d") and 45 < rsi < 55 and adx > 20:
        if ema_bull:
            qualifying.append(("B09_RSI_BREAK", "LONG", 2))
        if ema_bear:
            qualifying.append(("B09_RSI_BREAK", "SHORT", 2))

    if tf in ("4h", "1d") and adx > 20:
        if above_bb and ema_bull:
            qualifying.append(("B10_WEEKLY_RANGE", "LONG", 2))
        if below_bb and ema_bear:
            qualifying.append(("B10_WEEKLY_RANGE", "SHORT", 2))

    if tf in ("4h", "1d"):
        if zone_pct < 30 and macd_hist > 0 and rsi < 45:
            qualifying.append(("M01_MACD_DIV", "LONG", 2))
        if zone_pct > 70 and macd_hist < 0 and rsi > 55:
            qualifying.append(("M01_MACD_DIV", "SHORT", 2))

    if tf in ("4h", "1d"):
        if macd_line > 0 and macd_hist > 0 and ema_bull:
            qualifying.append(("M02_MACD_ZERO", "LONG", 2))
        if macd_line < 0 and macd_hist < 0 and ema_bear:
            qualifying.append(("M02_MACD_ZERO", "SHORT", 2))

    if tf in ("4h", "1d", "1w"):
        if 50 < rsi < 70 and ema_bull and within_2atr:
            qualifying.append(("M03_RSI_CONT", "LONG", 2))
        if 30 < rsi < 50 and ema_bear and within_2atr:
            qualifying.append(("M03_RSI_CONT", "SHORT", 2))

    if tf in ("4h", "1d") and adx > 30 and bb_width > 3.0:
        if ema_bull:
            qualifying.append(("M06_ACCELERATION", "LONG", 2))
        if ema_bear:
            qualifying.append(("M06_ACCELERATION", "SHORT", 2))

    if tf in ("4h", "1d", "1w"):
        all_swings = list(swing_highs) + list(swing_lows)
        for level in all_swings:
            try:
                lv = float(level)
            except (TypeError, ValueError):
                continue
            if lv <= 0:
                continue
            nearby = [s for s in all_swings if _swing_near(lv, s)]
            if len(nearby) >= 2 and abs(price - lv) / lv < 0.004:
                qualifying.append(("SMC01_SR_FLIP", "BOTH", 2))
                break

    if tf in ("4h", "1d"):
        for i, h in enumerate(swing_highs[:-1]):
            for h2 in swing_highs[i + 1 :]:
                try:
                    fh, fh2 = float(h), float(h2)
                except (TypeError, ValueError):
                    continue
                if fh > 0 and abs(fh - fh2) / fh < 0.003:
                    if price > max(fh, fh2) and rsi > 60:
                        qualifying.append(("SMC05_EQ_HIGHS", "SHORT", 2))
                    break
        for i, lowv in enumerate(swing_lows[:-1]):
            for l2 in swing_lows[i + 1 :]:
                try:
                    fl, fl2 = float(lowv), float(l2)
                except (TypeError, ValueError):
                    continue
                if fl > 0 and abs(fl - fl2) / fl < 0.003:
                    if price < min(fl, fl2) and rsi < 40:
                        qualifying.append(("SMC05_EQ_LOWS", "LONG", 2))
                    break

    if tf in ("4h", "1d", "1w"):
        if zone_pct < 15 and rsi < 40:
            qualifying.append(("SMC09_PDM", "LONG", 3))
        if zone_pct > 85 and rsi > 60:
            qualifying.append(("SMC09_PDM", "SHORT", 3))

    if tf in ("4h", "1d"):
        if len(swing_lows) >= 2 and len(swing_highs) >= 2:
            if swing_lows[-1] > swing_lows[-2] and swing_highs and price > float(swing_highs[-1]):
                qualifying.append(("SMC10_CHOCH", "LONG", 3))
            if swing_highs[-1] < swing_highs[-2] and swing_lows and price < float(swing_lows[-1]):
                qualifying.append(("SMC10_CHOCH", "SHORT", 3))

    if tf in ("4h", "1d") and bb_width > 4.0 and adx > 25:
        if ema_bull:
            qualifying.append(("V02_ATR_EXPAND", "LONG", 2))
        if ema_bear:
            qualifying.append(("V02_ATR_EXPAND", "SHORT", 2))

    if tf in ("4h", "1d") and tight and adx < 15:
        qualifying.append(("V03_SQUEEZE", "BOTH", 3))

    if tf in ("1d", "1w") and bb_width > 5.0:
        if zone_pct < 20 and rsi < 40:
            qualifying.append(("V06_VOL_REVERSION", "LONG", 2))
        if zone_pct > 80 and rsi > 60:
            qualifying.append(("V06_VOL_REVERSION", "SHORT", 2))

    if tf in ("15m", "30m") and days_ago <= 55:
        if rsi <= 30:
            qualifying.append(("I01_VWAP_SCALP", "LONG", 2))
        if rsi >= 70:
            qualifying.append(("I01_VWAP_SCALP", "SHORT", 2))
        for level in swing_highs + swing_lows:
            try:
                lv = float(level)
            except (TypeError, ValueError):
                continue
            if lv > 0 and abs(price - lv) / lv < 0.002:
                qualifying.append(("I02_HTF_REJECT", "BOTH", 2))
                break
        if adx > 15 and (above_bb or below_bb):
            qualifying.append(("B05_ORB", "BOTH", 2))
        if rsi <= 35 or rsi >= 65:
            qualifying.append(("M08_NEWS_MOM", "BOTH", 2))

    seen: set[str] = set()
    unique: list[tuple[str, str, int]] = []
    for raw_sid, dire, score in qualifying:
        cid = _canon_strategy_id(raw_sid)
        if cid is None:
            continue
        key = f"{cid}_{dire}"
        if key not in seen:
            seen.add(key)
            unique.append((cid, dire, score))

    if not unique:
        return False, [], "No strategy conditions met"

    return True, unique, f"{len(unique)} strategies qualify"


def _swing_near(level: float, s: Any) -> bool:
    try:
        fv = float(s)
    except (TypeError, ValueError):
        return False
    if level <= 0:
        return False
    return abs(fv - level) / level < 0.005
