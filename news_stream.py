"""News alert module (Benzinga removed). Public API kept for api.py and intraday_backtester."""
from __future__ import annotations

import threading
from typing import Any

from utils import DATA_DIR, log

BENZINGA_KEY = ""
NEWS_FILE = DATA_DIR / "live_news.json"
ALERT_FILE = DATA_DIR / "news_alerts.json"

_stream_thread: threading.Thread | None = None
_stream_lock = threading.Lock()
_disabled_logged = False


def start_stream() -> None:
    global _disabled_logged
    if not _disabled_logged:
        log("[NEWS] Benzinga removed — news stream disabled", level="info")
        _disabled_logged = True


def start_news_stream_thread() -> threading.Thread | None:
    start_stream()
    return None


def get_latest_news() -> dict[str, Any]:
    return {}


def get_recent_alerts(minutes: int = 30, min_sentiment: float = 0.3) -> list[dict[str, Any]]:  # noqa: ARG001
    return []
