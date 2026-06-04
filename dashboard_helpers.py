"""Read-only helpers for APEX master dashboard and Siri summary."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from utils import DATA_DIR, load_json, utcnow_iso

LIVE_START_DATE = "2026-06-01"
LIVE_START_CAPITAL = 96908.79
LIVE_START_CURRENCY = "EUR"

BENCHMARK_CAPITAL: list[tuple[str, float]] = [
    ("2020-01-01", 10000.0),
    ("2020-06-01", 26930.0),
    ("2020-12-01", 29027.0),
    ("2021-01-01", 30111.0),
    ("2021-06-01", 36623.0),
    ("2021-12-01", 35969.0),
    ("2022-01-01", 36441.0),
    ("2022-03-01", 56867.0),
    ("2022-06-01", 110659.0),
]

CHRONO_PROGRESS_START = date(2020, 1, 1)
CHRONO_PROGRESS_END = date(2022, 6, 30)


def _live_v76_data_dir() -> Any:
    from pathlib import Path

    raw = os.environ.get("APEX_LIVE_V76_DIR") or os.environ.get("APEX_DATA_DIR") or str(DATA_DIR)
    return Path(raw).resolve()


def read_live_status_snapshot() -> dict[str, Any]:
    try:
        import apex_trader_v76 as v76

        return v76.get_live_status_api()
    except Exception:  # noqa: BLE001
        path = _live_v76_data_dir() / "apex_v76_live_status.json"
        if path.is_file():
            data = load_json(path, default=None)
            if isinstance(data, dict):
                data.setdefault("source", "file")
                return data
        return {"status": "offline", "source": "missing"}


def read_live_logs_text(max_lines: int = 50) -> str:
    n = max(1, min(int(max_lines), 500))
    try:
        import apex_trader_v76 as v76

        payload = v76.get_live_logs_api(n)
        lines = payload.get("lines") or []
        return "\n".join(str(x) for x in lines)
    except Exception:  # noqa: BLE001
        path = _live_v76_data_dir() / "apex_v76_live.log"
        if not path.is_file():
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = f.readlines()
            return "".join(rows[-n:]).rstrip("\n")
        except OSError:
            return ""


def read_chrono_active() -> dict[str, Any]:
    import continuous_backtester as cb

    active = cb.get_active_chrono()
    live = cb.CHRONO_LIVE_STATUS.copy()
    if not active:
        return {"active": False, "live_status": live}
    job_id = str(active.get("job_id", "") or "").strip()
    if not job_id:
        return {"active": False, "live_status": live}
    chrono_file = cb.chrono_results_path(job_id)
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
                "start_date": data.get("start_date"),
                "end_date": data.get("end_date"),
                "daily_pnl": data.get("daily_pnl", []),
                "summary": data.get("summary", {}),
                "live_status": live,
            }
    return {"active": True, "job_id": job_id, "current_date": None, "live_status": live}


def benchmark_capital_at(as_of: str | None) -> float | None:
    if not as_of:
        return None
    try:
        d = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return None
    points = [(date.fromisoformat(ds), cap) for ds, cap in BENCHMARK_CAPITAL]
    if d <= points[0][0]:
        return points[0][1]
    if d >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        d0, c0 = points[i]
        d1, c1 = points[i + 1]
        if d0 <= d <= d1:
            span = (d1 - d0).days or 1
            frac = (d - d0).days / span
            return c0 + (c1 - c0) * frac
    return None


def chrono_progress_pct(current_date: str | None) -> float:
    if not current_date:
        return 0.0
    try:
        d = date.fromisoformat(str(current_date)[:10])
    except ValueError:
        return 0.0
    span = (CHRONO_PROGRESS_END - CHRONO_PROGRESS_START).days or 1
    done = (d - CHRONO_PROGRESS_START).days
    return round(max(0.0, min(100.0, done / span * 100.0)), 1)


def _fmt_money(v: float | None, currency: str = "USD") -> str:
    if v is None:
        return "unknown"
    sym = "€" if currency.upper() == "EUR" else "$"
    return f"{sym}{v:,.0f}"


def _month_year_label(d: str | None) -> str:
    if not d:
        return "unknown date"
    try:
        dt = date.fromisoformat(str(d)[:10])
        return dt.strftime("%B %Y")
    except ValueError:
        return str(d)


def build_dashboard_summary_text() -> str:
    live = read_live_status_snapshot()
    chrono = read_chrono_active()

    equity = live.get("equity")
    if equity is None:
        equity = live.get("balance")
    try:
        equity_f = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        equity_f = None

    daily_pnl = live.get("daily_pnl")
    try:
        daily_f = float(daily_pnl) if daily_pnl is not None else 0.0
    except (TypeError, ValueError):
        daily_f = 0.0

    open_positions = live.get("open_positions") or live.get("open_count")
    if isinstance(open_positions, list):
        open_n = len(open_positions)
    else:
        try:
            open_n = int(open_positions or 0)
        except (TypeError, ValueError):
            open_n = 0

    period_mode = str(live.get("period_mode") or "unknown").strip().lower()
    halt = live.get("circuit_halt_until") or live.get("circuit_halt")
    halted = bool(halt)

    cap = chrono.get("capital")
    try:
        cap_f = float(cap) if cap is not None else None
    except (TypeError, ValueError):
        cap_f = None
    cur_d = chrono.get("current_date")

    parts: list[str] = ["APEX Status:"]
    if equity_f is not None:
        rel = equity_f - LIVE_START_CAPITAL
        parts.append(
            f"Live equity {_fmt_money(equity_f, LIVE_START_CURRENCY)}, "
            f"{'up' if daily_f >= 0 else 'down'} {_fmt_money(abs(daily_f), LIVE_START_CURRENCY)} today."
        )
    else:
        parts.append("Live trader offline or no equity data.")

    if cap_f is not None and cur_d:
        parts.append(f"Backtest at {_month_year_label(cur_d)}, capital {_fmt_money(cap_f, 'USD')}.")
    elif chrono.get("active"):
        parts.append("Backtest running.")
    else:
        parts.append("Backtest idle.")

    parts.append(f"{open_n} open position{'s' if open_n != 1 else ''}.")
    parts.append(f"Period mode {period_mode}.")
    if halted:
        parts.append("Circuit breaker active.")
    else:
        parts.append("All systems running.")

    return " ".join(parts)


def dashboard_config() -> dict[str, Any]:
    base = (os.environ.get("APEX_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    live_base = (os.environ.get("APEX_LIVE_BASE_URL") or base or "").strip().rstrip("/")
    return {
        "api_base": base or None,
        "live_base": live_base or None,
        "live_start_date": LIVE_START_DATE,
        "live_start_capital": LIVE_START_CAPITAL,
        "live_start_currency": LIVE_START_CURRENCY,
        "benchmark_capital": [{"date": d, "capital": c} for d, c in BENCHMARK_CAPITAL],
        "chrono_progress_start": CHRONO_PROGRESS_START.isoformat(),
        "chrono_progress_end": CHRONO_PROGRESS_END.isoformat(),
        "updated_at": utcnow_iso(),
    }
