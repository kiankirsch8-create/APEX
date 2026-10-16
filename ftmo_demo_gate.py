"""FTMO demo → live gate.

The challenge login (``511443540``) is blocked until a clean demo run is
recorded. Demo must run on the same broker/server family as the FTMO challenge
for a minimum of ``DEMO_MIN_TRADING_DAYS`` with all Prompt 1–3 guardrails
observed and no day ≤ −5% / total DD ≤ −10%.

Never commit passwords. Credentials come only from environment variables.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Challenge account — LIVE only after demo clean. Do not default the engine here.
FTMO_CHALLENGE_LOGIN: int = 511443540

# Same broker/server family as the FTMO challenge (override via env if needed).
FTMO_DEMO_SERVER_DEFAULT: str = os.environ.get("APEX_FTMO_DEMO_SERVER", "FTMO-Server")
FTMO_LIVE_SERVER_DEFAULT: str = os.environ.get("APEX_FTMO_LIVE_SERVER", "FTMO-Server")

DEMO_MIN_TRADING_DAYS: int = 3
DEMO_TARGET_TRADING_DAYS: int = 5
DEMO_MAX_DAY_LOSS_PCT: float = 5.0
DEMO_MAX_TOTAL_DD_PCT: float = 10.0

REQUIRED_GUARDRAIL_EVENTS: tuple[str, ...] = (
    "margin_guard",
    "pair_corr_cap",
    "daily_loss_halt",
    "dd_ladder",
    "entry_with_sl",
)

# Unlock file written only by ``mark_demo_clean`` after acceptance checks pass.
_DEFAULT_UNLOCK = Path(
    os.environ.get("APEX_FTMO_DEMO_UNLOCK_FILE")
    or str(Path(__file__).resolve().parent / "results" / "ftmo_demo_clean.json")
)
_DEFAULT_STATUS = Path(
    os.environ.get("APEX_FTMO_DEMO_STATUS_FILE")
    or str(Path(__file__).resolve().parent / "results" / "ftmo_demo_status.json")
)


def demo_status_path() -> Path:
    return Path(os.environ.get("APEX_FTMO_DEMO_STATUS_FILE") or _DEFAULT_STATUS)


def demo_unlock_path() -> Path:
    return Path(os.environ.get("APEX_FTMO_DEMO_UNLOCK_FILE") or _DEFAULT_UNLOCK)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def empty_demo_status() -> dict[str, Any]:
    return {
        "mode": "demo",
        "profile": "ftmo",
        "started_at": None,
        "trading_days": [],
        "guardrail_events": {k: [] for k in REQUIRED_GUARDRAIL_EVENTS},
        "entries_with_sl": 0,
        "entries_missing_sl": 0,
        "worst_day_loss_pct": 0.0,
        "worst_total_dd_pct": 0.0,
        "breaches": [],
        "clean": False,
        "updated_at": None,
    }


def load_demo_status() -> dict[str, Any]:
    path = demo_status_path()
    if not path.is_file():
        return empty_demo_status()
    st = empty_demo_status()
    st.update(_load_json(path))
    events = st.get("guardrail_events")
    if not isinstance(events, dict):
        events = {}
    for k in REQUIRED_GUARDRAIL_EVENTS:
        events.setdefault(k, [])
    st["guardrail_events"] = events
    return st


def save_demo_status(st: dict[str, Any]) -> None:
    st = dict(st)
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(demo_status_path(), st)


def record_trading_day(day: date | str, *, day_pnl_pct: float, total_dd_pct: float) -> dict[str, Any]:
    """Append/update a trading day and flag −5% / −10% breaches."""
    st = load_demo_status()
    if st.get("started_at") is None:
        st["started_at"] = datetime.now(timezone.utc).isoformat()
    ds = day.isoformat() if isinstance(day, date) else str(day)[:10]
    days = [d for d in (st.get("trading_days") or []) if isinstance(d, dict)]
    row = {
        "date": ds,
        "day_pnl_pct": round(float(day_pnl_pct), 4),
        "total_dd_pct": round(float(total_dd_pct), 4),
    }
    replaced = False
    for i, d in enumerate(days):
        if str(d.get("date") or "")[:10] == ds:
            days[i] = row
            replaced = True
            break
    if not replaced:
        days.append(row)
    st["trading_days"] = days
    st["worst_day_loss_pct"] = min(
        float(st.get("worst_day_loss_pct") or 0.0),
        float(day_pnl_pct),
    )
    st["worst_total_dd_pct"] = max(
        float(st.get("worst_total_dd_pct") or 0.0),
        float(total_dd_pct),
    )
    breaches = list(st.get("breaches") or [])
    if float(day_pnl_pct) <= -float(DEMO_MAX_DAY_LOSS_PCT):
        breaches.append({"date": ds, "type": "day_loss", "pct": float(day_pnl_pct)})
    if float(total_dd_pct) >= float(DEMO_MAX_TOTAL_DD_PCT):
        breaches.append({"date": ds, "type": "total_dd", "pct": float(total_dd_pct)})
    st["breaches"] = breaches
    save_demo_status(st)
    return st


def record_guardrail_event(kind: str, *, detail: str = "", **extra: Any) -> dict[str, Any]:
    """Record that a Prompt 1–3 guardrail (or SL-on-entry) fired on demo."""
    st = load_demo_status()
    events = st.setdefault("guardrail_events", {})
    if kind not in events or not isinstance(events.get(kind), list):
        events[kind] = []
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
        **{k: v for k, v in extra.items() if v is not None},
    }
    events[kind].append(entry)
    # Cap history
    events[kind] = events[kind][-50:]
    if kind == "entry_with_sl":
        st["entries_with_sl"] = int(st.get("entries_with_sl") or 0) + 1
    save_demo_status(st)
    return st


def record_missing_sl(**extra: Any) -> dict[str, Any]:
    st = load_demo_status()
    st["entries_missing_sl"] = int(st.get("entries_missing_sl") or 0) + 1
    events = st.setdefault("guardrail_events", {})
    events.setdefault("entry_with_sl", [])
    save_demo_status(st)
    return st


def demo_acceptance(st: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Return (ok, reasons) for a clean 3–5 day demo."""
    st = st or load_demo_status()
    reasons: list[str] = []
    days = [d for d in (st.get("trading_days") or []) if isinstance(d, dict)]
    if len(days) < DEMO_MIN_TRADING_DAYS:
        reasons.append(
            f"need ≥{DEMO_MIN_TRADING_DAYS} trading days (have {len(days)}; "
            f"target {DEMO_TARGET_TRADING_DAYS})"
        )
    events = st.get("guardrail_events") or {}
    for kind in REQUIRED_GUARDRAIL_EVENTS:
        rows = events.get(kind) or []
        if kind == "entry_with_sl":
            if int(st.get("entries_with_sl") or 0) < 1 and not rows:
                reasons.append("no entry_with_sl observed (every trade must attach SL)")
            continue
        if not rows:
            reasons.append(f"guardrail never fired on demo: {kind}")
    if int(st.get("entries_missing_sl") or 0) > 0:
        reasons.append(f"entries_missing_sl={st.get('entries_missing_sl')} (must be 0)")
    if list(st.get("breaches") or []):
        reasons.append("demo had day≤-5% or total DD≥10% breach — not clean")
    if float(st.get("worst_day_loss_pct") or 0) <= -DEMO_MAX_DAY_LOSS_PCT:
        reasons.append(
            f"worst_day_loss_pct={st.get('worst_day_loss_pct')} breached -{DEMO_MAX_DAY_LOSS_PCT}%"
        )
    if float(st.get("worst_total_dd_pct") or 0) >= DEMO_MAX_TOTAL_DD_PCT:
        reasons.append(
            f"worst_total_dd_pct={st.get('worst_total_dd_pct')} reached {DEMO_MAX_TOTAL_DD_PCT}%"
        )
    return (len(reasons) == 0), reasons


def mark_demo_clean(*, force: bool = False) -> tuple[bool, list[str]]:
    """Write unlock file only when acceptance passes (or force for tests)."""
    st = load_demo_status()
    ok, reasons = demo_acceptance(st)
    if not ok and not force:
        return False, reasons
    st["clean"] = True
    save_demo_status(st)
    unlock = {
        "clean": True,
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "trading_days": len(st.get("trading_days") or []),
        "guardrail_events": {
            k: len(v or []) for k, v in (st.get("guardrail_events") or {}).items()
        },
        "worst_day_loss_pct": st.get("worst_day_loss_pct"),
        "worst_total_dd_pct": st.get("worst_total_dd_pct"),
        "challenge_login_allowed": FTMO_CHALLENGE_LOGIN,
        "note": "Live FTMO login unlocked. Set APEX_MT5_LOGIN to challenge account only after review.",
    }
    _save_json(demo_unlock_path(), unlock)
    return True, []


def is_demo_clean() -> bool:
    path = demo_unlock_path()
    if not path.is_file():
        return False
    data = _load_json(path)
    return bool(data.get("clean"))


def assert_login_allowed(login: int | str, *, server: str = "") -> None:
    """
    Raise ``PermissionError`` if ``login`` is the FTMO challenge account and
    demo has not been marked clean.

    Set ``APEX_FTMO_ALLOW_LIVE=1`` only together with a valid unlock file —
    the env flag alone is not enough.
    """
    try:
        lid = int(str(login).strip())
    except (TypeError, ValueError):
        return
    if lid != FTMO_CHALLENGE_LOGIN:
        return
    env_allow = os.environ.get("APEX_FTMO_ALLOW_LIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if env_allow and is_demo_clean():
        return
    raise PermissionError(
        f"FTMO challenge login {FTMO_CHALLENGE_LOGIN} is blocked until a clean "
        f"demo run is recorded ({demo_unlock_path()}). Run PROFILE=ftmo on an "
        f"MT5 DEMO account on {FTMO_DEMO_SERVER_DEFAULT} for "
        f"{DEMO_MIN_TRADING_DAYS}–{DEMO_TARGET_TRADING_DAYS} trading days, "
        f"confirm Prompt 1–3 guardrails + SL-on-entry, then "
        f"`python tools/ftmo_demo_validate.py --mark-clean`. "
        f"Do NOT go live if any guardrail failed to fire. "
        f"(APEX_FTMO_ALLOW_LIVE={env_allow}, unlock={is_demo_clean()})"
    )


def resolve_mt5_login_server() -> tuple[int, str]:
    """
    Resolve login/server for the current phase.

    Demo default: require explicit ``APEX_MT5_LOGIN`` (never the challenge id
    unless unlocked). Server defaults to FTMO-Server.
    """
    raw_login = os.environ.get("APEX_MT5_LOGIN") or os.environ.get("MT5_LOGIN") or ""
    raw_server = (
        os.environ.get("APEX_MT5_SERVER")
        or os.environ.get("MT5_SERVER")
        or FTMO_DEMO_SERVER_DEFAULT
    )
    if not str(raw_login).strip():
        raise PermissionError(
            "APEX_MT5_LOGIN is not set. For the FTMO demo phase set it to a "
            f"DEMO account on {FTMO_DEMO_SERVER_DEFAULT} (same broker/server as "
            f"the challenge). Challenge login {FTMO_CHALLENGE_LOGIN} is blocked "
            "until demo is clean."
        )
    login = int(str(raw_login).strip())
    assert_login_allowed(login, server=str(raw_server))
    return login, str(raw_server).strip()
