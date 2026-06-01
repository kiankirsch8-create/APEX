"""Defaults and hints for APEX live dashboards (chrono scan status, polling, copy).

This module is the single place to tune client-facing live behaviour without
digging through ``continuous_backtester`` or ``api``. Import values where
needed (e.g. FastAPI handlers or frontend env generation).
"""

from __future__ import annotations

# --- HTTP / UI polling ---------------------------------------------------------

# Suggested interval (seconds) for polling ``GET /api/chrono/live`` and related
# endpoints. Docstrings in ``api`` historically referenced ~3s.
LIVE_POLL_INTERVAL_SEC: float = 3.0

# Soft cap for how long a UI might show "stale" live data before warning (seconds).
LIVE_STALE_AFTER_SEC: float = 120.0

# --- Documented live API paths (reference only) ------------------------------

CHRONO_LIVE_PATH: str = "/api/chrono/live"
CHRONO_ACTIVE_PATH: str = "/api/chrono/active"
CHRONO_START_PATH: str = "/api/chrono/start"
CHRONO_STOP_PATH_TEMPLATE: str = "/api/chrono/{job_id}/stop"

# v7.6 MT5 live trader (VPS writes logs; API reads via ``APEX_LIVE_V76_DIR``)
LIVE_V76_LOGS_PATH: str = "/api/live/logs"
LIVE_V76_STATUS_PATH: str = "/api/live/status"
LIVE_V76_POLL_INTERVAL_SEC: float = 3.0

# --- Display -----------------------------------------------------------------

APP_LIVE_LABEL: str = "APEX live"
