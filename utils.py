"""Shared utilities for APEX: logging, file IO, async HTTP helpers."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent

# All persisted JSON: Railway volume at /data (override with APEX_RESULTS_DIR). Local
# fallback to ./results if /data is not writable.
_DATA_PRIMARY = Path(os.environ.get("APEX_RESULTS_DIR", "/data")).expanduser()
DATA_DIR = _DATA_PRIMARY.resolve()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "daily_picks").mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = (ROOT_DIR / "results").resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "daily_picks").mkdir(parents=True, exist_ok=True)

# Alias kept for modules that still import RESULTS_DIR.
RESULTS_DIR = DATA_DIR

LOGS_DIR = ROOT_DIR / "logs"
CONFIG_DIR = ROOT_DIR / "config"
for _d in (LOGS_DIR, CONFIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return a configured singleton logger that writes to logs/apex.log and stdout."""
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("apex")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOGS_DIR / "apex.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    _logger = logger
    return logger


def log(message: str, level: str = "info") -> None:
    """Convenience function used across modules."""
    logger = get_logger()
    getattr(logger, level.lower(), logger.info)(message)


def save_json(path: str | Path, data: Any) -> Path | None:
    """Write `data` as JSON atomically (temp file + rename). Logs and returns None on failure."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(p))
        return p
    except Exception as e:  # noqa: BLE001
        log(f"[IO] save error {path}: {e}")
        return None


def load_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON from `path`. On missing file or any error, log and return `default`."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT_DIR / p
        if not p.exists():
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"[IO] load error {path}: {e}")
        return default


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")
