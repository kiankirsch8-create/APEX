#!/usr/bin/env python3
"""
Parity harness: backtester Layer-2 decisions vs live decision path.

For each (pair, timeframe, date) in the test window, loads identical OHLC via
``continuous_backtester.get_ohlcv``, runs both decision paths, and diffs
enter / direction / confidence / entry / stop / TPs / risk_usd.

Exit code 0 iff zero divergences (or no comparable cells).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

import apex_v76_decision_logic as live
import continuous_backtester as cb

# Validated chrono excludes 4h at scan level; user asked 1w/1d/4h for parity coverage.
DEFAULT_TFS = ("1w", "1d", "4h")
DEFAULT_PAIRS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "USDCHF",
    "EURGBP",
)
COMPARE_KEYS = (
    "enter",
    "direction",
    "confidence",
    "entry",
    "stop",
    "tp1",
    "tp2",
    "tp3",
    "risk_usd",
)


@dataclass
class DecisionSnap:
    enter: bool
    direction: str
    confidence: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    risk_usd: float
    skip_reason: str = ""
    strategy_id: str = ""

    def roundtrip(self) -> dict[str, Any]:
        return asdict(self)


def _yf_sym(pair: str) -> str:
    p = pair.strip().upper()
    if len(p) == 6 and p.isalpha():
        return f"{p}=X"
    return p


def _zone(past: pd.DataFrame, price: float) -> tuple[float, str]:
    try:
        hi = float(past["High"].max())
        lo = float(past["Low"].min())
    except Exception:  # noqa: BLE001
        return 50.0, "EQUILIBRIUM"
    span = hi - lo
    if span <= 0:
        zp = 50.0
    else:
        zp = (float(price) - lo) / span * 100.0
    zp = max(0.0, min(100.0, float(zp)))
    if zp >= 66:
        return zp, "PREMIUM"
    if zp <= 33:
        return zp, "DISCOUNT"
    return zp, "EQUILIBRIUM"


def _build_ind(past: pd.DataFrame) -> tuple[dict[str, Any], float]:
    """Mirror continuous_backtester.run_one_backtest indicator block."""
    frame = past.copy()
    frame.ta.rsi(length=14, append=True)
    frame.ta.macd(append=True)
    frame.ta.ema(length=20, append=True)
    frame.ta.ema(length=50, append=True)
    frame.ta.ema(length=200, append=True)
    frame.ta.bbands(length=20, append=True)
    frame.ta.atr(length=14, append=True)
    frame.ta.adx(length=14, append=True)
    latest = frame.iloc[-1]
    price = round(float(latest["Close"]), 5)

    def safe(col: str, default: float = 0.0) -> float:
        try:
            if col not in frame.columns:
                return default
            v = frame[col].iloc[-1]
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                return default
            return round(float(v), 5)
        except Exception:  # noqa: BLE001
            return default

    bbl, bbm, bbu = cb._pick_bb_cols(frame)
    ind: dict[str, Any] = {
        "rsi": safe("RSI_14"),
        "macd_hist": safe("MACDh_12_26_9"),
        "ema20": safe("EMA_20", price),
        "ema50": safe("EMA_50", price),
        "ema200": safe("EMA_200", price),
        "atr": safe("ATRr_14"),
        "adx": safe("ADX_14"),
        "bb_upper": safe(bbu, price) if bbu else price,
        "bb_lower": safe(bbl, price) if bbl else price,
    }
    try:
        if "MACD_12_26_9" in frame.columns and "MACDs_12_26_9" in frame.columns:
            ind["macd_line"] = round(float(frame["MACD_12_26_9"].iloc[-1]), 5)
            ind["macd_signal"] = round(float(frame["MACDs_12_26_9"].iloc[-1]), 5)
        else:
            ind["macd_line"] = 0.0
            ind["macd_signal"] = 0.0
    except Exception:  # noqa: BLE001
        ind["macd_line"] = 0.0
        ind["macd_signal"] = 0.0
    try:
        ind["bb_mid"] = (
            round(float(frame[bbm].iloc[-1]), 5)
            if bbm and bbm in frame.columns
            else round((ind["bb_upper"] + ind["bb_lower"]) / 2, 5)
        )
    except Exception:  # noqa: BLE001
        ind["bb_mid"] = round((ind["bb_upper"] + ind["bb_lower"]) / 2, 5)
    try:
        ind["high_52w"] = round(float(frame["High"].max()), 5)
        ind["low_52w"] = round(float(frame["Low"].min()), 5)
    except Exception:  # noqa: BLE001
        ind["high_52w"] = ind["low_52w"] = price
    return ind, price


def _reset_shared_state() -> None:
    """Clear confluence / A+B state so both paths see the same empty slate."""
    for mod in (cb, live):
        for name in (
            "CHRONO_DAY_PREFILTER_SIDS",
            "CHRONO_SYMDIR_TFS",
        ):
            d = getattr(mod, name, None)
            if d is not None:
                d.clear()
        for name in ("CHRONO_JPY_STORM_SNAPSHOT", "CHRONO_JPY_PAIRS_SIGNALLED"):
            obj = getattr(mod, name, None)
            if isinstance(obj, dict):
                obj.clear()
            elif isinstance(obj, set):
                obj.clear()
        if hasattr(mod, "CHRONO_JPY_RISK_DAY"):
            setattr(mod, "CHRONO_JPY_RISK_DAY", 0.0)
    # A+B histories
    for attr in (
        "STRATEGY_PNL_HISTORY",
        "ST_MEDIUM_PNL_HISTORY",
        "_LIVE_STRAT_PNL_HISTORY",
        "_LIVE_ST_MEDIUM_PNL_HISTORY",
    ):
        for mod in (cb, live):
            h = getattr(mod, attr, None)
            if isinstance(h, dict):
                h.clear()


def _normalize_row(row: dict[str, Any] | None) -> DecisionSnap:
    if not isinstance(row, dict):
        return DecisionSnap(
            enter=False,
            direction="NONE",
            confidence="",
            entry=0.0,
            stop=0.0,
            tp1=0.0,
            tp2=0.0,
            tp3=0.0,
            risk_usd=0.0,
            skip_reason="null_result",
        )
    skipped = bool(row.get("skipped") or row.get("skip_trade") or row.get("_ab_skip"))
    direction = str(row.get("direction") or "NONE").strip().upper()
    if direction not in ("LONG", "SHORT"):
        skipped = True
    entry = float(row.get("entry_price", row.get("entry", 0)) or 0)
    stop = float(row.get("stop_loss", 0) or 0)
    risk = float(
        row.get("max_risk_dollars", row.get("_max_risk_dollars", row.get("risk_usd", 0)))
        or 0
    )
    return DecisionSnap(
        enter=not skipped,
        direction=direction if not skipped else "NONE",
        confidence=str(row.get("confidence") or "").strip().upper(),
        entry=round(entry, 5),
        stop=round(stop, 5),
        tp1=round(float(row.get("tp1", 0) or 0), 5),
        tp2=round(float(row.get("tp2", 0) or 0), 5),
        tp3=round(float(row.get("tp3", 0) or 0), 5),
        risk_usd=round(risk, 2),
        skip_reason=str(row.get("skip_reason") or "")[:200],
        strategy_id=str(row.get("strategy_id") or "").strip().upper(),
    )


def _near(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _diff_snaps(bt: DecisionSnap, lv: DecisionSnap) -> list[str]:
    diffs: list[str] = []
    if bt.enter != lv.enter:
        diffs.append(f"enter bt={bt.enter} live={lv.enter}")
    if bt.enter or lv.enter:
        if bt.direction != lv.direction:
            diffs.append(f"direction bt={bt.direction} live={lv.direction}")
        if bt.confidence != lv.confidence:
            diffs.append(f"confidence bt={bt.confidence} live={lv.confidence}")
        if not _near(bt.entry, lv.entry):
            diffs.append(f"entry bt={bt.entry} live={lv.entry}")
        if not _near(bt.stop, lv.stop):
            diffs.append(f"stop bt={bt.stop} live={lv.stop}")
        if not _near(bt.tp1, lv.tp1):
            diffs.append(f"tp1 bt={bt.tp1} live={lv.tp1}")
        if not _near(bt.tp2, lv.tp2):
            diffs.append(f"tp2 bt={bt.tp2} live={lv.tp2}")
        if not _near(bt.tp3, lv.tp3):
            diffs.append(f"tp3 bt={bt.tp3} live={lv.tp3}")
        if not _near(bt.risk_usd, lv.risk_usd, tol=1e-3):
            diffs.append(f"risk_usd bt={bt.risk_usd} live={lv.risk_usd}")
        if bt.strategy_id and lv.strategy_id and bt.strategy_id != lv.strategy_id:
            diffs.append(f"strategy_id bt={bt.strategy_id} live={lv.strategy_id}")
    return diffs


def _weekday_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += one
    return out


def _accumulate_prefilter(
    mod: Any,
    sym: str,
    tf_key: str,
    layer2: list[Any],
) -> None:
    sym_u = sym.upper()
    for row in layer2:
        if len(row) < 2:
            continue
        sid = str(row[0]).strip().upper()
        dr = str(row[1]).strip().upper()
        bag = getattr(mod, "CHRONO_DAY_PREFILTER_SIDS")
        tfs = getattr(mod, "CHRONO_SYMDIR_TFS")
        if dr == "BOTH":
            bag[(sym_u, "LONG")].add(sid)
            bag[(sym_u, "SHORT")].add(sid)
            tfs[(sym_u, "LONG")].add(tf_key)
            tfs[(sym_u, "SHORT")].add(tf_key)
        elif dr in ("LONG", "SHORT"):
            bag[(sym_u, dr)].add(sid)
            tfs[(sym_u, dr)].add(tf_key)
        jpy = getattr(mod, "JPY_STORM_PAIRS", set())
        if sym_u in jpy:
            getattr(mod, "CHRONO_JPY_PAIRS_SIGNALLED").add(sym_u)


def run_cell(
    *,
    pair: str,
    tf: str,
    analysis_date: str,
    balance: float,
) -> tuple[DecisionSnap, DecisionSnap, list[str], str]:
    """Return (bt_snap, live_snap, diffs, note)."""
    tf_key = tf.strip().lower()
    yf_t = _yf_sym(pair)
    past, future = cb.get_ohlcv(yf_t, tf_key, analysis_date, chrono_yfinance=True)
    if past is None or future is None or past.empty or future.empty:
        empty = _normalize_row(None)
        empty.skip_reason = "no_ohlc"
        return empty, empty, [], "no_ohlc"
    min_past = 30 if tf_key == "1w" else 50
    if len(past) < min_past or len(future) < 5:
        empty = _normalize_row(None)
        empty.skip_reason = "short_history"
        return empty, empty, [], "short_history"

    ind, price = _build_ind(past)
    if price <= 0:
        empty = _normalize_row(None)
        empty.skip_reason = "bad_price"
        return empty, empty, [], "bad_price"
    zone_pct, zone_label = _zone(past, price)
    is_exotic = pair.upper() in getattr(cb, "EXOTIC_REDUCE", set())

    # Shared prefilter (CB is source of truth; live wrapper must match).
    ok, layer2, reason = cb._v7_python_prefilter_bundle(
        pair.upper(),
        tf_key,
        float(price),
        ind,
        float(zone_pct),
        analysis_date=analysis_date,
        past=past,
    )
    layer2 = [
        q
        for q in layer2
        if str(q[0]).strip().upper() not in getattr(cb, "LAYER1_STRATEGY_IDS", set())
    ]
    if not ok or not layer2:
        empty = DecisionSnap(
            enter=False,
            direction="NONE",
            confidence="",
            entry=0.0,
            stop=0.0,
            tp1=0.0,
            tp2=0.0,
            tp3=0.0,
            risk_usd=0.0,
            skip_reason=f"prefilter:{reason}",
        )
        return empty, empty, [], "prefilter_empty"

    # Deterministic pick (same helper both sides after live delegates).
    picked = cb._layer2_tuple_for_deterministic_pick(
        pair.upper(), tf_key, float(zone_pct), layer2
    )
    if picked is None:
        empty = _normalize_row(None)
        empty.skip_reason = "no_pick"
        return empty, empty, [], "no_pick"

    _accumulate_prefilter(cb, pair, tf_key, layer2)
    _accumulate_prefilter(live, pair, tf_key, layer2)

    from intelligence_fetch_cached import cached_calendar_historical

    scan_d = date.fromisoformat(analysis_date[:10])
    hist_cal = cached_calendar_historical(pair.upper(), scan_d)

    regime_ctx = cb.cached_regime("parity", scan_d)

    bt_row = cb._python_forced_layer2_trade(
        sym=pair.upper(),
        timeframe=tf_key,
        analysis_date=analysis_date,
        tf_key=tf_key,
        price=float(price),
        zone_pct=float(zone_pct),
        zone_label=zone_label,
        is_exotic=bool(is_exotic),
        future=future,
        layer2=[picked],
        rsi_live=float(ind.get("rsi", 50) or 50),
        chrono_risk_mult=1.0,
        chrono_tp_mult=1.0,
        chrono_job=True,
        calendar_risk=hist_cal,
        regime_ctx=regime_ctx,
        chrono_balance=balance,
        chrono_compounding_base=balance,
        chrono_peak_capital=balance,
        chrono_day_pnl=0.0,
        period_mode="NEUTRAL",
        past=past,
        ind=ind,
    )

    live_row = live.python_layer2_live_plan(
        sym=pair.upper(),
        timeframe=tf_key,
        analysis_date=analysis_date,
        tf_key=tf_key,
        price=float(price),
        zone_pct=float(zone_pct),
        zone_label=zone_label,
        is_exotic=bool(is_exotic),
        layer2=[picked],
        rsi_live=float(ind.get("rsi", 50) or 50),
        chrono_risk_mult=1.0,
        chrono_tp_mult=1.0,
        regime_ctx=regime_ctx,
        chrono_balance=balance,
        chrono_day_pnl=0.0,
        period_mode="NEUTRAL",
        past=past,
        ind=ind,
        future=future,
        calendar_risk=hist_cal,
        chrono_compounding_base=balance,
        chrono_peak_capital=balance,
        log_fn=None,
    )

    bt_snap = _normalize_row(bt_row)
    lv_snap = _normalize_row(live_row)
    return bt_snap, lv_snap, _diff_snaps(bt_snap, lv_snap), "ok"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--start",
        default="2024-01-02",
        help="Window start YYYY-MM-DD (inclusive)",
    )
    p.add_argument(
        "--end",
        default="2024-01-12",
        help="Window end YYYY-MM-DD (inclusive)",
    )
    p.add_argument(
        "--pairs",
        default=",".join(DEFAULT_PAIRS),
        help="Comma-separated pairs",
    )
    p.add_argument(
        "--tfs",
        default=",".join(DEFAULT_TFS),
        help="Comma-separated timeframes",
    )
    p.add_argument("--balance", type=float, default=10_000.0)
    p.add_argument(
        "--out",
        default="",
        help="Optional JSON report path",
    )
    p.add_argument(
        "--max-cells",
        type=int,
        default=0,
        help="Optional cap on cells evaluated (0 = all)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = date.fromisoformat(args.start[:10])
    end = date.fromisoformat(args.end[:10])
    pairs = [x.strip().upper() for x in str(args.pairs).split(",") if x.strip()]
    tfs = [x.strip().lower() for x in str(args.tfs).split(",") if x.strip()]
    dates = _weekday_dates(start, end)

    cb._v72_load_strategy_status(log_startup=False)
    live.bind_backtester_parity_state()

    divergences: list[dict[str, Any]] = []
    compared = 0
    skipped_cells = 0
    cells = 0

    print(
        f"Parity window {start} → {end} | pairs={pairs} | tfs={tfs} | "
        f"balance={args.balance:.0f} | ACCOUNT_TYPE={cb.ACCOUNT_TYPE}"
    )

    for d in dates:
        ds = d.isoformat()
        for pair in pairs:
            for tf in tfs:
                cells += 1
                if args.max_cells and compared >= int(args.max_cells):
                    break
                _reset_shared_state()
                live.bind_backtester_parity_state()
                bt, lv, diffs, note = run_cell(
                    pair=pair,
                    tf=tf,
                    analysis_date=ds,
                    balance=float(args.balance),
                )
                if note != "ok":
                    skipped_cells += 1
                    continue
                compared += 1
                if diffs:
                    row = {
                        "pair": pair,
                        "timeframe": tf,
                        "date": ds,
                        "diffs": diffs,
                        "backtester": bt.roundtrip(),
                        "live": lv.roundtrip(),
                    }
                    divergences.append(row)
                    print(
                        f"DIVERGENCE {pair} {tf} {ds}: {'; '.join(diffs)}\n"
                        f"  bt={bt.roundtrip()}\n  live={lv.roundtrip()}"
                    )
            if args.max_cells and compared >= int(args.max_cells):
                break
        if args.max_cells and compared >= int(args.max_cells):
            break

    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "pairs": pairs,
        "timeframes": tfs,
        "cells_scheduled": cells,
        "cells_compared": compared,
        "cells_skipped_no_setup": skipped_cells,
        "divergence_count": len(divergences),
        "divergences": divergences,
        "main_account_type": cb.ACCOUNT_TYPE,
        "risk_by_confidence": dict(cb.RISK_BY_CONFIDENCE),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    out_path = Path(args.out) if args.out else ROOT / "results" / "parity_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"\nCompared {compared} cells ({skipped_cells} skipped). "
        f"Divergences: {len(divergences)}. Report: {out_path}"
    )
    if divergences:
        print("FAIL — list every divergence above / in the report.")
        return 1
    print("PASS — 0 divergences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
