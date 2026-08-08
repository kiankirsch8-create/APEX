"""
Shadow-only strategy infrastructure.

Write-only: strategies registered here never affect live selection, sizing,
position books, capital, or dashboards. Failures log one warning and continue.

Live trading modules must NOT import this file.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from utils import DATA_DIR, log

# ── Output ───────────────────────────────────────────────────────────────────
SHADOW_TRADES_FILE = DATA_DIR / "shadow_strategy_trades.jsonl"

# ── Registry ─────────────────────────────────────────────────────────────────
SHADOW_STRATEGIES: dict[str, Callable[["ShadowStrategyContext"], dict[str, Any] | None]] = {}

SignalFn = Callable[["ShadowStrategyContext"], dict[str, Any] | None]


def register_shadow_strategy(strategy_id: str) -> Callable[[SignalFn], SignalFn]:
    """Decorator: register a shadow strategy function by ID."""

    def _wrap(fn: SignalFn) -> SignalFn:
        sid = str(strategy_id or "").strip().upper()
        if not sid:
            raise ValueError("strategy_id required")
        SHADOW_STRATEGIES[sid] = fn
        fn._shadow_strategy_id = sid  # type: ignore[attr-defined]
        return fn

    return _wrap


# ── Cross-pair OHLC day cache (PART E) — non-popping, past bars only ─────────
_shadow_ohlc_lock = threading.Lock()
# (ticker, timeframe, YYYY-MM-DD) → past DataFrame (no future bars)
_SHADOW_OHLC_DAY: dict[tuple[str, str, str], pd.DataFrame] = {}


def remember_shadow_ohlc(
    ticker: str,
    timeframe: str,
    analysis_date: str,
    past: pd.DataFrame | None,
) -> None:
    """Store past OHLC for cross-pair access. Never stores future bars."""
    if past is None or getattr(past, "empty", True):
        return
    tku = (ticker or "").strip().upper()
    tf = (timeframe or "").strip().lower()
    ds = str(analysis_date or "").strip()[:10]
    if not tku or not tf or not ds:
        return
    try:
        # Defensive copy of past only.
        snap = past.copy(deep=False)
    except Exception:  # noqa: BLE001
        return
    with _shadow_ohlc_lock:
        _SHADOW_OHLC_DAY[(tku, tf, ds)] = snap


def clear_shadow_ohlc_day(analysis_date: str | None = None) -> None:
    """Drop cached past frames (optionally for one calendar day)."""
    with _shadow_ohlc_lock:
        if analysis_date is None:
            _SHADOW_OHLC_DAY.clear()
            return
        ds = str(analysis_date).strip()[:10]
        dead = [k for k in _SHADOW_OHLC_DAY if k[2] != ds]
        # Keep only current day to bound memory; drop older.
        for k in list(_SHADOW_OHLC_DAY):
            if k[2] != ds:
                _SHADOW_OHLC_DAY.pop(k, None)
        _ = dead


# ── JSONL append (O(1), same pattern as append_result) ───────────────────────
_shadow_trades_lock = threading.Lock()
_shadow_seen_keys: set[str] | None = None
_shadow_trades_count: int = 0


def _shadow_dedup_key(
    strategy_id: str,
    ticker: str,
    timeframe: str,
    entry_date: str,
) -> str:
    return (
        f"{str(strategy_id).strip().upper()}_"
        f"{str(ticker).strip().upper()}_"
        f"{str(timeframe).strip().lower()}_"
        f"{str(entry_date).strip()[:10]}"
    )


def _ensure_shadow_trades_index_locked() -> None:
    global _shadow_seen_keys, _shadow_trades_count
    if _shadow_seen_keys is not None:
        return
    keys: set[str] = set()
    count = 0
    if SHADOW_TRADES_FILE.exists():
        with open(SHADOW_TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                count += 1
                k = _shadow_dedup_key(
                    str(row.get("strategy_id", "")),
                    str(row.get("ticker", "")),
                    str(row.get("timeframe", "")),
                    str(row.get("date", row.get("entry_date", ""))),
                )
                if k.strip("_"):
                    keys.add(k)
    _shadow_seen_keys = keys
    _shadow_trades_count = count


def append_shadow_trade(row: dict[str, Any]) -> int:
    """Append one shadow trade JSONL line. Never rewrites the file. O(1)."""
    global _shadow_seen_keys, _shadow_trades_count
    try:
        with _shadow_trades_lock:
            _ensure_shadow_trades_index_locked()
            assert _shadow_seen_keys is not None
            key = _shadow_dedup_key(
                str(row.get("strategy_id", "")),
                str(row.get("ticker", "")),
                str(row.get("timeframe", "")),
                str(row.get("date", "")),
            )
            if key in _shadow_seen_keys:
                return _shadow_trades_count
            SHADOW_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, default=str, separators=(",", ":")) + "\n"
            with open(SHADOW_TRADES_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            _shadow_seen_keys.add(key)
            _shadow_trades_count += 1
            return _shadow_trades_count
    except Exception as e:  # noqa: BLE001
        log(f"[SHADOW STRAT] append_shadow_trade error: {e}", level="warning")
        return 0


# ── Read-only context (PART C) ───────────────────────────────────────────────
@dataclass(frozen=True)
class ShadowStrategyContext:
    """
    Read-only view for shadow strategies.
    OHLC accessors enforce no-future: only bars ≤ scan day are exposed.
    """

    ticker: str
    timeframe: str
    analysis_date: str
    _past: pd.DataFrame = field(repr=False)
    _ind: Mapping[str, Any] = field(repr=False, default_factory=dict)
    _regime: Mapping[str, Any] = field(repr=False, default_factory=dict)
    _universe: tuple[str, ...] = field(default_factory=tuple)

    # ── Calendar ─────────────────────────────────────────────────────────
    @property
    def session_date(self) -> str:
        return str(self.analysis_date).strip()[:10]

    @property
    def day_of_week(self) -> int:
        """Monday=0 … Sunday=6."""
        try:
            return date.fromisoformat(self.session_date).weekday()
        except ValueError:
            return -1

    @property
    def month(self) -> int:
        try:
            return date.fromisoformat(self.session_date).month
        except ValueError:
            return -1

    # ── OHLC (no future) ─────────────────────────────────────────────────
    def ohlc(self) -> pd.DataFrame:
        """Past OHLC only (copy). Never includes forward bars."""
        if self._past is None or getattr(self._past, "empty", True):
            return pd.DataFrame()
        return self._past.copy(deep=False)

    def closes(self) -> list[float]:
        df = self.ohlc()
        if df.empty or "Close" not in df.columns:
            return []
        return [float(x) for x in pd.to_numeric(df["Close"], errors="coerce").dropna().tolist()]

    def last_close(self) -> float | None:
        c = self.closes()
        return c[-1] if c else None

    # ── Indicators ───────────────────────────────────────────────────────
    @property
    def atr(self) -> float | None:
        try:
            v = self._ind.get("atr")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def indicators(self) -> dict[str, Any]:
        return dict(self._ind)

    # ── Regime ───────────────────────────────────────────────────────────
    @property
    def regime_state(self) -> str:
        return str(self._regime.get("state") or "NEUTRAL")

    @property
    def regime_raw_score(self) -> float | None:
        try:
            v = self._regime.get("raw_score")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def regime_er(self) -> float | None:
        try:
            v = self._regime.get("er")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def regime_confidence(self) -> float | None:
        try:
            v = self._regime.get("confidence")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def regime_days_in_state(self) -> int:
        try:
            return int(self._regime.get("days_in_state", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def atr_at_regime_entry(self) -> float | None:
        try:
            v = self._regime.get("atr_at_regime_entry")
            if v is None:
                from regime_engine import get_atr_at_regime_entry

                return get_atr_at_regime_entry(self.ticker)
            return float(v)
        except (TypeError, ValueError):
            return None

    def score_history(self) -> list[float | None]:
        from regime_engine import get_score_history

        return get_score_history(self.ticker)

    def er_history(self) -> list[float | None]:
        from regime_engine import get_er_history

        return get_er_history(self.ticker)

    def confidence_history(self) -> list[float | None]:
        from regime_engine import get_confidence_history

        return get_confidence_history(self.ticker)

    # ── Cross-pair (PART E) ──────────────────────────────────────────────
    def get_pair_history(self, ticker: str) -> pd.DataFrame | None:
        """
        Past OHLC for another ticker on the same scan date/timeframe.
        Returns None if not available. Never returns future bars.
        """
        tku = (ticker or "").strip().upper()
        tf = self.timeframe.strip().lower()
        ds = self.session_date
        with _shadow_ohlc_lock:
            hit = _SHADOW_OHLC_DAY.get((tku, tf, ds))
        if hit is None or getattr(hit, "empty", True):
            return None
        return hit.copy(deep=False)

    def get_universe_returns(self, lookback_days: int = 1) -> dict[str, float]:
        """
        Same-day-aligned pct return over ``lookback_days`` closes for every
        pair present in the shadow OHLC day cache for this date/timeframe.
        """
        n = max(1, int(lookback_days))
        tf = self.timeframe.strip().lower()
        ds = self.session_date
        out: dict[str, float] = {}
        with _shadow_ohlc_lock:
            items = [(k, v) for k, v in _SHADOW_OHLC_DAY.items() if k[1] == tf and k[2] == ds]
        for (tku, _tf, _ds), df in items:
            try:
                closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
                if len(closes) < n + 1:
                    continue
                c0 = float(closes.iloc[-1 - n])
                c1 = float(closes.iloc[-1])
                if c0 == 0:
                    continue
                out[tku] = (c1 - c0) / c0
            except Exception:  # noqa: BLE001
                continue
        return out


def build_shadow_context(
    *,
    ticker: str,
    timeframe: str,
    analysis_date: str,
    past: pd.DataFrame | None,
    ind: Mapping[str, Any] | None = None,
    regime: Mapping[str, Any] | None = None,
    universe: list[str] | tuple[str, ...] | None = None,
) -> ShadowStrategyContext:
    """Build a context from scan locals. Remembers current past into day cache."""
    tku = (ticker or "").strip().upper()
    tf = (timeframe or "").strip().lower()
    ds = str(analysis_date or "").strip()[:10]
    # Deep-copy so strategy code cannot mutate the real scan's DataFrame.
    if past is None or getattr(past, "empty", True):
        past_use = pd.DataFrame()
    else:
        try:
            past_use = past.copy(deep=True)
        except Exception:  # noqa: BLE001
            past_use = past.copy(deep=False)
    remember_shadow_ohlc(tku, tf, ds, past_use if not past_use.empty else None)
    return ShadowStrategyContext(
        ticker=tku,
        timeframe=tf,
        analysis_date=ds,
        _past=past_use,
        _ind=dict(ind or {}),
        _regime=dict(regime or {}),
        _universe=tuple(universe or ()),
    )


# ── Shadow trade simulation (PART A + B) ─────────────────────────────────────
def _flat_sizing(
    entry: float,
    stop: float,
    *,
    starting_capital: float,
    risk_pct: float,
    leverage: int,
) -> tuple[float, float, float]:
    """Return (position_size, leveraged_exposure, max_risk_dollars) at flat 1.0x."""
    risk = abs(float(entry) - float(stop))
    max_risk = max(0.0, float(starting_capital) * float(risk_pct))
    if risk <= 0 or entry <= 0:
        return 0.0, 0.0, round(max_risk, 2)
    ps = max_risk / risk
    exposure = ps * float(entry)
    return round(ps, 2), round(exposure, 2), round(max_risk, 2)


def _custom_exit_hit(
    custom_exit: Mapping[str, Any] | None,
    *,
    direction: str,
    candle: Mapping[str, Any],
    session_i: int,
    past_plus: pd.DataFrame,
) -> tuple[bool, float | None, str]:
    """
    Check custom exit on this candle. Returns (hit, exit_price, reason).
    Extension point: type == \"condition\" reserved (not implemented).
    """
    if not isinstance(custom_exit, Mapping):
        return False, None, ""
    ctype = str(custom_exit.get("type") or "").strip().lower()
    d = str(direction).strip().upper()
    try:
        high = float(candle.get("High", 0) or 0)
        low = float(candle.get("Low", 0) or 0)
        close = float(candle.get("Close", 0) or 0)
    except (TypeError, ValueError):
        return False, None, ""

    if ctype == "time_exit":
        try:
            max_sessions = int(custom_exit.get("max_sessions", 0) or 0)
        except (TypeError, ValueError):
            max_sessions = 0
        if max_sessions > 0 and session_i >= max_sessions:
            return True, close, f"CUSTOM_TIME_EXIT_{max_sessions}"
        return False, None, ""

    if ctype == "price_target":
        try:
            level = float(custom_exit.get("level"))
        except (TypeError, ValueError):
            return False, None, ""
        if d == "LONG" and high >= level:
            return True, level, "CUSTOM_PRICE_TARGET"
        if d == "SHORT" and low <= level:
            return True, level, "CUSTOM_PRICE_TARGET"
        return False, None, ""

    if ctype == "indicator_touch":
        ind_name = str(custom_exit.get("indicator") or "").strip().lower()
        try:
            period = int(custom_exit.get("period", 20) or 20)
        except (TypeError, ValueError):
            period = 20
        if ind_name in ("sma", "sma_close", "sma_close_price"):
            try:
                closes = pd.to_numeric(past_plus["Close"], errors="coerce").dropna()
                if len(closes) < period:
                    return False, None, ""
                sma = float(closes.iloc[-period:].mean())
            except Exception:  # noqa: BLE001
                return False, None, ""
            # Touch: price range crosses SMA
            if low <= sma <= high:
                return True, sma, f"CUSTOM_INDICATOR_TOUCH_SMA_{period}"
        return False, None, ""

    if ctype == "condition":
        # Reserved for later (e.g. spread z-score). Clean extension point.
        return False, None, ""

    return False, None, ""


def _simulate_shadow_trade(
    signal: Mapping[str, Any],
    *,
    forward_df: pd.DataFrame,
    past_df: pd.DataFrame,
    atr: float | None,
) -> dict[str, Any] | None:
    """
    Simulate a shadow signal with the real exit engine (+ optional custom_exit).
    Lazy-imports continuous_backtester to avoid import cycles / live coupling.
    """
    from continuous_backtester import (
        LEVERAGE,
        RISK_BY_CONFIDENCE,
        STARTING_CAPITAL,
        _apply_realistic_costs,
        _evaluate_forward_with_trend_continuation,
    )

    direction = str(signal.get("direction") or "").strip().upper()
    if direction not in ("LONG", "SHORT"):
        return None
    try:
        entry = float(signal["entry_price"])
        stop = float(signal["stop_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if entry <= 0 or abs(entry - stop) <= 0:
        return None

    sid = str(signal.get("strategy_id") or "").strip().upper()
    tf = str(signal.get("timeframe") or "").strip().lower()
    custom_exit = signal.get("custom_exit")
    if custom_exit is not None and not isinstance(custom_exit, Mapping):
        custom_exit = None

    ps, exposure, max_risk = _flat_sizing(
        entry,
        stop,
        starting_capital=STARTING_CAPITAL,
        risk_pct=float(RISK_BY_CONFIDENCE.get("MEDIUM", 0.01)),
        leverage=LEVERAGE,
    )
    if ps <= 0:
        return None

    # Default ladder levels (engine recomputes from risk × regime multiples).
    risk = abs(entry - stop)
    sign = 1.0 if direction == "LONG" else -1.0
    tp1 = entry + sign * risk * 2.0
    tp2 = entry + sign * risk * 4.0
    tp3 = entry + sign * risk * 7.0

    if custom_exit is None:
        exit_data = _evaluate_forward_with_trend_continuation(
            direction,
            entry,
            stop,
            tp1,
            tp2,
            tp3,
            forward_df,
            sid,
            position_size=ps,
            leverage=LEVERAGE,
            timeframe=tf,
            macro_bias="NEUTRAL",
            macro_bias_adjusted="NEUTRAL",
            trail_regime="TRENDING",
            trend_strength=0.0,
            rate_differential=0.0,
            atr=float(atr or 0) or risk,
            buffer_stop_price=stop,
            ticker=str(signal.get("ticker") or ""),
            period_mode="NEUTRAL",
        )
    else:
        exit_data = _simulate_with_custom_exit(
            direction=direction,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            forward_df=forward_df,
            past_df=past_df,
            position_size=ps,
            timeframe=tf,
            atr=float(atr or 0) or risk,
            ticker=str(signal.get("ticker") or ""),
            strategy_id=sid,
            custom_exit=custom_exit,
        )

    if not exit_data or exit_data.get("outcome") in ("NO_DATA", "INVALID"):
        return None

    raw_pct = float(exit_data.get("pnl_pct", 0) or 0)
    candles = int(exit_data.get("candles_to_exit", 0) or 0)
    pnl_d, pnl_pct, gross_pct, cost_fields = _apply_realistic_costs(
        ticker=str(signal.get("ticker") or ""),
        direction=direction,
        timeframe=tf,
        position_size=ps,
        entry=entry,
        leveraged_exposure=exposure,
        raw_pct=raw_pct,
        candles_to_exit=candles,
    )
    outcome = str(exit_data.get("outcome") or ("WIN" if pnl_d > 0 else "LOSS"))
    return {
        "direction": direction,
        "entry_price": round(entry, 5),
        "stop_loss": round(stop, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "tp3": round(tp3, 5),
        "exit_price": exit_data.get("exit_price"),
        "exit_reason": exit_data.get("exit_reason"),
        "outcome": outcome,
        "pnl_dollars": pnl_d,
        "pnl_pct": pnl_pct,
        "gross_pnl_pct": gross_pct,
        **cost_fields,
        "trailing_activated": bool(exit_data.get("trailing_activated")),
        "hit_tp1": bool(exit_data.get("hit_tp1")),
        "hit_tp2": bool(exit_data.get("hit_tp2")),
        "hit_tp3": bool(exit_data.get("hit_tp3")),
        "hit_stop": bool(exit_data.get("hit_stop")),
        "candles_to_exit": candles,
        "final_stop": exit_data.get("final_stop"),
        "position_size": ps,
        "leveraged_exposure": exposure,
        "max_risk_dollars": max_risk,
        "entry_atr": atr,
        "custom_exit": dict(custom_exit) if isinstance(custom_exit, Mapping) else None,
    }


def _simulate_with_custom_exit(
    *,
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    tp3: float,
    forward_df: pd.DataFrame,
    past_df: pd.DataFrame,
    position_size: float,
    timeframe: str,
    atr: float,
    ticker: str,
    strategy_id: str,
    custom_exit: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Candle walk: normal stop/ladder/trail vs custom_exit — first trigger wins.
    Reuses the real forward engine per-prefix would be O(n²); instead walk once
    mirroring evaluate_forward_candles TRENDING path + custom checks.
    """
    from continuous_backtester import _evaluate_forward_with_trend_continuation

    # Run the standard engine first to get the "normal" exit bar index / result.
    normal = _evaluate_forward_with_trend_continuation(
        direction,
        entry,
        stop,
        tp1,
        tp2,
        tp3,
        forward_df,
        strategy_id,
        position_size=position_size,
        timeframe=timeframe,
        macro_bias="NEUTRAL",
        macro_bias_adjusted="NEUTRAL",
        trail_regime="TRENDING",
        atr=atr,
        buffer_stop_price=stop,
        ticker=ticker,
        period_mode="NEUTRAL",
    )
    normal_bars = int(normal.get("candles_to_exit", 0) or 0)

    # Scan forward for custom exit; if it fires earlier than normal, win.
    rows = list(forward_df.iterrows())
    past_acc = past_df.copy(deep=False) if past_df is not None and not past_df.empty else pd.DataFrame()
    for i, (_, candle) in enumerate(rows, start=1):
        try:
            row_df = pd.DataFrame([candle])
            past_acc = pd.concat([past_acc, row_df], ignore_index=True) if not past_acc.empty else row_df
        except Exception:  # noqa: BLE001
            past_acc = past_acc
        hit, px, reason = _custom_exit_hit(
            custom_exit,
            direction=direction,
            candle=candle if isinstance(candle, Mapping) else dict(candle),
            session_i=i,
            past_plus=past_acc,
        )
        if hit and px is not None:
            # Custom wins if it triggers on/before the normal exit bar.
            if normal_bars <= 0 or i <= normal_bars:
                d = direction
                move = (px - entry) if d == "LONG" else (entry - px)
                denom = position_size * entry if position_size > 0 and entry > 0 else 0.0
                pnl_pct = (position_size * move / denom) if denom > 0 else 0.0
                # Approximate: full position exit at custom price (no partials).
                return {
                    "outcome": "WIN" if pnl_pct > 0 else "LOSS",
                    "exit_price": round(float(px), 5),
                    "exit_reason": reason,
                    "pnl_pct": round(pnl_pct, 6),
                    "hit_tp1": False,
                    "hit_tp2": False,
                    "hit_tp3": False,
                    "hit_stop": False,
                    "candles_to_exit": i,
                    "trailing_activated": False,
                    "final_stop": stop,
                }
            break
    return normal


# ── Orchestrator ─────────────────────────────────────────────────────────────
def evaluate_shadow_strategies(
    context: ShadowStrategyContext,
    *,
    forward_df: pd.DataFrame | None,
    analysis_date: str,
) -> int:
    """
    Run all registered shadow strategies for one scan cell.
    Returns number of shadow trades appended. Never raises to caller.
    """
    n_out = 0
    if forward_df is None or getattr(forward_df, "empty", True):
        return 0
    # Snapshot registry items so strategies can't mutate mid-loop.
    items = list(SHADOW_STRATEGIES.items())
    for sid, fn in items:
        try:
            signal = fn(context)
            if signal is None:
                continue
            if not isinstance(signal, dict):
                continue
            sig = dict(signal)
            sig.setdefault("strategy_id", sid)
            sig.setdefault("ticker", context.ticker)
            sig.setdefault("timeframe", context.timeframe)
            trade = _simulate_shadow_trade(
                sig,
                forward_df=forward_df,
                past_df=context.ohlc(),
                atr=context.atr,
            )
            if trade is None:
                continue
            # Regime / shadow fields (write-only mirrors of existing shadow_*).
            entry_ds = str(analysis_date).strip()[:10]
            row = {
                "date": entry_ds,
                "entry_date": entry_ds,
                "ticker": context.ticker,
                "timeframe": context.timeframe,
                "strategy_id": sid,
                "strategy_name": sid,
                "skipped": False,
                "shadow_trade": True,
                "shadow_state": context.regime_state,
                "shadow_raw_score": context.regime_raw_score,
                "shadow_er": context.regime_er,
                "shadow_confidence": context.regime_confidence,
                "shadow_days_in_state": context.regime_days_in_state,
                "shadow_bias": None,  # filled below from state × direction
                "shadow_mult_flat": 1.0,
                "atr_at_regime_entry": context.atr_at_regime_entry,
                **trade,
            }
            # Map pair state × direction → bias labels (same as backtester helper).
            try:
                from continuous_backtester import _shadow_bias_from_state

                row["shadow_bias"] = _shadow_bias_from_state(
                    context.regime_state, str(trade.get("direction")),
                )
            except Exception:  # noqa: BLE001
                row["shadow_bias"] = None
            append_shadow_trade(row)
            n_out += 1
        except Exception as e:  # noqa: BLE001
            log(f"[SHADOW STRAT] {sid}: {e}", level="warning")
            continue
    return n_out


def run_shadow_strategies_safe(
    *,
    ticker: str,
    timeframe: str,
    analysis_date: str,
    past: pd.DataFrame | None,
    future: pd.DataFrame | None,
    ind: Mapping[str, Any] | None,
    regime: Mapping[str, Any] | None = None,
    universe: list[str] | None = None,
) -> None:
    """
    Entry point for the backtester hook. Entire body try/except'd —
    must never interrupt or alter the real scan.
    """
    try:
        if past is None or future is None or getattr(past, "empty", True):
            return
        if getattr(future, "empty", True):
            return
        ctx = build_shadow_context(
            ticker=ticker,
            timeframe=timeframe,
            analysis_date=analysis_date,
            past=past,
            ind=ind,
            regime=regime,
            universe=universe,
        )
        evaluate_shadow_strategies(ctx, forward_df=future, analysis_date=analysis_date)
    except Exception as e:  # noqa: BLE001
        log(
            f"[SHADOW STRAT] evaluate_shadow_strategies failed "
            f"{ticker} {timeframe} {analysis_date}: {e}",
            level="warning",
        )


# ── PART F — validation dummy (remove in a later prompt) ─────────────────────
@register_shadow_strategy("PDUMMY01_VALIDATION")
def pdummy01_validation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Trivial long on EURUSD 1d every Monday open, stop = entry − 1×ATR.
    Proves signal → sim → JSONL end-to-end. No custom exit.
    """
    if ctx.ticker != "EURUSD":
        return None
    if ctx.timeframe != "1d":
        return None
    if ctx.day_of_week != 0:  # Monday
        return None
    df = ctx.ohlc()
    if df.empty or "Open" not in df.columns:
        return None
    try:
        entry = float(pd.to_numeric(df["Open"], errors="coerce").iloc[-1])
    except Exception:  # noqa: BLE001
        return None
    atr = ctx.atr
    if entry <= 0 or atr is None or atr <= 0:
        return None
    return {
        "direction": "LONG",
        "entry_price": float(entry),
        "stop_price": float(entry) - float(atr),
        "timeframe": "1d",
        "strategy_id": "PDUMMY01_VALIDATION",
    }
