"""15m / 30m intraday backtest loop with Claude + Benzinga news context (optional)."""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pandas_ta as ta  # noqa: F401  # registers DataFrame accessor .ta
import yfinance as yf
from anthropic import Anthropic

from news_stream import get_latest_news, get_recent_alerts
from utils import DATA_DIR, load_json, log, save_json

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("INTRADAY_CLAUDE_MODEL", "claude-sonnet-4-5")

RESULTS_FILE = DATA_DIR / "intraday_backtest_results.json"
STATE_FILE = DATA_DIR / "intraday_state.json"

TIMEFRAMES: dict[str, str] = {
    "15m": "15 minutes",
    "30m": "30 minutes",
}

INTRADAY_PAIRS: list[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "EURGBP",
    "GBPJPY",
    "EURJPY",
    "CADJPY",
    "EURNZD",
    "GBPNZD",
]

MARKET_PROXIES: dict[str, str] = {
    "SPY": "S&P 500",
    "QQQ": "NASDAQ",
    "GLD": "Gold",
    "USO": "Oil",
}

STARTING_CAPITAL = 10000.0
LEVERAGE = 50.0

_stop = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def is_enabled() -> bool:
    data = load_json(STATE_FILE, default={})
    return isinstance(data, dict) and bool(data.get("enabled"))


def set_enabled(enabled: bool) -> None:
    save_json(
        STATE_FILE,
        {
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _flatten_yf_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_intraday_data(ticker: str, interval: str, bars: int = 100) -> pd.DataFrame:
    try:
        yf_interval = {"15m": "15m", "30m": "30m"}.get(interval, "15m")
        data = yf.download(
            ticker,
            period="5d",
            interval=yf_interval,
            progress=False,
            auto_adjust=True,
        )
        data = _flatten_yf_columns(data)
        if data.empty or len(data) < 20:
            return pd.DataFrame()
        return data.tail(int(bars))
    except Exception as e:  # noqa: BLE001
        log(f"[Intraday] Data error {ticker}: {e}")
        return pd.DataFrame()


def calculate_vwap(df: pd.DataFrame) -> float:
    try:
        if df.empty:
            return 0.0
        vol = df.get("Volume")
        if vol is None or float(vol.sum() or 0) <= 0:
            return float(df["Close"].iloc[-1])
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
        vwap = float((typical_price * vol).sum() / float(vol.sum()))
        return vwap
    except Exception:  # noqa: BLE001
        return 0.0


def calculate_opening_range(df: pd.DataFrame, minutes: int = 30, bar_minutes: int = 15) -> dict[str, Any]:
    try:
        if df.empty:
            return {}
        idx = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
        day_key = idx[-1].normalize()
        mask = idx.normalize() == day_key
        today_data = df.loc[mask]
        if len(today_data) < 2:
            return {}
        bars_needed = max(1, minutes // bar_minutes)
        opening = today_data.head(bars_needed)
        hi = float(opening["High"].max())
        lo = float(opening["Low"].min())
        last_c = float(opening["Close"].iloc[-1]) or 1e-9
        return {
            "high": hi,
            "low": lo,
            "range_pct": round((hi - lo) / last_c * 100, 3),
        }
    except Exception:  # noqa: BLE001
        return {}


def calculate_intraday_indicators(df: pd.DataFrame, bar_minutes: int = 15) -> dict[str, Any]:
    try:
        if df.empty or len(df) < 20:
            return {}
        dfc = df.copy()
        dfc.ta.rsi(length=14, append=True)
        dfc.ta.macd(append=True)
        dfc.ta.bbands(length=20, append=True)
        dfc.ta.adx(length=14, append=True)
        dfc.ta.ema(length=9, append=True)
        dfc.ta.ema(length=21, append=True)
        dfc.ta.ema(length=50, append=True)
        dfc.ta.atr(length=14, append=True)

        close = dfc["Close"]
        high = dfc["High"]
        low = dfc["Low"]
        volume = dfc.get("Volume", pd.Series([1.0] * len(dfc)))

        def _last(col: str, default: float = 0.0) -> float:
            if col in dfc.columns:
                v = dfc[col].iloc[-1]
                if pd.notna(v):
                    return float(v)
            return default

        rsi = _last("RSI_14", 50.0)
        macd_hist = _last("MACDh_12_26_9", 0.0)
        macd_line = _last("MACD_12_26_9", 0.0)
        macd_sig = _last("MACDs_12_26_9", 0.0)
        ema9 = _last("EMA_9", float(close.iloc[-1]))
        ema21 = _last("EMA_21", float(close.iloc[-1]))
        ema50 = _last("EMA_50", float(close.iloc[-1]))
        bb_lower = _last("BBL_20_2.0", float(close.iloc[-1]))
        bb_mid = _last("BBM_20_2.0", float(close.iloc[-1]))
        bb_upper = _last("BBU_20_2.0", float(close.iloc[-1]))
        bb_width = round((bb_upper - bb_lower) / bb_mid * 100, 3) if bb_mid else 1.0
        atr = _last("ATRr_14", _last("ATR_14", float(close.iloc[-1]) * 0.001))
        adx = _last("ADX_14", 20.0)

        vwap = calculate_vwap(dfc)
        price = float(close.iloc[-1])
        vwap_dev = round((price - vwap) / vwap * 100, 3) if vwap > 0 else 0.0

        opening_range = calculate_opening_range(dfc, minutes=30, bar_minutes=bar_minutes)

        recent_highs = high.tail(20)
        recent_lows = low.tail(20)
        swing_highs: list[float] = []
        swing_lows: list[float] = []
        for i in range(1, len(recent_highs) - 1):
            if recent_highs.iloc[i] > recent_highs.iloc[i - 1] and recent_highs.iloc[i] > recent_highs.iloc[i + 1]:
                swing_highs.append(round(float(recent_highs.iloc[i]), 5))
            if recent_lows.iloc[i] < recent_lows.iloc[i - 1] and recent_lows.iloc[i] < recent_lows.iloc[i + 1]:
                swing_lows.append(round(float(recent_lows.iloc[i]), 5))

        vol_cur = float(volume.iloc[-1])
        vol_avg = float(volume.tail(20).mean() or 1.0)

        return {
            "price": price,
            "rsi": round(rsi, 2),
            "macd_hist": round(macd_hist, 6),
            "macd_line": round(macd_line, 6),
            "macd_signal": round(macd_sig, 6),
            "ema9": round(ema9, 5),
            "ema21": round(ema21, 5),
            "ema50": round(ema50, 5),
            "bb_upper": round(bb_upper, 5),
            "bb_mid": round(bb_mid, 5),
            "bb_lower": round(bb_lower, 5),
            "bb_width": bb_width,
            "atr": round(atr, 5),
            "adx": round(adx, 2),
            "vwap": round(vwap, 5),
            "vwap_deviation_pct": vwap_dev,
            "swing_highs": swing_highs[-5:],
            "swing_lows": swing_lows[-5:],
            "opening_range": opening_range,
            "volume_current": vol_cur,
            "volume_avg20": vol_avg,
        }
    except Exception as e:  # noqa: BLE001
        log(f"[Intraday] Indicator error: {e}")
        return {}


def get_news_context(ticker: str, timeframe: str) -> str:
    try:
        minutes = 30 if timeframe == "15m" else 60
        alerts = get_recent_alerts(minutes=minutes, min_sentiment=0.25)
        if not alerts:
            _ = get_latest_news()
            return "No significant news in window."
        tu = ticker.upper()
        pair_relevant = [
            a
            for a in alerts
            if not a.get("tickers")
            or tu in {str(x).upper() for x in (a.get("tickers") or [])}
        ]
        pick = pair_relevant[:5] if pair_relevant else alerts[:5]
        lines = [f"RECENT NEWS (last {minutes} min):"]
        for a in pick:
            s = float(a.get("sentiment", 0) or 0)
            tag = "[+]" if s > 0.3 else "[-]" if s < -0.3 else "[~]"
            title = str(a.get("title", ""))[:80]
            lines.append(f"{tag} [{a.get('impact')}] {title} (sentiment: {s:+.2f})")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        log(f"[Intraday] News context error: {e}")
        return "News unavailable."


def build_intraday_prompt(ticker: str, timeframe: str, ind: dict[str, Any], news_context: str) -> str:
    price = float(ind.get("price", 0) or 0)
    vwap = float(ind.get("vwap", 0) or 0)
    vwap_dev = float(ind.get("vwap_deviation_pct", 0) or 0)
    opening_range = ind.get("opening_range") if isinstance(ind.get("opening_range"), dict) else {}
    vol_pct = round(
        float(ind.get("volume_current", 1) or 1) / max(float(ind.get("volume_avg20", 1) or 1), 1e-9) * 100,
        1,
    )

    or_text = ""
    or_range_status = "Range not yet established"
    if opening_range:
        or_high = float(opening_range.get("high", 0) or 0)
        or_low = float(opening_range.get("low", 0) or 0)
        or_range = float(opening_range.get("range_pct", 0) or 0)
        if price > or_high:
            or_position = "ABOVE opening range"
            or_range_status = "ABOVE — potential long trigger"
        elif price < or_low:
            or_position = "BELOW opening range"
            or_range_status = "BELOW — potential short trigger"
        else:
            or_position = "INSIDE opening range"
            or_range_status = "INSIDE — wait for breakout"
        or_text = f"""
Opening Range (first 30min):
  High: {or_high:.5f}
  Low: {or_low:.5f}
  Range: {or_range:.3f}%
  Price vs Range: {or_position}"""

    utc_now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return f"""
You are APEX — elite intraday trading AI.

You trade 15-minute and 30-minute forex charts
using strategies proven by professional traders.

You have FOUR intraday strategies.
You MUST skip if none clearly qualifies.
Skipping bad setups is professional discipline.

═══════════════════════════════════════════
MARKET DATA
═══════════════════════════════════════════
Asset: {ticker}
Timeframe: {timeframe}
Time: {utc_now}
Price: {price:.5f}

INTRADAY INDICATORS:
RSI(14): {float(ind.get('rsi', 50)):.1f}
EMA9: {float(ind.get('ema9', 0)):.5f}
EMA21: {float(ind.get('ema21', 0)):.5f}
EMA50: {float(ind.get('ema50', 0)):.5f}
MACD Histogram: {float(ind.get('macd_hist', 0)):.6f}
ATR: {float(ind.get('atr', 0)):.5f}
ADX: {float(ind.get('adx', 0)):.1f}
BB Width: {float(ind.get('bb_width', 0)):.2f}%
BB Upper: {float(ind.get('bb_upper', 0)):.5f}
BB Lower: {float(ind.get('bb_lower', 0)):.5f}

VWAP: {vwap:.5f}
VWAP Deviation: {vwap_dev:+.3f}%
(+= above VWAP, -= below VWAP)
{or_text}

Swing Highs: {ind.get("swing_highs", [])}
Swing Lows: {ind.get("swing_lows", [])}

Volume current vs avg:
{float(ind.get("volume_current", 0)):.0f} vs
{float(ind.get("volume_avg20", 0)):.0f}
({vol_pct}% of average)

═══════════════════════════════════════════
LIVE NEWS
═══════════════════════════════════════════
{news_context}

═══════════════════════════════════════════
THE 4 INTRADAY STRATEGIES
Scan all. Pick best. Skip if none qualify.
═══════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S13: NEWS MOMENTUM (Benzinga-powered)
Best when: High-impact news just released
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
What: Strong news drives price in one
direction for 15-60 minutes. Trade the
momentum not the news itself.

Qualifies when (need all 3):
→ Recent news sentiment above 0.4 or
  below -0.4 (see news section above)
→ Price already moving in news direction
  (first 15m candle confirms direction)
→ Volume above 130% of 20-bar average
  Current volume %: {vol_pct}%

LONG: bullish news (>0.4) + price rising
  + high volume confirming
SHORT: bearish news (<-0.4) + price falling
  + high volume confirming

Stop: pre-news candle low/high
Hold: 15-60 minutes maximum
Note: Speed matters. Enter within first 15min
or skip — news momentum fades fast.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S14: OPENING RANGE BREAKOUT
Best when: First 30-60 min establishes range
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
What: First 30 minutes sets the day's range.
A clean break above or below = directional
trade for the rest of the session.

Qualifies when (need all 3):
→ Opening range established (time > 30min)
→ Price breaks clearly above OR below range
  (close outside range, not just wick)
→ Volume spike on breakout bar (>150% avg)

LONG: close above opening range high
SHORT: close below opening range low

Stop: midpoint of opening range
Target: Range height projected from breakout
Hold: 1-4 hours (rest of session)

Current position vs range: {or_range_status}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S15: VWAP DEVIATION REVERSION
Best when: Price far from VWAP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
What: Price deviates from VWAP by unusual
amount then reverts. VWAP is the daily
institutional average price.

Qualifies when (need 2 of 3):
→ VWAP deviation above 0.15% or below -0.15%
  Current deviation: {vwap_dev:+.3f}%
→ RSI confirms deviation:
  Short: RSI above 65 (overbought)
  Long: RSI below 35 (oversold)
  Current RSI: {float(ind.get('rsi', 50)):.1f}
→ First touch of extreme level
  (not already bouncing off VWAP today)

LONG: deviation below -0.15%, RSI below 35
SHORT: deviation above +0.15%, RSI above 65

Stop: 1x ATR beyond current extreme
Target: VWAP itself (mean reversion)
Hold: 15-90 minutes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S16: HTF LEVEL REJECTION
Best when: Price reaches key 4H/1D level
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
What: Higher timeframe key levels (daily
swing highs/lows) act as strong support
or resistance on intraday charts.

Qualifies when (need 2 of 3):
→ Price touching a level that appears
  multiple times in swing_highs or
  swing_lows (within 0.3%)
→ Rejection candle visible
  (price touched level and pulled back)
→ RSI confirming:
  Short at resistance: RSI above 60
  Long at support: RSI below 40

LONG: price at swing_low cluster + RSI<40
SHORT: price at swing_high cluster + RSI>60

Stop: 0.5x ATR beyond the level
Target: Opposite swing cluster or VWAP
Hold: 30-120 minutes

Swing highs: {ind.get("swing_highs", [])}
Swing lows: {ind.get("swing_lows", [])}
Price distance to nearest level:
Check if within 0.3% of any swing level

═══════════════════════════════════════════
INTRADAY RULES
═══════════════════════════════════════════

TIME FILTERS:
Best trading windows (UTC):
✅ 07:00-10:00 London open
✅ 13:30-16:00 NY open overlap
⚠️ 10:00-13:00 Mid-session (quieter)
❌ 21:00-07:00 Asian session (avoid forex)

Current time: {utc_now}

SKIP RULES:
Skip if:
- No strategy qualifies with clear signals
- Volume below 80% of average (no liquidity)
- News sentiment between -0.3 and +0.3
  when S13 is the only option
- Price inside opening range (S14 not triggered)
- VWAP deviation below 0.1% (S15 not triggered)
- No swing level within 0.3% (S16 not triggered)
- Outside trading hours (21:00-06:00 UTC)

POSITION SIZING:
All intraday trades: LOW confidence
Maximum risk: 0.5% per trade
Stop distance: Never more than 0.3% from entry
Hold time: Maximum 4 hours

MINIMUM R/R: 1.5 (intraday — lower than swing)
Reason: Faster moves, quicker profits

Use strategy_id one of:
S13_NEWS_MOMENTUM, S14_OPENING_RANGE_BREAKOUT, S15_VWAP_DEVIATION, S16_HTF_LEVEL_REJECTION

═══════════════════════════════════════════
OUTPUT — JSON ONLY
═══════════════════════════════════════════

If skipping:
{{
  "skip_trade": true,
  "skip_reason": "specific reason",
  "strategy_id": "SKIP",
  "direction": "NONE"
}}

If trading:
{{
  "skip_trade": false,
  "strategy_id": "S13_NEWS_MOMENTUM",
  "strategy_name": "News Momentum",
  "direction": "LONG",
  "confidence": "LOW",
  "conviction_score": 5,
  "entry": {price:.5f},
  "stop_loss": 0.0,
  "tp1": 0.0,
  "tp2": 0.0,
  "rr_ratio": "1:2.0",
  "hold_minutes": 30,
  "signals_used": ["SIGNALS"],
  "news_trigger": "headline that triggered",
  "news_sentiment": 0.0,
  "vwap_deviation": {vwap_dev:.3f},
  "reasoning": "max 50 words"
}}
"""


def _parse_json_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    clean = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s*```\s*$", "", clean).strip()
    try:
        out = json.loads(clean)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            out = json.loads(m.group(0))
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def call_claude_intraday(prompt: str) -> dict[str, Any]:
    if not ANTHROPIC_KEY:
        return {"skip_trade": True, "skip_reason": "ANTHROPIC_API_KEY missing"}
    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        text = text.strip()
        parsed = _parse_json_response(text)
        if isinstance(parsed, dict):
            return parsed
        return {"skip_trade": True, "skip_reason": "parse error"}
    except Exception as e:  # noqa: BLE001
        log(f"[Intraday] Claude call error: {e}")
        return {"skip_trade": True, "skip_reason": str(e)}


def evaluate_intraday_exit(
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    forward_df: pd.DataFrame,
    timeframe: str,
    hold_minutes: int = 60,
) -> dict[str, Any]:
    bar_mins = 15 if timeframe == "15m" else 30
    max_bars = max(1, int(hold_minutes // bar_mins))

    if forward_df is None or forward_df.empty:
        return {"outcome": "NO_DATA", "pnl_pct": 0.0, "exit_price": entry, "hit_tp1": False, "hit_stop": False}

    risk = abs(entry - stop)
    if risk <= 0 or not (entry > 0):
        return {"outcome": "INVALID", "pnl_pct": 0.0, "exit_price": entry, "hit_tp1": False, "hit_stop": False}

    hit_tp1 = False
    hit_stop = False
    current_stop = stop
    exit_price = entry
    direction = direction.strip().upper()

    for i, (_idx, candle) in enumerate(forward_df.iterrows()):
        if i >= max_bars:
            exit_price = float(candle.get("Close", entry))
            break
        high = float(candle.get("High", entry))
        low = float(candle.get("Low", entry))
        close = float(candle.get("Close", entry))

        if direction == "LONG":
            if low <= current_stop:
                hit_stop = True
                exit_price = current_stop
                break
            if high >= tp2:
                exit_price = tp2
                break
            if high >= tp1 and not hit_tp1:
                hit_tp1 = True
                current_stop = entry
        else:
            if high >= current_stop:
                hit_stop = True
                exit_price = current_stop
                break
            if low <= tp2:
                exit_price = tp2
                break
            if low <= tp1 and not hit_tp1:
                hit_tp1 = True
                current_stop = entry
    else:
        exit_price = float(forward_df["Close"].iloc[-1])

    if direction == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100.0
    else:
        pnl_pct = (entry - exit_price) / entry * 100.0

    return {
        "outcome": "WIN" if pnl_pct > 0 else "LOSS",
        "exit_price": round(exit_price, 5),
        "pnl_pct": round(pnl_pct, 4),
        "hit_tp1": hit_tp1,
        "hit_stop": hit_stop,
    }


def save_intraday_result(result: dict[str, Any]) -> None:
    try:
        results = load_json(RESULTS_FILE, default=[])
        if not isinstance(results, list):
            results = []
        results.append(result)
        results = results[-500:]
        save_json(RESULTS_FILE, results)
    except Exception as e:  # noqa: BLE001
        log(f"[Intraday] Save error: {e}")


def run_intraday_backtest() -> None:
    log("[Intraday] Starting backtest cycle")
    ticker = random.choice(INTRADAY_PAIRS)
    timeframe = random.choice(list(TIMEFRAMES.keys()))
    bar_mins = 15 if timeframe == "15m" else 30

    log(f"[Intraday] Testing {ticker} {timeframe}")
    yf_sym = ticker + "=X"
    df = fetch_intraday_data(yf_sym, timeframe, bars=120)
    if df.empty:
        log(f"[Intraday] No data for {ticker}")
        return

    fwd_bars = 8
    if len(df) < fwd_bars + 30:
        log(f"[Intraday] Not enough bars for {ticker}")
        return

    hist = df.iloc[:-fwd_bars].copy()
    forward = df.iloc[-fwd_bars:].copy()

    ind = calculate_intraday_indicators(hist, bar_minutes=bar_mins)
    if not ind:
        log(f"[Intraday] No indicators for {ticker}")
        return

    price = float(ind.get("price", 0) or 0)
    news_ctx = get_news_context(ticker, timeframe)
    prompt = build_intraday_prompt(ticker, timeframe, ind, news_ctx)
    ai = call_claude_intraday(prompt)

    result: dict[str, Any] = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "time": datetime.now(timezone.utc).strftime("%H:%M"),
        "ticker": ticker,
        "timeframe": timeframe,
        "strategy_id": str(ai.get("strategy_id", "SKIP")),
        "direction": str(ai.get("direction", "NONE")),
        "confidence": "LOW",
        "entry_price": float(ai.get("entry", price) or price),
        "stop_loss": float(ai.get("stop_loss", 0) or 0),
        "tp1": float(ai.get("tp1", 0) or 0),
        "tp2": float(ai.get("tp2", 0) or 0),
        "news_sentiment": float(ai.get("news_sentiment", 0) or 0),
        "vwap_deviation": float(ai.get("vwap_deviation", ind.get("vwap_deviation_pct", 0)) or 0),
        "signals_used": ai.get("signals_used") if isinstance(ai.get("signals_used"), list) else [],
        "reasoning": str(ai.get("reasoning", "")),
        "skipped": bool(ai.get("skip_trade", True)),
        "skip_reason": str(ai.get("skip_reason", "")),
        "outcome": "SKIPPED",
        "pnl_dollars": 0.0,
        "pnl_pct": 0.0,
    }

    if ai.get("skip_trade", True):
        log(f"[Intraday] SKIP: {ai.get('skip_reason', '')}")
        save_intraday_result(result)
        return

    entry = float(ai.get("entry", price) or price)
    stop = float(ai.get("stop_loss", 0) or 0)
    tp1 = float(ai.get("tp1", 0) or 0)
    tp2 = float(ai.get("tp2", 0) or 0)
    direction = str(ai.get("direction", "LONG")).strip().upper()
    if direction not in ("LONG", "SHORT"):
        result["skipped"] = True
        result["skip_reason"] = "invalid direction"
        save_intraday_result(result)
        return

    hold_minutes = int(ai.get("hold_minutes", 60) or 60)
    exit_data = evaluate_intraday_exit(
        direction=direction,
        entry=entry,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        forward_df=forward,
        timeframe=timeframe,
        hold_minutes=hold_minutes,
    )

    if exit_data.get("outcome") in ("NO_DATA", "INVALID"):
        result["skipped"] = True
        result["skip_reason"] = str(exit_data.get("outcome"))
        save_intraday_result(result)
        return

    pnl_pct = float(exit_data.get("pnl_pct", 0) or 0)
    risk_pct_price = abs(entry - stop) / entry if entry else 0.001
    risk_dollars = STARTING_CAPITAL * 0.005
    position_size = min(500.0, risk_dollars / max(risk_pct_price * LEVERAGE, 1e-9))
    leveraged_exposure = position_size * LEVERAGE
    pnl_dollars = round(leveraged_exposure * (pnl_pct / 100.0), 2)

    result.update(
        {
            "outcome": str(exit_data.get("outcome", "UNKNOWN")),
            "exit_price": float(exit_data.get("exit_price", entry)),
            "pnl_pct": pnl_pct,
            "pnl_dollars": pnl_dollars,
            "hit_tp1": bool(exit_data.get("hit_tp1")),
            "hit_stop": bool(exit_data.get("hit_stop")),
            "skipped": False,
        }
    )

    log(
        f"[Intraday] {ticker} {timeframe} {result['direction']} "
        f"→ {result['outcome']} ${pnl_dollars:.2f}"
    )
    save_intraday_result(result)


def _daemon_loop() -> None:
    while not _stop.is_set():
        if is_enabled():
            try:
                run_intraday_backtest()
            except Exception as e:  # noqa: BLE001
                log(f"[Intraday] Loop error: {e}")
            time.sleep(45)
        else:
            time.sleep(3)


def ensure_intraday_daemon() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_daemon_loop, daemon=True, name="IntradayBacktestDaemon")
        _thread.start()
        log("[Intraday] Daemon thread started")


def stop_intraday_daemon() -> None:
    _stop.set()
    log("[Intraday] Daemon stop requested")


__all__ = [
    "MARKET_PROXIES",
    "RESULTS_FILE",
    "STATE_FILE",
    "TIMEFRAMES",
    "ensure_intraday_daemon",
    "is_enabled",
    "run_intraday_backtest",
    "set_enabled",
    "stop_intraday_daemon",
]
