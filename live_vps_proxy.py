"""Fetch v7.6 live status and logs from the Windows VPS (proxy for Railway dashboard)."""

from __future__ import annotations

import os
import re
from typing import Any

import requests

from utils import DATA_DIR, load_json, utcnow_iso

DEFAULT_VPS_LIVE_URL = "http://192.248.191.134:8000"
_FETCH_TIMEOUT = float(os.environ.get("APEX_VPS_FETCH_TIMEOUT", "8"))


def vps_live_base_url() -> str:
    raw = (os.environ.get("APEX_VPS_LIVE_URL") or os.environ.get("APEX_LIVE_BASE_URL") or "").strip()
    if raw.lower() in ("0", "false", "no", "off", "disabled"):
        return ""
    if raw:
        return raw.rstrip("/")
    if os.environ.get("APEX_VPS_LIVE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    return DEFAULT_VPS_LIVE_URL


def _live_data_dir() -> Any:
    from pathlib import Path

    raw = os.environ.get("APEX_LIVE_V76_DIR") or os.environ.get("APEX_DATA_DIR") or str(DATA_DIR)
    return Path(raw).resolve()


def _http_get(url: str, *, as_text: bool = False) -> Any | None:
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    if as_text:
        return resp.text
    try:
        return resp.json()
    except ValueError:
        return None


def _fetch_vps_status_json() -> dict[str, Any] | None:
    base = vps_live_base_url()
    if not base:
        return None
    for path in (
        "/api/live/status",
        "/apex_v76_live_status.json",
        "/live/apex_v76_live_status.json",
    ):
        data = _http_get(f"{base}{path}")
        if isinstance(data, dict) and data:
            data.setdefault("source", f"vps:{path}")
            data.setdefault("vps_url", base)
            return data
    return None


def _fetch_vps_logs_text(max_lines: int) -> str | None:
    base = vps_live_base_url()
    if not base:
        return None
    n = max(1, min(int(max_lines), 5000))
    for path in (
        f"/api/live/logs/text?lines={n}",
        "/apex_v76_live.log",
        "/live/apex_v76_live.log",
    ):
        text = _http_get(f"{base}{path}", as_text=True)
        if isinstance(text, str) and text.strip():
            lines = text.splitlines()
            if len(lines) > n:
                lines = lines[-n:]
            return "\n".join(lines)
    return None


def _read_local_status_file() -> dict[str, Any] | None:
    path = _live_data_dir() / "apex_v76_live_status.json"
    if not path.is_file():
        return None
    data = load_json(path, default=None)
    if isinstance(data, dict):
        data.setdefault("source", "local_file")
        data["status_path"] = str(path)
        return data
    return None


def _read_local_log_text(max_lines: int) -> str:
    path = _live_data_dir() / "apex_v76_live.log"
    if not path.is_file():
        return ""
    n = max(1, min(int(max_lines), 5000))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        return "".join(rows[-n:]).rstrip("\n")
    except OSError:
        return ""


def _parse_float_field(line: str, key: str) -> float | None:
    m = re.search(rf"(?:^|\s|,\s*){re.escape(key)}=(-?\d+(?:\.\d+)?)", line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def enrich_live_status_from_log(data: dict[str, Any], log_text: str) -> dict[str, Any]:
    """Fill missing balance/equity/daily_pnl from recent ``[SCAN CYCLE] context`` log lines."""
    if not log_text:
        return data
    out = dict(data)
    for line in reversed(log_text.splitlines()):
        if out.get("balance") is None:
            v = _parse_float_field(line, "balance")
            if v is not None:
                out["balance"] = v
        if out.get("equity") is None:
            v = _parse_float_field(line, "equity")
            if v is not None:
                out["equity"] = v
        if out.get("daily_pnl") is None:
            v = _parse_float_field(line, "day_pnl")
            if v is not None:
                out["daily_pnl"] = v
        if out.get("period_mode") is None and "period_mode=" in line:
            m = re.search(r"period_mode=(\w+)", line)
            if m:
                out["period_mode"] = m.group(1)
        if out.get("balance") is not None and out.get("equity") is not None:
            break
    return out


def read_live_status() -> dict[str, Any]:
    """
    Priority: VPS HTTP → local JSON file → in-process v76 module → log enrichment.
    """
    data: dict[str, Any] | None = None
    log_text = ""

    vps = _fetch_vps_status_json()
    if vps:
        data = vps
        log_text = _fetch_vps_logs_text(120) or ""

    if data is None:
        data = _read_local_status_file()

    if data is None:
        try:
            import apex_trader_v76 as v76

            data = v76.get_live_status_api()
            if isinstance(data, dict):
                data.setdefault("source", "in_process")
        except Exception:  # noqa: BLE001
            data = None

    if data is None:
        data = {
            "status": "offline",
            "source": "missing",
            "vps_url": vps_live_base_url() or None,
            "detail": "No live status from VPS, local file, or trader process.",
        }

    if not log_text:
        log_text = _fetch_vps_logs_text(120) or _read_local_log_text(120)

    data = enrich_live_status_from_log(data, log_text)
    data["live_fetched_at"] = utcnow_iso()
    if vps_live_base_url():
        data.setdefault("vps_url", vps_live_base_url())
    return data


def read_live_logs(max_lines: int = 50) -> dict[str, Any]:
    n = max(1, min(int(max_lines), 10000))
    lines: list[str] = []
    source = "missing"

    text = _fetch_vps_logs_text(n)
    if text:
        lines = text.splitlines()
        source = "vps"

    if not lines:
        try:
            import apex_trader_v76 as v76

            payload = v76.get_live_logs_api(n)
            lines = list(payload.get("lines") or [])
            if lines:
                source = "in_process"
        except Exception:  # noqa: BLE001
            pass

    if not lines:
        local = _read_local_log_text(n)
        if local:
            lines = local.splitlines()
            source = "local_file"

    return {
        "lines": lines,
        "count": len(lines),
        "source": source,
        "vps_url": vps_live_base_url() or None,
        "updated_at": utcnow_iso(),
    }
