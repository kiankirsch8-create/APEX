"""
Shadow instrument evaluation for chrono backtests — isolated from the real curve.

All contract specs in SHADOW_INSTRUMENTS are ESTIMATES. Correct in one place below.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Mapping

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

SHADOW_FX_CURRENCIES: tuple[str, ...] = (
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

_shadow_strat_by_class: dict[ShadowClass, dict[str, list[float]]] = {
    c: {} for c in _AB_CLASSES
}
_shadow_st_medium_by_class: dict[ShadowClass, list[tuple[float, bool]]] = {
    c: [] for c in _AB_CLASSES
}

_shadow_trades_buffer: list[dict[str, Any]] = []
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
    global _shadow_trades_buffer
    for c in _AB_CLASSES:
        _shadow_strat_by_class[c].clear()
        _shadow_st_medium_by_class[c].clear()
    _shadow_trades_buffer = []


def _strat_history_dict(shadow_class: ShadowClass) -> dict[str, dict[str, Any]]:
    raw = _shadow_strat_by_class.get(shadow_class, {})
    return {
        sid: {"n": len(vals), "last3": [float(x) for x in vals[-3:]]}
        for sid, vals in raw.items()
    }


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


def trades_buffer() -> list[dict[str, Any]]:
    return list(_shadow_trades_buffer)


def _record_ab_histories(row: Mapping[str, Any], shadow_class: ShadowClass) -> None:
    if str(row.get("outcome", "")).strip().upper() not in ("WIN", "LOSS"):
        return
    sid = str(row.get("strategy_id", "")).strip().upper()
    pnl = float(row.get("pnl_dollars", 0) or 0)
    if sid and sid != "SKIP":
        _shadow_strat_by_class.setdefault(shadow_class, {}).setdefault(sid, []).append(pnl)
    if (
        str(row.get("confidence", "")).strip().upper() == "MEDIUM"
        and str(row.get("macro_bias", "")).strip().upper() == "STRONG_TAILWIND"
    ):
        won = str(row.get("outcome", "")).strip().upper() == "WIN"
        _shadow_st_medium_by_class.setdefault(shadow_class, []).append((pnl, won))


def rebuild_histories(rows: list[dict[str, Any]]) -> None:
    reset_run_state()
    completed = [
        r
        for r in rows
        if isinstance(r, dict)
        and not r.get("skipped")
        and str(r.get("outcome", "")).strip().upper() in ("WIN", "LOSS")
    ]
    completed.sort(
        key=lambda r: (
            str(r.get("date", ""))[:10],
            str(r.get("ticker", "")).strip().upper(),
            str(r.get("timeframe", "")).strip().lower(),
            str(r.get("strategy_id", "")).strip().upper(),
        )
    )
    for r in completed:
        sc = str(r.get("shadow_class") or "blocked_fx")
        if sc not in _AB_CLASSES:
            sc = "blocked_fx"
        _record_ab_histories(r, sc)
        _shadow_trades_buffer.append(dict(r))


def load_trades_for_job(job_id: str) -> list[dict[str, Any]]:
    jid = str(job_id or "").strip()
    if not jid or not SHADOW_INSTRUMENTS_FILE.is_file():
        return []
    out: list[dict[str, Any]] = []
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
                    out.append(row)
    except OSError as e:
        log(f"[SHADOW INST] load error: {e}", level="warning")
    return out


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
    _shadow_trades_buffer.append(out)


def ab_histories_for_active() -> tuple[dict[str, dict[str, Any]] | None, list[tuple[float, bool]] | None]:
    sc = active_shadow_class()
    if not sc:
        return None, None
    return _strat_history_dict(sc), list(_shadow_st_medium_by_class.get(sc, []))


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


def compute_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    executed = [
        t
        for t in trades
        if isinstance(t, dict)
        and not t.get("skipped")
        and str(t.get("outcome", "")).strip().upper() in ("WIN", "LOSS")
    ]
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for t in executed:
        tkr = str(t.get("shadow_instrument") or t.get("ticker", "")).strip().upper()
        if tkr:
            by_ticker.setdefault(tkr, []).append(t)
    summary: dict[str, Any] = {
        "by_ticker": {},
        "total_trades": len(executed),
        "primary_metric": "R-multiple (pnl / max_risk_dollars)",
    }
    for tkr, rows in sorted(by_ticker.items()):
        rows.sort(key=lambda r: str(r.get("date", ""))[:10])
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
    financing = 0.0
    if model == "cfd":
        spread = float(spec.get("spread_est") or 0)
        contract_size = float(spec.get("contract_size") or 1)
        spread_cost = spread * contract_size * abs(contracts)
        fin_yr = float(spec.get("financing_pct_yr") or 0) / 100.0
        financing = notional * fin_yr * max(0.0, float(nights_held)) / 365.0
        if str(direction or "").strip().upper() == "SHORT":
            financing = -financing
    elif model == "futures":
        comm_side = float(spec.get("commission_per_contract") or 0)
        comm_cost = comm_side * abs(contracts) * 2.0
    net = gross - spread_cost - comm_cost + financing
    net_pct = (net / notional * 100.0) if notional > 0 else gross_pct
    return (
        round(net, 2),
        round(net_pct, 2),
        {
            "gross_pnl_dollars": round(gross, 2),
            "spread_cost": round(spread_cost, 2),
            "commission_cost": round(comm_cost, 2),
            "swap_amount": round(financing, 2),
            "total_cost_dollars": round(spread_cost + comm_cost - financing, 2),
            "nights_held": round(float(nights_held), 2),
            "cost_model": model,
            "spec_estimated": True,
        },
    )


def _fx_pair_candidates() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in SHADOW_FX_CURRENCIES:
        for b in SHADOW_FX_CURRENCIES:
            if a == b:
                continue
            for pair in (f"{a}{b}", f"{b}{a}"):
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
    return out


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
    failed: dict[str, str] = {}
    tickers: set[str] = set()
    class_by: dict[str, ShadowClass] = {}
    yf_by: dict[str, str] = {}
    real = set(real_chrono_tickers)

    part1 = set(blocked_pairs) | set(PART1_DATA_EXCLUDED_FX)
    for pair in sorted(part1):
        if pair in SHADOW_INSTRUMENTS:
            continue
        tested += 1
        ok, reason = _test_fx_ohlc(pair, start_d, end_d, yf_download_fn=yf_download_fn, hourly_ok=hourly_ok)
        if ok:
            loaded_fx += 1
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

    for pair in _fx_pair_candidates():
        if pair in tickers or pair in real:
            continue
        tested += 1
        ok, reason = _test_fx_ohlc(pair, start_d, end_d, yf_download_fn=yf_download_fn, hourly_ok=hourly_ok)
        if ok:
            loaded_fx += 1
            tickers.add(pair)
            class_by[pair] = "extra_fx"
            yf_by[pair] = f"{pair}=X"
        else:
            failed[pair] = reason

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

    _shadow_universe["tickers"] = tickers
    _shadow_universe["class_by_ticker"] = class_by
    _shadow_universe["yf_by_ticker"] = yf_by
    _shadow_universe["failed"] = failed

    total_loaded = len(tickers)
    real_scan_count = len(real)
    est_pct = round(100.0 * total_loaded / max(1, real_scan_count), 1)
    discovery = {
        "enabled": True,
        "pairs_tested": tested,
        "fx_loaded": loaded_fx,
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
    if total_loaded > 40:
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
