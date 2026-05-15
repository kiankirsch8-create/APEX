"""Real-time Benzinga WebSocket listener; persists alerts and latest headline for intraday use."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import websocket

from utils import DATA_DIR, load_json, log, save_json

BENZINGA_KEY = os.getenv("BENZINGA_API_KEY", "")
WS_URL = (
    "wss://api.benzinga.com/api/v1/"
    f"news/stream?token={BENZINGA_KEY}"
)

NEWS_FILE = DATA_DIR / "live_news.json"
ALERT_FILE = DATA_DIR / "news_alerts.json"

# Channels that move forex markets
FOREX_CHANNELS: frozenset[str] = frozenset(
    {
        "Federal Reserve",
        "FOMC",
        "Fed",
        "European Central Bank",
        "ECB",
        "Bank of England",
        "BOE",
        "BOJ",
        "Bank of Canada",
        "BOC",
        "Economic Data",
        "GDP",
        "CPI",
        "Inflation",
        "Employment",
        "NFP",
        "Interest Rates",
        "Rate Decision",
        "Trade Balance",
        "Retail Sales",
        "PMI",
        "ISM",
        "Manufacturing",
    }
)

# Channels that move stocks
STOCK_CHANNELS: frozenset[str] = frozenset(
    {
        "Earnings",
        "M&A",
        "Mergers",
        "Analyst Ratings",
        "Price Target",
        "Upgrades",
        "Downgrades",
        "FDA",
        "Drug Approval",
        "Clinical Trial",
        "Guidance",
        "Revenue",
        "Profit Warning",
        "Dividend",
        "Buyback",
        "IPO",
    }
)

BULLISH_WORDS = [
    "beat",
    "beats",
    "exceeded",
    "surpass",
    "record",
    "raise",
    "raised",
    "upgrade",
    "strong",
    "better than expected",
    "above estimates",
    "outperform",
    "buy",
    "overweight",
    "positive",
    "growth",
    "approved",
    "approval",
    "deal",
    "acquisition",
    "merger",
    "dividend",
]

BEARISH_WORDS = [
    "miss",
    "missed",
    "below",
    "cut",
    "lowered",
    "downgrade",
    "weak",
    "worse than expected",
    "below estimates",
    "underperform",
    "sell",
    "underweight",
    "negative",
    "decline",
    "loss",
    "warning",
    "rejected",
    "delay",
    "recall",
    "fraud",
    "investigation",
    "lawsuit",
]

_stream_thread: threading.Thread | None = None
_stream_lock = threading.Lock()


def _as_str_set(items: Any) -> set[str]:
    out: set[str] = set()
    if not isinstance(items, list):
        return out
    for c in items:
        if isinstance(c, str) and c.strip():
            out.add(c.strip())
        elif isinstance(c, dict):
            for k in ("name", "title", "channel", "label"):
                v = c.get(k)
                if v:
                    out.add(str(v).strip())
                    break
    return out


def score_sentiment(title: str, body: str = "") -> float:
    text = (title + " " + body[:300]).lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text)
    bear = sum(1 for w in BEARISH_WORDS if w in text)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 2)


def classify_impact(channels: list[Any], tags: list[Any], sentiment: float) -> str:
    channel_set = _as_str_set(channels) | _as_str_set(tags)

    forex_match = bool(channel_set & FOREX_CHANNELS)
    stock_match = bool(channel_set & STOCK_CHANNELS)

    if abs(sentiment) >= 0.6:
        if forex_match:
            return "HIGH_FOREX"
        if stock_match:
            return "HIGH_STOCK"
        return "HIGH_GENERAL"
    if abs(sentiment) >= 0.3:
        if forex_match:
            return "MEDIUM_FOREX"
        if stock_match:
            return "MEDIUM_STOCK"
        return "LOW"
    return "NOISE"


def save_news_alert(alert: dict[str, Any]) -> None:
    try:
        alerts = load_json(ALERT_FILE, default=[])
        if not isinstance(alerts, list):
            alerts = []
        alerts.append(alert)
        alerts = alerts[-100:]
        save_json(ALERT_FILE, alerts)
        log(
            f"[NEWS] Alert saved: {alert.get('impact')} | "
            f"{float(alert.get('sentiment', 0)):+.2f} | "
            f"{str(alert.get('title', ''))[:60]}"
        )
    except Exception as e:  # noqa: BLE001
        log(f"[NEWS] Save error: {e}")


def on_message(_ws: Any, message: str) -> None:
    try:
        data = json.loads(message)
        content = (data.get("data") or {}).get("content") or {}
        if not content:
            return

        title = str(content.get("title") or "")
        body = str(content.get("body") or "")
        channels = content.get("channels") or []
        tags = content.get("tags") or []
        securities = content.get("securities") or []
        created_at = str(content.get("created_at") or "")

        if not title:
            return

        sentiment = score_sentiment(title, body)
        impact = classify_impact(channels, tags, sentiment)

        if impact == "NOISE":
            return

        tickers = [str(s.get("symbol", "")).strip() for s in securities if isinstance(s, dict) and s.get("symbol")]

        alert: dict[str, Any] = {
            "id": content.get("id"),
            "timestamp": created_at or datetime.now(timezone.utc).isoformat(),
            "received_at": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "teaser": str(content.get("teaser") or ""),
            "sentiment": sentiment,
            "impact": impact,
            "channels": channels if isinstance(channels, list) else [],
            "tags": tags if isinstance(tags, list) else [],
            "tickers": tickers,
            "url": str(content.get("url") or ""),
            "actionable": abs(sentiment) >= 0.4,
        }

        save_news_alert(alert)

        current = {
            "latest_alert": alert,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "high_impact_active": ("HIGH" in str(impact)) and abs(sentiment) >= 0.5,
        }
        save_json(NEWS_FILE, current)
    except Exception as e:  # noqa: BLE001
        log(f"[NEWS] Message error: {e}")


def on_error(_ws: Any, error: Any) -> None:
    log(f"[NEWS] WebSocket error: {error}")


def on_close(_ws: Any, close_status: Any, close_msg: Any) -> None:
    log(f"[NEWS] Connection closed: {close_status} {close_msg}")


def on_open(_ws: Any) -> None:
    log("[NEWS] Benzinga WebSocket connected")
    log("[NEWS] Streaming live news...")


def start_stream() -> None:
    if not BENZINGA_KEY:
        log("[NEWS] No Benzinga API key — news stream disabled")
        return

    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:  # noqa: BLE001
            log(f"[NEWS] run_forever error: {e}")
        log("[NEWS] Reconnecting in 5 seconds...")
        time.sleep(5)


def start_news_stream_thread() -> threading.Thread | None:
    global _stream_thread
    with _stream_lock:
        if not BENZINGA_KEY:
            log("[NEWS] No Benzinga API key — not starting stream thread")
            return None
        if _stream_thread is not None and _stream_thread.is_alive():
            return _stream_thread
        t = threading.Thread(target=start_stream, daemon=True, name="BenzingaNewsStream")
        t.start()
        _stream_thread = t
        log("[NEWS] News stream thread started")
        return t


def get_latest_news() -> dict[str, Any]:
    data = load_json(NEWS_FILE, default={})
    return data if isinstance(data, dict) else {}


def get_recent_alerts(minutes: int = 30, min_sentiment: float = 0.3) -> list[dict[str, Any]]:
    try:
        alerts = load_json(ALERT_FILE, default=[])
        if not isinstance(alerts, list):
            return []

        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - (minutes * 60)

        recent: list[dict[str, Any]] = []
        for a in alerts:
            if not isinstance(a, dict):
                continue
            try:
                ts_raw = str(a.get("timestamp") or a.get("received_at") or "")
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.timestamp() > cutoff and abs(float(a.get("sentiment", 0) or 0)) >= min_sentiment:
                    recent.append(a)
            except Exception:  # noqa: BLE001
                continue

        return sorted(recent, key=lambda x: str(x.get("timestamp", "")), reverse=True)
    except Exception as e:  # noqa: BLE001
        log(f"[NEWS] Get alerts error: {e}")
        return []
