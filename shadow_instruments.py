"""
Shadow instrument evaluation for chrono backtests — isolated from the real curve.

All contract specs in SHADOW_INSTRUMENTS are ESTIMATES. Correct in one place below.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

from utils import DATA_DIR, log

# ── ESTIMATED non-forex specs (correct here only) ─────────────────────────────
SHADOW_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "XAUUSD": {
        "source": "GC=F",
        "contract_size": 100,
        "tick": 0.10,
        "tick_value": 10.0,
        "cost_model": "cfd",
        "spread_est": 0.30,
        "financing_pct_yr": 3.0,
    },
    "WTI": {
        "source": "CL=F",
        "contract_size": 1000,
        "tick": 0.01,
        "tick_value": 10.0,
        "cost_model": "cfd",
        "spread_est": 0.03,
        "financing_pct_yr": 3.0,
    },
    "ES": {
        "source": "ES=F",
        "contract_size": 50,
        "tick": 0.25,
        "tick_value": 12.50,
        "cost_model": "futures",
        "commission_per_contract": 2.50,
    },
    "NQ": {
        "source": "NQ=F",
        "contract_size": 20,
        "tick": 0.25,
        "tick_value": 5.00,
        "cost_model": "futures",
        "commission_per_contract": 2.50,
    },
}

SHADOW_INSTRUMENTS_FILE = DATA_DIR / "shadow_instruments.jsonl"
SHADOW_PROBE_CACHE_FILE = DATA_DIR / "shadow_universe_probe.json"

SHADOW_MAX_EXTRA_FX = 25
SHADOW_UNIVERSE_HARD_CAP = 45
PROBE_BATCH_SLEEP_SEC = 0.5

# Majors + traded crosses for Part 2 extra_fx discovery only (not Part 1 blocked pairs).
SHADOW_FX_EXTRA_CURRENCIES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "AUD",
    "NZD",
    "CAD",
    "SEK",
    "NOK",
    "ZAR",
)

SHADOW_FX_CURRENCIES: tuple[str, ...] = SHADOW_FX_EXTRA_CURRENCIES + (
    "MXN",
    "PLN",
    "TRY",
    "HUF",
    "SGD",
    "HKD",
)

PART1_DATA_EXCLUDED_FX: frozenset[str] = frozenset(
    {"AUDCAD", "AUDNZD", "EURCAD", "EURJPY", "GBPAUD", "NZDJPY"}
)

MIN_SHADOW_DAILY_BARS = 200
MIN_SHADOW_WEEKLY_BARS = 40

ShadowClass = str

_AB_CLASSES: tuple[ShadowClass, ...] = (
    "blocked_fx",
    "extra_fx",
    "commodity",
    "index_future",
)

_shadow_strat_by_class: dict[ShadowClass, dict[str, dict[str, Any]]] = {
    c: {} for c in _AB_CLASSES
}
_shadow_st_medium_by_class: dict[ShadowClass, dict[str, Any]] = {
    c: {"n": 0, "last3": []} for c in _AB_CLASSES
}

_shadow_eval_ticker: str | None = None
_shadow_eval_class: ShadowClass | None = None

_shadow_universe: dict[str, Any] = {
    "tickers": set(),
    "class_by_ticker": {},
    "yf_by_ticker": {},
    "spec_by_ticker": {},
    "failed": {},
    "provisional_futures": set(),
    "real_tickers": set(),
    "discovery": {},
}


def reset_run_state() -> None:
    for c in _AB_CLASSES:
        _shadow_strat_by_class[c].clear()
        _shadow_st_medium_by_class[c] = {"n": 0, "last3": []}


def _strat_history_dict(shadow_class: ShadowClass) -> dict[str, dict[str, Any]]:
    raw = _shadow_strat_by_class.get(shadow_class, {})
    out: dict[str, dict[str, Any]] = {}
    for sid, entry in raw.items():
        if isinstance(entry, dict):
            last3_raw = entry.get("last3") or []
            out[sid] = {
                "n": int(entry.get("n", 0) or 0),
                "last3": [float(x) for x in last3_raw][-3:]
                if isinstance(last3_raw, list)
                else [],
            }
        elif isinstance(entry, list):
            out[sid] = {"n": len(entry), "last3": [float(x) for x in entry[-3:]]}
    return out


def _record_strat_pnl_for_class(shadow_class: ShadowClass, sid: str, pnl: float) -> None:
    key = str(sid or "").strip().upper()
    if not key or key == "SKIP":
        return
    hist = _shadow_strat_by_class.setdefault(shadow_class, {})
    entry = hist.get(key)
    if isinstance(entry, list):
        entry = {"n": len(entry), "last3": [float(x) for x in entry[-3:]]}
    elif not isinstance(entry, dict):
        entry = {"n": 0, "last3": []}
    last3 = [float(x) for x in (entry.get("last3") or [])]
    last3.append(float(pnl))
    entry["n"] = int(entry.get("n", 0) or 0) + 1
    entry["last3"] = last3[-3:]
    hist[key] = entry


def _record_st_medium_for_class(shadow_class: ShadowClass, pnl: float, won: bool) -> None:
    hist = _shadow_st_medium_by_class.setdefault(shadow_class, {"n": 0, "last3": []})
    if "n" not in hist or "last3" not in hist:
        hist.clear()
        hist.update({"n": 0, "last3": []})
    last3 = list(hist.get("last3") or [])
    last3.append([float(pnl), bool(won)])
    hist["last3"] = last3[-3:]
    hist["n"] = int(hist.get("n", 0) or 0) + 1


def active_shadow_class() -> ShadowClass | None:
    return _shadow_eval_class


def in_shadow_eval(sym: str | None = None) -> bool:
    if not _shadow_eval_ticker:
        return False
    if sym is None:
        return True
    return _shadow_eval_ticker == str(sym).strip().upper()


def enter_shadow_eval(sym: str, *, shadow_class: ShadowClass) -> None:
    global _shadow_eval_ticker, _shadow_eval_class
    _shadow_eval_ticker = str(sym or "").strip().upper()
    _shadow_eval_class = shadow_class


def exit_shadow_eval() -> None:
    global _shadow_eval_ticker, _shadow_eval_class
    _shadow_eval_ticker = None
    _shadow_eval_class = None


def shadow_class_for_ticker(sym: str) -> ShadowClass | None:
    return _shadow_universe.get("class_by_ticker", {}).get(str(sym).strip().upper())


def is_shadow_ticker(sym: str) -> bool:
    return str(sym).strip().upper() in _shadow_universe.get("tickers", set())


def yf_symbol_for(sym: str) -> str:
    sym_u = str(sym).strip().upper()
    yf_map = _shadow_universe.get("yf_by_ticker", {})
    if sym_u in yf_map:
        return str(yf_map[sym_u])
    if sym_u in SHADOW_INSTRUMENTS:
        return str(SHADOW_INSTRUMENTS[sym_u]["source"])
    if len(sym_u) == 6 and sym_u.isalpha():
        return f"{sym_u}=X"
    return sym_u


def instrument_spec_for(sym: str) -> dict[str, Any] | None:
    sym_u = str(sym).strip().upper()
    if sym_u in SHADOW_INSTRUMENTS:
        return dict(SHADOW_INSTRUMENTS[sym_u])
    return None


def is_provisional_future(sym: str) -> bool:
    return str(sym).strip().upper() in _shadow_universe.get("provisional_futures", set())


def extra_chrono_tickers() -> list[str]:
    real = _shadow_universe.get("real_tickers", set())
    out: list[str] = []
    for t in sorted(_shadow_universe.get("tickers", set())):
        if t not in real:
            out.append(t)
    return out


def universe_meta() -> dict[str, Any]:
    return dict(_shadow_universe.get("discovery", {}))


def _close_ts_from_row(r: Mapping[str, Any]) -> int:
    ts_raw = r.get("close_ts")
    if ts_raw is not None:
        try:
            ts = int(ts_raw)
            if ts > 0:
                return ts
        except (TypeError, ValueError):
            pass
    ct = r.get("close_time_utc")
    if ct:
        try:
            dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (TypeError, ValueError):
            pass
    return 0


def _shadow_trade_sort_key(r: Mapping[str, Any]) -> tuple[int, str, str, str]:
    ts = _close_ts_from_row(r)
    if ts <= 0:
        d = str(r.get("date", ""))[:10]
        try:
            ts = int(
                datetime.strptime(d, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            ts = 0
    return (
        ts,
        str(r.get("ticker", "")).strip().upper(),
        str(r.get("timeframe", "")).strip().lower(),
        str(r.get("strategy_id", "")).strip().upper(),
    )


def _iter_trades_for_job(job_id: str) -> Iterator[dict[str, Any]]:
    jid = str(job_id or "").strip()
    if not jid or not SHADOW_INSTRUMENTS_FILE.is_file():
        return
    try:
        with open(SHADOW_INSTRUMENTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("job_id", "")).strip() == jid:
                    yield row
    except OSError as e:
        log(f"[SHADOW INST] stream error: {e}", level="warning")


def _record_ab_histories(row: Mapping[str, Any], shadow_class: ShadowClass) -> None:
    if str(row.get("outcome", "")).strip().upper() not in ("WIN", "LOSS"):
        return
    sid = str(row.get("strategy_id", "")).strip().upper()
    pnl = float(row.get("pnl_dollars", 0) or 0)
    if sid and sid != "SKIP":
        _record_strat_pnl_for_class(shadow_class, sid, pnl)
    if (
        str(row.get("confidence", "")).strip().upper() == "MEDIUM"
        and str(row.get("macro_bias", "")).strip().upper() == "STRONG_TAILWIND"
    ):
        won = str(row.get("outcome", "")).strip().upper() == "WIN"
        _record_st_medium_for_class(shadow_class, pnl, won)


def rebuild_histories(job_id: str) -> None:
    reset_run_state()
    completed: list[dict[str, Any]] = []
    for r in _iter_trades_for_job(job_id):
        if (
            not r.get("skipped")
            and str(r.get("outcome", "")).strip().upper() in ("WIN", "LOSS")
        ):
            completed.append(r)
    completed.sort(key=_shadow_trade_sort_key)
    for r in completed:
        sc = str(r.get("shadow_class") or "blocked_fx")
        if sc not in _AB_CLASSES:
            sc = "blocked_fx"
        _record_ab_histories(r, sc)


def load_trades_for_job(job_id: str) -> list[dict[str, Any]]:
    return list(_iter_trades_for_job(job_id))


def append_trade_row(row: dict[str, Any]) -> None:
    try:
        SHADOW_INSTRUMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_INSTRUMENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
    except OSError as e:
        log(f"[SHADOW INST] append error: {e}", level="error")


def persist_shadow_trade(row: dict[str, Any], *, job_id: str) -> None:
    out = dict(row)
    sym = str(out.get("ticker", "")).strip().upper()
    sc = shadow_class_for_ticker(sym) or str(out.get("shadow_class") or "blocked_fx")
    out["shadow_instrument"] = sym
    out["shadow_class"] = sc
    out["spec_estimated"] = bool(sym in SHADOW_INSTRUMENTS)
    mrd = float(out.get("max_risk_dollars", 0) or 0)
    pnl = float(out.get("pnl_dollars", 0) or 0)
    out["pnl_r"] = round(pnl / mrd, 6) if mrd > 0 else 0.0
    out.setdefault("job_id", job_id)
    if is_provisional_future(sym):
        out["provisional_roll_series"] = True
    append_trade_row(out)
    _record_ab_histories(out, sc)


def ab_histories_for_active() -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    sc = active_shadow_class()
    if not sc:
        return None, None
    st_hist = _shadow_st_medium_by_class.get(sc, {"n": 0, "last3": []})
    return _strat_history_dict(sc), dict(st_hist)


def trade_fields_for_row(sym: str) -> dict[str, Any]:
    if not in_shadow_eval(sym):
        return {}
    sc = shadow_class_for_ticker(sym) or active_shadow_class() or "blocked_fx"
    fields: dict[str, Any] = {
        "shadow_instrument": str(sym).strip().upper(),
        "shadow_class": sc,
        "spec_estimated": sym in SHADOW_INSTRUMENTS,
    }
    if is_provisional_future(sym):
        fields["provisional_roll_series"] = True
    return fields


def _standalone_max_dd_r(r_values: list[float]) -> float:
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        cum += float(r)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return round(float(max_dd), 4)


def _ticker_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if t.get("outcome") == "WIN"]
    rs = [float(t.get("pnl_r", 0) or 0) for t in trades]
    pnls = [float(t.get("pnl_dollars", 0) or 0) for t in trades]
    net_r = sum(rs)
    n = len(trades)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate_pct": round(len(wins) / max(1, n) * 100.0, 1),
        "net_r": round(net_r, 4),
        "r_per_trade": round(net_r / max(1, n), 4),
        "best_r": round(max(rs), 4) if rs else 0.0,
        "worst_r": round(min(rs), 4) if rs else 0.0,
        "max_drawdown_r_standalone": _standalone_max_dd_r(rs),
        "estimated_dollars": {
            "net_pnl": round(sum(pnls), 2),
            "note": "ESTIMATED — contract specs may be wrong; use net_r as primary",
        },
    }


def compute_summary(job_id: str) -> dict[str, Any]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    executed = 0
    for t in _iter_trades_for_job(job_id):
        if (
            not isinstance(t, dict)
            or t.get("skipped")
            or str(t.get("outcome", "")).strip().upper() not in ("WIN", "LOSS")
        ):
            continue
        executed += 1
        tkr = str(t.get("shadow_instrument") or t.get("ticker", "")).strip().upper()
        if tkr:
            by_ticker.setdefault(tkr, []).append(t)
    summary: dict[str, Any] = {
        "by_ticker": {},
        "total_trades": executed,
        "primary_metric": "R-multiple (pnl / max_risk_dollars)",
    }
    for tkr, rows in sorted(by_ticker.items()):
        rows.sort(key=_shadow_trade_sort_key)
        stats = _ticker_stats(rows)
        by_year: dict[str, dict[str, Any]] = {}
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            yr = str(r.get("date", ""))[:4] or "unknown"
            buckets.setdefault(yr, []).append(r)
        for yr, yr_rows in sorted(buckets.items()):
            by_year[yr] = _ticker_stats(yr_rows)
        stats["by_year"] = by_year
        summary["by_ticker"][tkr] = stats
    return summary


def apply_instrument_sizing(
    ai: dict[str, Any],
    *,
    spec: Mapping[str, Any],
    entry: float,
    risk_dollars: float,
) -> None:
    stop = float(ai.get("stop_loss", 0) or 0)
    stop_dist = abs(float(entry) - stop)
    tick = float(spec.get("tick") or 0)
    tick_value = float(spec.get("tick_value") or 0)
    if tick <= 0 or tick_value <= 0 or stop_dist <= 0:
        return
    stop_ticks = stop_dist / tick
    if stop_ticks <= 0:
        return
    contracts = float(risk_dollars) / (stop_ticks * tick_value)
    contract_size = float(spec.get("contract_size") or 1)
    notional = contracts * contract_size * float(entry)
    ai["_position_size"] = round(contracts, 4)
    ai["_leveraged_exposure"] = round(notional, 2)
    ai["_max_risk_dollars"] = round(float(risk_dollars), 2)
    ai["_instrument_contracts"] = round(contracts, 4)
    ai["_instrument_spec_estimated"] = True


def apply_instrument_costs(
    *,
    spec: Mapping[str, Any],
    direction: str,
    contracts: float,
    entry: float,
    notional: float,
    raw_pct: float,
    nights_held: float,
) -> tuple[float, float, dict[str, float]]:
    gross = notional * raw_pct
    gross_pct = raw_pct * 100.0
    model = str(spec.get("cost_model") or "cfd").lower()
    spread_cost = 0.0
    comm_cost = 0.0
    financing_applied = 0.0
    if model == "cfd":
        spread = float(spec.get("spread_est") or 0)
        contract_size = float(spec.get("contract_size") or 1)
        spread_cost = spread * contract_size * abs(contracts)
        fin_yr = float(spec.get("financing_pct_yr") or 0) / 100.0
        financing = notional * fin_yr * max(0.0, float(nights_held)) / 365.0
        if str(direction or "").strip().upper() == "SHORT":
            # ESTIMATE: short receives financing minus broker markup (~50%).
            financing_applied = +financing * 0.5
        else:
            financing_applied = -financing
    elif model == "futures":
        comm_side = float(spec.get("commission_per_contract") or 0)
        comm_cost = comm_side * abs(contracts) * 2.0
        financing_applied = 0.0
    else:
        financing_applied = 0.0
    net = gross - spread_cost - comm_cost + financing_applied
    net_pct = (net / notional * 100.0) if notional > 0 else gross_pct
    return (
        round(net, 2),
        round(net_pct, 2),
        {
            "gross_pnl_dollars": round(gross, 2),
            "spread_cost": round(spread_cost, 2),
            "commission_cost": round(comm_cost, 2),
            "swap_amount": round(financing_applied, 2),
            "total_cost_dollars": round(spread_cost + comm_cost - financing_applied, 2),
            "nights_held": round(float(nights_held), 2),
            "cost_model": model,
            "spec_estimated": True,
        },
    )


def _fx_pair_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in SHADOW_FX_EXTRA_CURRENCIES:
        for b in SHADOW_FX_EXTRA_CURRENCIES:
            if a == b:
                continue
            for pair in (f"{a}{b}", f"{b}{a}"):
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
    return sorted(out)


def _probe_cache_key(symbol: str, start_d: date, end_d: date) -> str:
    return f"{symbol}|{start_d.isoformat()}|{end_d.isoformat()}"


def _load_probe_cache() -> dict[str, dict[str, Any]]:
    if not SHADOW_PROBE_CACHE_FILE.is_file():
        return {}
    try:
        raw = json.loads(SHADOW_PROBE_CACHE_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_probe_cache(cache: Mapping[str, Mapping[str, Any]]) -> None:
    try:
        SHADOW_PROBE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHADOW_PROBE_CACHE_FILE.write_text(
            json.dumps(dict(cache), separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError as e:
        log(f"[SHADOW INST] probe cache write error: {e}", level="warning")


def _probe_fx_ohlc_cached(
    pair: str,
    start_d: date,
    end_d: date,
    *,
    yf_download_fn: Any,
    hourly_ok: bool,
    cache: dict[str, dict[str, Any]],
) -> tuple[bool, str, bool]:
    key = _probe_cache_key(pair, start_d, end_d)
    hit = cache.get(key)
    if isinstance(hit, dict) and "ok" in hit:
        return bool(hit.get("ok")), str(hit.get("reason", "")), False
    ok, reason = _test_fx_ohlc(
        pair, start_d, end_d, yf_download_fn=yf_download_fn, hourly_ok=hourly_ok
    )
    cache[key] = {"ok": ok, "reason": reason}
    return ok, reason, True


def _count_bars(df: Any) -> int:
    try:
        return len(df) if df is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _test_fx_ohlc(
    pair: str,
    start_d: date,
    end_d: date,
    *,
    yf_download_fn: Any,
    hourly_ok: bool,
) -> tuple[bool, str]:
    yf_sym = f"{pair}=X"
    start_s = start_d.isoformat()
    end_s = (end_d + timedelta(days=1)).isoformat()
    try:
        df_d = yf_download_fn(yf_sym, start_s, end_s, "1d")
        if _count_bars(df_d) < MIN_SHADOW_DAILY_BARS:
            return False, f"daily bars {_count_bars(df_d)} < {MIN_SHADOW_DAILY_BARS}"
        df_w = yf_download_fn(yf_sym, start_s, end_s, "1wk")
        if _count_bars(df_w) < MIN_SHADOW_WEEKLY_BARS:
            return False, f"weekly bars {_count_bars(df_w)} < {MIN_SHADOW_WEEKLY_BARS}"
        if hourly_ok:
            sample_end = end_d
            sample_start = max(start_d, end_d - timedelta(days=60))
            df_h = yf_download_fn(
                yf_sym,
                sample_start.isoformat(),
                (sample_end + timedelta(days=1)).isoformat(),
                "1h",
            )
            if _count_bars(df_h) < 20:
                return False, "insufficient 1h sample in recent window"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _test_non_forex_ohlc(
    ticker: str,
    spec: Mapping[str, Any],
    start_d: date,
    end_d: date,
    *,
    yf_download_fn: Any,
    hourly_ok: bool,
) -> tuple[bool, str, bool]:
    yf_sym = str(spec.get("source") or ticker)
    provisional = ticker in ("ES", "NQ")
    start_s = start_d.isoformat()
    end_s = (end_d + timedelta(days=1)).isoformat()
    try:
        if _count_bars(yf_download_fn(yf_sym, start_s, end_s, "1d")) < MIN_SHADOW_DAILY_BARS:
            return False, "daily bars insufficient", provisional
        if _count_bars(yf_download_fn(yf_sym, start_s, end_s, "1wk")) < MIN_SHADOW_WEEKLY_BARS:
            return False, "weekly bars insufficient", provisional
        if hourly_ok:
            sample_start = max(start_d, end_d - timedelta(days=60))
            df_h = yf_download_fn(
                yf_sym,
                sample_start.isoformat(),
                (end_d + timedelta(days=1)).isoformat(),
                "1h",
            )
            if _count_bars(df_h) < 20:
                return False, "insufficient hourly sample", provisional
        if provisional:
            log(
                f"[SHADOW INST] {ticker} uses Yahoo continuous {yf_sym} — "
                "back-adjusted; roll gaps possible; results PROVISIONAL",
                level="warning",
            )
        return True, "ok", provisional
    except Exception as e:  # noqa: BLE001
        return False, str(e), provisional


def init_shadow_universe(
    *,
    start_date: str,
    end_date: str,
    blocked_pairs: frozenset[str],
    excluded_pairs: frozenset[str],
    real_chrono_tickers: frozenset[str],
    yf_download_fn: Any,
    hourly_earliest_fn: Any,
    enabled: bool,
) -> dict[str, Any]:
    global _shadow_universe
    _shadow_universe = {
        "tickers": set(),
        "class_by_ticker": {},
        "yf_by_ticker": {},
        "spec_by_ticker": {k: dict(v) for k, v in SHADOW_INSTRUMENTS.items()},
        "failed": {},
        "provisional_futures": set(),
        "real_tickers": set(real_chrono_tickers),
        "discovery": {},
    }
    if not enabled:
        return {"enabled": False}

    try:
        start_d = date.fromisoformat(str(start_date).strip()[:10])
        end_d = date.fromisoformat(str(end_date).strip()[:10])
    except ValueError:
        log("[SHADOW INST] invalid chrono dates — shadow universe empty", level="error")
        return {"enabled": False, "error": "invalid dates"}

    hourly_ok = end_d >= hourly_earliest_fn()
    tested = 0
    loaded_fx = 0
    loaded_part1 = 0
    loaded_extra_fx = 0
    failed: dict[str, str] = {}
    tickers: set[str] = set()
    class_by: dict[str, ShadowClass] = {}
    yf_by: dict[str, str] = {}
    real = set(real_chrono_tickers)
    probe_cache = _load_probe_cache()
    cache_dirty = False
    extra_fx_untested = 0

    part1 = set(blocked_pairs) | set(PART1_DATA_EXCLUDED_FX)
    for pair in sorted(part1):
        if pair in SHADOW_INSTRUMENTS:
            continue
        tested += 1
        ok, reason, probed = _probe_fx_ohlc_cached(
            pair,
            start_d,
            end_d,
            yf_download_fn=yf_download_fn,
            hourly_ok=hourly_ok,
            cache=probe_cache,
        )
        if probed:
            cache_dirty = True
            time.sleep(PROBE_BATCH_SLEEP_SEC)
        if ok:
            loaded_fx += 1
            loaded_part1 += 1
            tickers.add(pair)
            class_by[pair] = "blocked_fx"
            yf_by[pair] = f"{pair}=X"
        else:
            failed[pair] = reason
            if pair in PART1_DATA_EXCLUDED_FX:
                log(
                    f"[SHADOW INST] {pair} — OHLC unavailable ({reason}); skipping shadow",
                    level="warning",
                )

    extra_candidates = [
        p for p in _fx_pair_candidates() if p not in tickers and p not in real
    ]
    for idx, pair in enumerate(extra_candidates):
        if loaded_extra_fx >= SHADOW_MAX_EXTRA_FX:
            extra_fx_untested = len(extra_candidates) - idx
            break
        tested += 1
        ok, reason, probed = _probe_fx_ohlc_cached(
            pair,
            start_d,
            end_d,
            yf_download_fn=yf_download_fn,
            hourly_ok=hourly_ok,
            cache=probe_cache,
        )
        if probed:
            cache_dirty = True
            time.sleep(PROBE_BATCH_SLEEP_SEC)
        if ok:
            loaded_fx += 1
            loaded_extra_fx += 1
            tickers.add(pair)
            class_by[pair] = "extra_fx"
            yf_by[pair] = f"{pair}=X"
        else:
            failed[pair] = reason
    else:
        extra_fx_untested = 0

    if extra_fx_untested > 0:
        log(
            f"[SHADOW INST] extra_fx cap reached ({SHADOW_MAX_EXTRA_FX} loaded); "
            f"{extra_fx_untested} candidates left untested",
            level="info",
        )

    if cache_dirty:
        _save_probe_cache(probe_cache)

    loaded_nf = 0
    for ticker, spec in SHADOW_INSTRUMENTS.items():
        tested += 1
        ok, reason, provisional = _test_non_forex_ohlc(
            ticker,
            spec,
            start_d,
            end_d,
            yf_download_fn=yf_download_fn,
            hourly_ok=hourly_ok,
        )
        if ok:
            loaded_nf += 1
            tickers.add(ticker)
            sc_nf: ShadowClass = "index_future" if ticker in ("ES", "NQ") else "commodity"
            class_by[ticker] = sc_nf
            yf_by[ticker] = str(spec["source"])
            if provisional:
                _shadow_universe["provisional_futures"].add(ticker)
        else:
            failed[ticker] = reason
        time.sleep(PROBE_BATCH_SLEEP_SEC)

    hard_cap_applied = False
    total_loaded = len(tickers)
    if total_loaded > SHADOW_UNIVERSE_HARD_CAP:
        log(
            f"[SHADOW INST] ERROR: {total_loaded} instruments exceeds hard cap "
            f"{SHADOW_UNIVERSE_HARD_CAP}; disabling extra_fx",
            level="error",
        )
        for sym in list(tickers):
            if class_by.get(sym) == "extra_fx":
                tickers.discard(sym)
                class_by.pop(sym, None)
                yf_by.pop(sym, None)
        loaded_extra_fx = 0
        loaded_fx = loaded_part1
        hard_cap_applied = True
        total_loaded = len(tickers)

    _shadow_universe["tickers"] = tickers
    _shadow_universe["class_by_ticker"] = class_by
    _shadow_universe["yf_by_ticker"] = yf_by
    _shadow_universe["failed"] = failed

    real_scan_count = len(real)
    est_pct = round(100.0 * total_loaded / max(1, real_scan_count), 1)
    discovery = {
        "enabled": True,
        "pairs_tested": tested,
        "fx_loaded": loaded_fx,
        "part1_fx_loaded": loaded_part1,
        "extra_fx_loaded": loaded_extra_fx,
        "extra_fx_untested": extra_fx_untested,
        "extra_fx_max": SHADOW_MAX_EXTRA_FX,
        "universe_hard_cap": SHADOW_UNIVERSE_HARD_CAP,
        "hard_cap_applied": hard_cap_applied,
        "non_forex_loaded": loaded_nf,
        "total_loaded": total_loaded,
        "total_failed": len(failed),
        "failed_sample": dict(list(failed.items())[:20]),
        "loaded_tickers": sorted(tickers),
        "est_scan_increase_pct_per_day": est_pct,
    }
    _shadow_universe["discovery"] = discovery

    log(
        f"[SHADOW INST] Universe: {total_loaded} loaded, {len(failed)} failed, "
        f"~{est_pct}% vs {real_scan_count} real tickers/day",
        level="info",
    )
    if total_loaded > SHADOW_UNIVERSE_HARD_CAP - 5:
        log(
            f"[SHADOW INST] WARNING: {total_loaded} shadow instruments — materially longer run",
            level="warning",
        )
    return discovery


def restore_universe_from_saved(
    discovery: Mapping[str, Any],
    *,
    blocked_pairs: frozenset[str],
    real_chrono_tickers: frozenset[str],
) -> None:
    """Rehydrate shadow universe from a prior chrono job (skip OHLC re-probe)."""
    global _shadow_universe
    loaded = [str(t).strip().upper() for t in (discovery.get("loaded_tickers") or [])]
    tickers: set[str] = set()
    class_by: dict[str, ShadowClass] = {}
    yf_by: dict[str, str] = {}
    for sym in loaded:
        tickers.add(sym)
        if sym in blocked_pairs or sym in PART1_DATA_EXCLUDED_FX:
            class_by[sym] = "blocked_fx"
        elif sym in SHADOW_INSTRUMENTS:
            class_by[sym] = "index_future" if sym in ("ES", "NQ") else "commodity"
            yf_by[sym] = str(SHADOW_INSTRUMENTS[sym]["source"])
        else:
            class_by[sym] = "extra_fx"
            yf_by[sym] = f"{sym}=X"
    provisional = {t for t in loaded if t in ("ES", "NQ")}
    _shadow_universe = {
        "tickers": tickers,
        "class_by_ticker": class_by,
        "yf_by_ticker": yf_by,
        "spec_by_ticker": {k: dict(v) for k, v in SHADOW_INSTRUMENTS.items()},
        "failed": dict(discovery.get("failed_sample") or {}),
        "provisional_futures": provisional,
        "real_tickers": set(real_chrono_tickers),
        "discovery": dict(discovery),
    }
