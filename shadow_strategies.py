"""
Shadow-only strategy infrastructure.

Write-only: strategies registered here never affect live selection, sizing,
position books, capital, or dashboards. Failures log one warning and continue.

Live trading modules must NOT import this file.
"""
from __future__ import annotations

import calendar
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

    @property
    def rate_diff(self) -> float:
        """Pair rate differential from the shadow regime day entry (0.0 if missing)."""
        try:
            v = self._regime.get("rate_diff")
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @property
    def trend_strength(self) -> float:
        try:
            v = self._ind.get("trend_strength")
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def score_history(self) -> list[float | None]:
        from regime_engine import get_score_history

        return get_score_history(self.ticker)

    def er_history(self) -> list[float | None]:
        from regime_engine import get_er_history

        return get_er_history(self.ticker)

    def confidence_history(self) -> list[float | None]:
        from regime_engine import get_confidence_history

        return get_confidence_history(self.ticker)

    def rate_component_history(self) -> list[float | None]:
        from regime_engine import get_rate_component_history

        return get_rate_component_history(self.ticker)

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


def _resolve_shadow_trail_inputs(
    *,
    regime_state: str,
    direction: str,
    timeframe: str,
    trend_strength: float,
    rate_diff: float,
) -> tuple[str, str, float, float]:
    """
    Mirror the real path: bias from shadow regime × direction, then
    ``_resolve_trailing_regime``. Returns (bias, trail_regime, trend_strength, rate_diff).
    """
    from continuous_backtester import _resolve_trailing_regime, _shadow_bias_from_state

    bias_raw = _shadow_bias_from_state(regime_state, direction)
    bias = str(bias_raw or "NEUTRAL")
    try:
        ts = float(trend_strength)
    except (TypeError, ValueError):
        ts = 0.0
    try:
        rd = float(rate_diff)
    except (TypeError, ValueError):
        rd = 0.0
    trail_regime = _resolve_trailing_regime(bias, ts, rd, timeframe=timeframe)
    return bias, str(trail_regime or "CHOPPY"), ts, rd


def _simulate_shadow_trade(
    signal: Mapping[str, Any],
    *,
    forward_df: pd.DataFrame,
    past_df: pd.DataFrame,
    atr: float | None,
    regime_state: str = "NEUTRAL",
    rate_diff: float = 0.0,
    trend_strength: float = 0.0,
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

    bias, trail_regime, ts_use, rd_use = _resolve_shadow_trail_inputs(
        regime_state=regime_state,
        direction=direction,
        timeframe=tf,
        trend_strength=trend_strength,
        rate_diff=rate_diff,
    )

    # Placeholder ladder levels — evaluate_forward_candles recomputes from risk
    # × regime multiples (2/4/7 TRENDING or 1.5/3/5 CHOPPY); passed tp* ignored.
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
            macro_bias=bias,
            macro_bias_adjusted=bias,
            trail_regime=trail_regime,
            trend_strength=ts_use,
            rate_differential=rd_use,
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
            macro_bias=bias,
            trail_regime=trail_regime,
            trend_strength=ts_use,
            rate_differential=rd_use,
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
    macro_bias: str = "NEUTRAL",
    trail_regime: str = "CHOPPY",
    trend_strength: float = 0.0,
    rate_differential: float = 0.0,
) -> dict[str, Any]:
    """
    Candle walk: normal stop/ladder/trail vs custom_exit — first trigger wins.
    Reuses the real forward engine per-prefix would be O(n²); instead walk once
    with custom checks against the normal exit bar.
    """
    from continuous_backtester import _evaluate_forward_with_trend_continuation

    bias = str(macro_bias or "NEUTRAL")
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
        macro_bias=bias,
        macro_bias_adjusted=bias,
        trail_regime=trail_regime,
        trend_strength=float(trend_strength or 0.0),
        rate_differential=float(rate_differential or 0.0),
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
            entry_ds = str(analysis_date).strip()[:10]
            # Next-open entries: fill from first forward bar open (signal used ≤ close).
            if sig.pop("entry_at_next_open", False):
                try:
                    nxt = float(pd.to_numeric(forward_df["Open"], errors="coerce").iloc[0])
                except Exception:  # noqa: BLE001
                    continue
                if nxt <= 0 or nxt != nxt:  # noqa: PLR0124 — NaN check
                    continue
                sig["entry_price"] = nxt
                try:
                    entry_ds = str(pd.Timestamp(forward_df.index[0]).date())
                except Exception:  # noqa: BLE001
                    entry_ds = str(analysis_date).strip()[:10]
            stop_atr_mult = sig.pop("stop_atr_mult", None)
            if stop_atr_mult is not None and sig.get("entry_price") is not None:
                try:
                    atr_use = float(context.atr or 0)
                    mult = float(stop_atr_mult)
                    ep = float(sig["entry_price"])
                    dirc = str(sig.get("direction") or "").upper()
                    if atr_use > 0 and ep > 0:
                        if dirc == "LONG":
                            sig["stop_price"] = ep - mult * atr_use
                        elif dirc == "SHORT":
                            sig["stop_price"] = ep + mult * atr_use
                except (TypeError, ValueError):
                    pass
            if sig.get("entry_price") is None or sig.get("stop_price") is None:
                continue
            trade = _simulate_shadow_trade(
                sig,
                forward_df=forward_df,
                past_df=context.ohlc(),
                atr=context.atr,
                regime_state=context.regime_state,
                rate_diff=context.rate_diff,
                trend_strength=context.trend_strength,
            )
            if trade is None:
                continue
            # Regime / shadow fields (write-only mirrors of existing shadow_*).
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



# ── PSH FAMILY — helpers + 15 STRONG_HEADWIND fade strategies ────────────────
# Shadow-only. Universal gate: STRONG_* regime and trade against score sign.

# Pending stop-order setups: (strategy_id, ticker, timeframe) -> state dict
_PSH_PENDING: dict[tuple[str, str, str], dict[str, Any]] = {}


def _psh_gate(ctx: ShadowStrategyContext) -> str | None:
    """
    Universal PSH gate. Returns trade direction (countertrend) or None.
    Requires STRONG_UP / STRONG_DOWN; trade AGAINST composite score sign.
    """
    state = str(ctx.regime_state or "").strip().upper()
    if state not in ("STRONG_UP", "STRONG_DOWN"):
        return None
    score = ctx.regime_raw_score
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s > 0:
        return "SHORT"
    if s < 0:
        return "LONG"
    return None


def _psh_ohlc_arrays(df: pd.DataFrame) -> tuple[Any, Any, Any, Any] | None:
    if df is None or getattr(df, "empty", True):
        return None
    need = ("Open", "High", "Low", "Close")
    if any(c not in df.columns for c in need):
        return None
    o = pd.to_numeric(df["Open"], errors="coerce")
    h = pd.to_numeric(df["High"], errors="coerce")
    l = pd.to_numeric(df["Low"], errors="coerce")
    c = pd.to_numeric(df["Close"], errors="coerce")
    return o, h, l, c


def _psh_atr_series(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    prev_c = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _psh_sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _psh_ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _psh_rsi(closes: pd.Series, n: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _psh_er_at(closes: list[float], end_i: int, n: int) -> float | None:
    """Kaufman ER on closes[end_i-n : end_i] inclusive end (n steps)."""
    if n <= 0 or end_i < n:
        return None
    window = closes[end_i - n : end_i + 1]
    if len(window) < n + 1:
        return None
    net = abs(window[-1] - window[0])
    path = 0.0
    for i in range(1, len(window)):
        path += abs(window[i] - window[i - 1])
    if path <= 0:
        return 0.0
    return float(net / path)


def _psh_bbands(closes: pd.Series, n: int = 20, n_sigma: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = closes.rolling(n, min_periods=n).mean()
    sd = closes.rolling(n, min_periods=n).std(ddof=0)
    return mid + n_sigma * sd, mid, mid - n_sigma * sd


def _psh_fractal_swings(
    highs: list[float],
    lows: list[float],
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """2-bar-each-side fractal swing highs / lows. Confirmed only (needs +2 bars)."""
    hi: list[tuple[int, float]] = []
    lo: list[tuple[int, float]] = []
    n = min(len(highs), len(lows))
    for i in range(2, n - 2):
        h = highs[i]
        l = lows[i]
        if h > highs[i - 1] and h > highs[i - 2] and h > highs[i + 1] and h > highs[i + 2]:
            hi.append((i, float(h)))
        if l < lows[i - 1] and l < lows[i - 2] and l < lows[i + 1] and l < lows[i + 2]:
            lo.append((i, float(l)))
    return hi, lo


def _psh_is_inside_day(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, i: int) -> bool:
    if i < 1:
        return False
    return float(h.iloc[i]) < float(h.iloc[i - 1]) and float(l.iloc[i]) > float(l.iloc[i - 1])


def _psh_is_nr7(h: pd.Series, l: pd.Series, i: int) -> bool:
    if i < 6:
        return False
    ranges = [(float(h.iloc[j]) - float(l.iloc[j])) for j in range(i - 6, i + 1)]
    return ranges[-1] <= min(ranges) + 1e-12


def _psh_weekday_trading_days(year: int, month: int) -> list[date]:
    """Mon–Fri calendar days in month (no holiday calendar available)."""
    out: list[date] = []
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:
            out.append(dt)
    return out


def _psh_signal(
    *,
    direction: str,
    stop_price: float,
    timeframe: str,
    strategy_id: str,
    custom_exit: dict[str, Any] | None = None,
    entry_at_next_open: bool = True,
    entry_price: float | None = None,
) -> dict[str, Any]:
    sig: dict[str, Any] = {
        "direction": direction,
        "stop_price": float(stop_price),
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "shadow_mult_flat": 1.0,
    }
    if custom_exit is not None:
        sig["custom_exit"] = custom_exit
    if entry_at_next_open:
        sig["entry_at_next_open"] = True
    else:
        if entry_price is None:
            raise ValueError("entry_price required when not entry_at_next_open")
        sig["entry_price"] = float(entry_price)
    return sig


def _psh_pending_key(sid: str, ctx: ShadowStrategyContext) -> tuple[str, str, str]:
    return (sid, ctx.ticker, ctx.timeframe)


def _psh_check_pending_stop(
    ctx: ShadowStrategyContext,
    sid: str,
) -> dict[str, Any] | None:
    """
    Check / age a pending stop-order. Trigger on the latest closed bar's range.
    Expire after 5 sessions or invalidate if close beyond stop side.
    """
    key = _psh_pending_key(sid, ctx)
    pend = _PSH_PENDING.get(key)
    if not pend:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 0:
        return None
    # Age by session: count bars since setup index
    setup_i = int(pend["setup_i"])
    age = i - setup_i
    if age <= 0:
        return None  # same bar as setup — wait for later sessions
    if age > 5:
        _PSH_PENDING.pop(key, None)
        return None
    direction = str(pend["direction"])
    trigger = float(pend["trigger"])
    stop_px = float(pend["stop"])
    open_i = float(o.iloc[i])
    high_i = float(h.iloc[i])
    low_i = float(l.iloc[i])
    close_i = float(c.iloc[i])
    # Invalidate if close beyond stop side
    if direction == "LONG" and close_i < stop_px:
        _PSH_PENDING.pop(key, None)
        return None
    if direction == "SHORT" and close_i > stop_px:
        _PSH_PENDING.pop(key, None)
        return None
    filled = False
    fill_px = trigger
    if direction == "LONG":
        if low_i <= trigger:
            filled = True
            fill_px = open_i if open_i < trigger else trigger
    else:
        if high_i >= trigger:
            filled = True
            fill_px = open_i if open_i > trigger else trigger
    if not filled:
        return None
    _PSH_PENDING.pop(key, None)
    return _psh_signal(
        direction=direction,
        stop_price=stop_px,
        timeframe=ctx.timeframe,
        strategy_id=sid,
        entry_at_next_open=False,
        entry_price=fill_px,
    )


def _psh_set_pending(
    ctx: ShadowStrategyContext,
    sid: str,
    *,
    direction: str,
    trigger: float,
    stop: float,
    setup_i: int,
) -> None:
    _PSH_PENDING[_psh_pending_key(sid, ctx)] = {
        "direction": direction,
        "trigger": float(trigger),
        "stop": float(stop),
        "setup_i": int(setup_i),
        "setup_date": ctx.session_date,
    }


def _psh_macro_up(direction: str) -> bool:
    """True when macro trend is up (we are fading with SHORT)."""
    return direction == "SHORT"


def _psh_build_weekly_from_daily(df: pd.DataFrame) -> pd.DataFrame | None:
    """Resample daily OHLC to weekly (W-FRI). Only completed weeks used by callers."""
    if df is None or df.empty:
        return None
    x = df.copy()
    if not isinstance(x.index, pd.DatetimeIndex):
        try:
            x.index = pd.to_datetime(x.index)
        except Exception:  # noqa: BLE001
            return None
    o = x["Open"].resample("W-FRI").first()
    h = x["High"].resample("W-FRI").max()
    l = x["Low"].resample("W-FRI").min()
    c = x["Close"].resample("W-FRI").last()
    out = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}).dropna()
    return out if len(out) else None


# ── PSH01 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH01_EFFICIENCY_STALL_FADE")
def psh01_efficiency_stall_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 40:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    rsi_s = _psh_rsi(c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    closes = [float(x) for x in c.tolist()]
    # 20-day extreme in macro direction (including today)
    window = closes[i - 19 : i + 1]
    if len(window) < 20:
        return None
    macro_up = _psh_macro_up(direction)
    if macro_up:
        if closes[i] < max(window) - 1e-12:
            return None
        extreme = closes[i]
        # prior 20-day extreme day (before today)
        prior = closes[i - 20 : i]
        if not prior:
            return None
        prev_ext_i = i - 20 + int(max(range(len(prior)), key=lambda k: prior[k]))
    else:
        if closes[i] > min(window) + 1e-12:
            return None
        extreme = closes[i]
        prior = closes[i - 20 : i]
        if not prior:
            return None
        prev_ext_i = i - 20 + int(min(range(len(prior)), key=lambda k: prior[k]))
    # ER(10) declined ≥0.15 from its own 20-day peak
    er_now = _psh_er_at(closes, i, 10)
    if er_now is None:
        return None
    er_peak = None
    for j in range(i - 19, i + 1):
        ej = _psh_er_at(closes, j, 10)
        if ej is None:
            continue
        er_peak = ej if er_peak is None else max(er_peak, ej)
    if er_peak is None or (er_peak - er_now) < 0.15:
        return None
    rsi_now = float(rsi_s.iloc[i]) if pd.notna(rsi_s.iloc[i]) else None
    rsi_prev = float(rsi_s.iloc[prev_ext_i]) if pd.notna(rsi_s.iloc[prev_ext_i]) else None
    if rsi_now is None or rsi_prev is None:
        return None
    if direction == "LONG" and not (rsi_now > rsi_prev):
        return None
    if direction == "SHORT" and not (rsi_now < rsi_prev):
        return None
    stop = extreme - atr if direction == "LONG" else extreme + atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PSH01_EFFICIENCY_STALL_FADE",
    )


# ── PSH02 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH02_STRETCH_BAND_REVERSION")
def psh02_stretch_band_reversion(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    sid = "PSH02_STRETCH_BAND_REVERSION"
    hit = _psh_check_pending_stop(ctx, sid)
    if hit is not None:
        return hit
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 30:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    sma20 = _psh_sma(c, 20)
    i = len(c) - 1
    # Setup: stretch day then inside day — check if today is inside day after stretch
    if not _psh_is_inside_day(o, h, l, c, i):
        return None
    j = i - 1  # stretch candidate
    if j < 20:
        return None
    atr_j = float(atr_s.iloc[j]) if pd.notna(atr_s.iloc[j]) else None
    sma_j = float(sma20.iloc[j]) if pd.notna(sma20.iloc[j]) else None
    if atr_j is None or atr_j <= 0 or sma_j is None:
        return None
    close_j = float(c.iloc[j])
    macro_up = _psh_macro_up(direction)
    if macro_up:
        if close_j < sma_j + 2.5 * atr_j:
            return None
        ext_extreme = float(h.iloc[j])
        # countertrend extreme of inside day = its low (for SHORT entry break below)
        trigger = float(l.iloc[i])
        stop = ext_extreme
    else:
        if close_j > sma_j - 2.5 * atr_j:
            return None
        ext_extreme = float(l.iloc[j])
        trigger = float(h.iloc[i])
        stop = ext_extreme
    _psh_set_pending(ctx, sid, direction=direction, trigger=trigger, stop=stop, setup_i=i)
    return None


# ── PSH03 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH03_WEEKLY_EXHAUSTION_WICK")
def psh03_weekly_exhaustion_wick(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1w":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 12:
        return None
    i = len(c) - 1
    ranges = [(float(h.iloc[k]) - float(l.iloc[k])) for k in range(i - 9, i + 1)]
    if len(ranges) < 10:
        return None
    avg10 = sum(ranges[:-1]) / 9.0
    rng = ranges[-1]
    if avg10 <= 0 or rng < 1.5 * avg10:
        return None
    open_i, high_i, low_i, close_i = float(o.iloc[i]), float(h.iloc[i]), float(l.iloc[i]), float(c.iloc[i])
    macro_up = _psh_macro_up(direction)
    # Closing against macro direction
    if macro_up and not (close_i < open_i):
        return None
    if (not macro_up) and not (close_i > open_i):
        return None
    if macro_up:
        wick = high_i - max(open_i, close_i)
        extreme = high_i
    else:
        wick = min(open_i, close_i) - low_i
        extreme = low_i
    if rng <= 0 or wick / rng < 0.60:
        return None
    return _psh_signal(
        direction=direction, stop_price=extreme, timeframe="1w",
        strategy_id="PSH03_WEEKLY_EXHAUSTION_WICK",
    )


# ── PSH04 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH04_BREAKOUT_FAILURE_SNAP")
def psh04_breakout_failure_snap(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 30:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    closes = [float(x) for x in c.tolist()]
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    macro_up = _psh_macro_up(direction)
    # Find a breakout close within last 3 sessions (before today) beyond then-20d extreme
    found = None
    for b in range(max(20, i - 3), i):
        prior = closes[b - 20 : b]
        if len(prior) < 20:
            continue
        atr_b = float(atr_s.iloc[b]) if pd.notna(atr_s.iloc[b]) else None
        if atr_b is None or atr_b <= 0:
            continue
        if macro_up:
            if closes[b] > max(prior):
                found = (b, max(prior), highs[b], atr_b)
                break
        else:
            if closes[b] < min(prior):
                found = (b, min(prior), lows[b], atr_b)
                break
    if found is None:
        return None
    b, prior_ext, bo_extreme, atr_b = found
    # Today close back inside prior range by ≥0.5×ATR
    atr_i = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else atr_b
    if macro_up:
        if not (closes[i] <= prior_ext - 0.5 * atr_i):
            return None
        stop = max(highs[b : i + 1])
    else:
        if not (closes[i] >= prior_ext + 0.5 * atr_i):
            return None
        stop = min(lows[b : i + 1])
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PSH04_BREAKOUT_FAILURE_SNAP",
    )


# ── PSH05 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH05_THREE_PUSH_TERMINAL")
def psh05_three_push_terminal(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 50:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    closes = [float(x) for x in c.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    macro_up = _psh_macro_up(direction)
    swings = f_hi if macro_up else f_lo
    if len(swings) < 4:
        return None
    s0, s1, s2, s3 = swings[-4], swings[-3], swings[-2], swings[-1]
    if macro_up:
        e1 = (s1[1] - s0[1]) / atr
        e2 = (s2[1] - s1[1]) / atr
        e3 = (s3[1] - s2[1]) / atr
    else:
        e1 = (s0[1] - s1[1]) / atr
        e2 = (s1[1] - s2[1]) / atr
        e3 = (s2[1] - s3[1]) / atr
    if not (e3 < e2 < e1 and e1 > 0 and e2 > 0 and e3 > 0):
        return None
    # Trigger: close beyond most recent countertrend fractal
    ctr = f_lo if macro_up else f_hi
    if not ctr:
        return None
    last_ctr_i, last_ctr_px = ctr[-1]
    if last_ctr_i < s3[0]:
        # need a countertrend swing after/near pushes — use most recent overall
        pass
    if macro_up:
        if closes[i] >= last_ctr_px:
            return None
    else:
        if closes[i] <= last_ctr_px:
            return None
    stop = s3[1]
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PSH05_THREE_PUSH_TERMINAL",
    )


# ── PSH06 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH06_SWEEP_RECLAIM")
def psh06_sweep_reclaim(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 40:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    macro_up = _psh_macro_up(direction)
    swings = f_hi if macro_up else f_lo
    if len(swings) < 2:
        return None
    # find pair within 0.25 ATR, ≥5 sessions apart
    pair = None
    for a in range(len(swings) - 1):
        for b in range(a + 1, len(swings)):
            ia, pa = swings[a]
            ib, pb = swings[b]
            if ib - ia < 5:
                continue
            if abs(pa - pb) <= 0.25 * atr:
                pair = (ia, pa, ib, pb)
    if pair is None:
        return None
    _, p1, _, p2 = pair
    level = (p1 + p2) / 2.0
    high_i, low_i, close_i = highs[i], lows[i], float(c.iloc[i])
    if macro_up:
        # Sweep above equal highs by ≤0.5×ATR, same bar closes back through level.
        beyond = high_i - level
        if beyond <= 0 or beyond > 0.5 * atr:
            return None
        if close_i >= level:
            return None
        stop = high_i
    else:
        beyond = level - low_i
        if beyond <= 0 or beyond > 0.5 * atr:
            return None
        if close_i <= level:
            return None
        stop = low_i
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PSH06_SWEEP_RECLAIM",
    )


# ── PSH07 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH07_COMPRESSION_SPRING")
def psh07_compression_spring(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    sid = "PSH07_COMPRESSION_SPRING"
    hit = _psh_check_pending_stop(ctx, sid)
    if hit is not None:
        return hit
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 45:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    if not _psh_is_nr7(h, l, i):
        return None
    atr_i = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr_i is None or atr_i <= 0:
        return None
    atr_window = atr_s.iloc[i - 39 : i + 1].dropna()
    if len(atr_window) < 40 or float(atr_i) > float(atr_window.min()) + 1e-12:
        return None
    closes = [float(x) for x in c.tolist()]
    macro_up = _psh_macro_up(direction)
    win = closes[i - 19 : i + 1]
    if len(win) < 20:
        return None
    if macro_up:
        ext = max(win)
        if abs(closes[i] - ext) > atr_i:
            return None
        trigger = float(l.iloc[i])  # break NR7 countertrend side
        stop = float(h.iloc[i])
    else:
        ext = min(win)
        if abs(closes[i] - ext) > atr_i:
            return None
        trigger = float(h.iloc[i])
        stop = float(l.iloc[i])
    _psh_set_pending(ctx, sid, direction=direction, trigger=trigger, stop=stop, setup_i=i)
    return None


# ── PSH08 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH08_BB_WALK_BREAK")
def psh08_bb_walk_break(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 30:
        return None
    upper2, mid, lower2 = _psh_bbands(c, 20, 2.0)
    upper1, _, lower1 = _psh_bbands(c, 20, 1.0)
    i = len(c) - 1
    macro_up = _psh_macro_up(direction)
    beyond = 0
    for j in range(i - 5, i + 1):
        cj = float(c.iloc[j])
        if macro_up:
            u = upper2.iloc[j]
            if pd.notna(u) and cj > float(u):
                beyond += 1
        else:
            lo = lower2.iloc[j]
            if pd.notna(lo) and cj < float(lo):
                beyond += 1
    if beyond < 4:
        return None
    ci = float(c.iloc[i])
    if macro_up:
        u1 = upper1.iloc[i]
        if not (pd.notna(u1) and ci <= float(u1)):
            return None
        stop = float(h.iloc[i - 5 : i + 1].max())
    else:
        l1 = lower1.iloc[i]
        if not (pd.notna(l1) and ci >= float(l1)):
            return None
        stop = float(l.iloc[i - 5 : i + 1].min())
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PSH08_BB_WALK_BREAK",
    )


# ── PSH09 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH09_RATE_IMPULSE_FADE")
def psh09_rate_impulse_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    hist = [x for x in ctx.rate_component_history() if x is not None]
    if len(hist) < 6:
        return None
    older = float(hist[-6])
    newer = float(hist[-1])
    delta = newer - older
    # Move ≥0.10 against the level's sign (level = older reading)
    if older < 0 and not (delta >= 0.10):
        return None
    if older > 0 and not (delta <= -0.10):
        return None
    if older == 0:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 15:
        return None
    ema10 = _psh_ema(c, 10)
    i = len(c) - 1
    if pd.isna(ema10.iloc[i]) or pd.isna(ema10.iloc[i - 1]):
        return None
    ci, cprev = float(c.iloc[i]), float(c.iloc[i - 1])
    ei, eprev = float(ema10.iloc[i]), float(ema10.iloc[i - 1])
    # Daily close crosses 10-EMA in the trade direction
    if direction == "LONG":
        if not (cprev <= eprev and ci > ei):
            return None
    else:
        if not (cprev >= eprev and ci < ei):
            return None
    atr_s = _psh_atr_series(h, l, c, 14)
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    sig = _psh_signal(
        direction=direction, stop_price=ci, timeframe="1d",
        strategy_id="PSH09_RATE_IMPULSE_FADE",
    )
    sig["stop_atr_mult"] = 1.25  # applied from next-open entry in evaluate
    return sig


# ── PSH10 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH10_CONFIDENCE_DECAY_TURN")
def psh10_confidence_decay_turn(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    conf = [x for x in ctx.confidence_history() if x is not None]
    if len(conf) < 5:
        return None
    last5 = [float(x) for x in conf[-5:]]
    if last5[0] < 0.80 or last5[-1] > 0.55:
        return None
    if not all(last5[k] >= last5[k + 1] for k in range(4)):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 30:
        return None
    # 2 consecutive countertrend daily closes
    if direction == "LONG":
        if not (float(c.iloc[-1]) > float(o.iloc[-1]) and float(c.iloc[-2]) > float(o.iloc[-2])):
            return None
    else:
        if not (float(c.iloc[-1]) < float(o.iloc[-1]) and float(c.iloc[-2]) < float(o.iloc[-2])):
            return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    macro_up = _psh_macro_up(direction)
    swings = f_hi if macro_up else f_lo
    if not swings:
        return None
    _, ext = swings[-1]
    stop = ext + atr if macro_up else ext - atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PSH10_CONFIDENCE_DECAY_TURN",
    )


# ── PSH11 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH11_MONTH_END_REBALANCE")
def psh11_month_end_rebalance(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Signal on close of the day BEFORE the penultimate weekday of the month;
    entry next open = penultimate day open.
    Custom time_exit max_sessions=5: penultimate(1), month-end(2), 1st(3), 2nd(4),
    3rd(5) → exit at close of 3rd trading day of the new month (weekday calendar).
    """
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    try:
        d = date.fromisoformat(ctx.session_date)
    except ValueError:
        return None
    tdays = _psh_weekday_trading_days(d.year, d.month)
    if len(tdays) < 3:
        return None
    penult = tdays[-2]
    # Prior trading day before penultimate
    prior = tdays[-3]
    if d != prior:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 25:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    # Month-to-date move through prior close
    month_mask = []
    idx = ctx.ohlc().index
    for k in range(len(c)):
        try:
            dk = pd.Timestamp(idx[k]).date()
        except Exception:  # noqa: BLE001
            continue
        if dk.year == d.year and dk.month == d.month:
            month_mask.append(k)
    if len(month_mask) < 2:
        return None
    first_i, last_i = month_mask[0], month_mask[-1]
    mtd = float(c.iloc[last_i]) - float(c.iloc[first_i])
    macro_up = _psh_macro_up(direction)
    if macro_up and mtd < 2.0 * atr:
        return None
    if (not macro_up) and mtd > -2.0 * atr:
        return None
    sig = _psh_signal(
        direction=direction,
        stop_price=float(c.iloc[i]),
        timeframe="1d",
        strategy_id="PSH11_MONTH_END_REBALANCE",
        custom_exit={"type": "time_exit", "max_sessions": 5},
    )
    sig["stop_atr_mult"] = 1.5  # fixed 1.5×ATR from next-open entry
    return sig


# ── PSH12 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH12_FRIDAY_UNWIND")
def psh12_friday_unwind(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Thursday close signal; entry Friday open.
    Custom time_exit max_sessions=2: Fri(1), Mon(2) → exit at Monday's close.
    """
    if ctx.timeframe != "1d":
        return None
    if ctx.day_of_week != 3:  # Thursday
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 10:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    # Week-to-date: from most recent Monday through Thursday
    idx = ctx.ohlc().index
    mon_i = None
    for k in range(i, -1, -1):
        try:
            dk = pd.Timestamp(idx[k]).date()
        except Exception:  # noqa: BLE001
            continue
        if dk.weekday() == 0:
            mon_i = k
            break
    if mon_i is None:
        return None
    wtd = float(c.iloc[i]) - float(c.iloc[mon_i])
    macro_up = _psh_macro_up(direction)
    if macro_up and wtd < 1.5 * atr:
        return None
    if (not macro_up) and wtd > -1.5 * atr:
        return None
    sig = _psh_signal(
        direction=direction,
        stop_price=float(c.iloc[i]),
        timeframe="1d",
        strategy_id="PSH12_FRIDAY_UNWIND",
        custom_exit={"type": "time_exit", "max_sessions": 2},
    )
    sig["stop_atr_mult"] = 1.0  # 1.0×ATR from Friday open entry
    return sig


# ── PSH13 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH13_WEEKLY_RECLAIM_ROTATION")
def psh13_weekly_reclaim_rotation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Weekly pattern detected from daily OHLC (completed W-FRI weeks).
    Runs on 1d so next daily open entry is available via entry_at_next_open.
    """
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    daily = ctx.ohlc()
    weekly = _psh_build_weekly_from_daily(daily)
    if weekly is None or len(weekly) < 6:
        return None
    # Only evaluate on the daily session that completes a week (Friday)
    if ctx.day_of_week != 4:
        return None
    w_o = pd.to_numeric(weekly["Open"], errors="coerce")
    w_h = pd.to_numeric(weekly["High"], errors="coerce")
    w_l = pd.to_numeric(weekly["Low"], errors="coerce")
    w_c = pd.to_numeric(weekly["Close"], errors="coerce")
    wi = len(w_c) - 1
    macro_up = _psh_macro_up(direction)
    # ≥4 consecutive weekly closes in macro direction, then reclaim week
    if wi < 4:
        return None
    for k in range(wi - 4, wi):
        # weeks wi-4 .. wi-1 are the streak; wi is reversal week
        ok = float(w_c.iloc[k]) > float(w_o.iloc[k]) if macro_up else float(w_c.iloc[k]) < float(w_o.iloc[k])
        if not ok:
            return None
    prev_rng = float(w_h.iloc[wi - 1]) - float(w_l.iloc[wi - 1])
    if prev_rng <= 0:
        return None
    # Reversal week closes against macro, reclaiming ≥50% of prior week's range
    if macro_up:
        if not (float(w_c.iloc[wi]) < float(w_o.iloc[wi])):
            return None
        reclaim = float(w_h.iloc[wi - 1]) - float(w_c.iloc[wi])
        if reclaim < 0.5 * prev_rng:
            return None
        stop = float(w_h.iloc[wi - 1])
    else:
        if not (float(w_c.iloc[wi]) > float(w_o.iloc[wi])):
            return None
        reclaim = float(w_c.iloc[wi]) - float(w_l.iloc[wi - 1])
        if reclaim < 0.5 * prev_rng:
            return None
        stop = float(w_l.iloc[wi - 1])
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PSH13_WEEKLY_RECLAIM_ROTATION",
    )


# ── PSH14 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH14_GAP_AGAINST_TREND")
def psh14_gap_against_trend(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 20:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr is None or atr <= 0:
        return None
    prev_c = float(c.iloc[i - 1])
    open_i = float(o.iloc[i])
    close_i = float(c.iloc[i])
    gap = open_i - prev_c
    # Gap countertrend ≥0.3 ATR and day closes in gap direction
    if direction == "LONG":
        # countertrend gap is up from bearish macro? trade LONG against down macro
        # gap against trend = gap up when macro down
        if gap < 0.3 * atr:
            return None
        if close_i < open_i:  # must close in gap direction (up)
            return None
        stop = float(l.iloc[i - 1])  # pre-gap extreme
    else:
        if gap > -0.3 * atr:
            return None
        if close_i > open_i:
            return None
        stop = float(h.iloc[i - 1])
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PSH14_GAP_AGAINST_TREND",
    )


# ── PSH15 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PSH15_ASYM_LOTTERY_REVERSAL")
def psh15_asym_lottery_reversal(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _psh_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    if len(c) < 55:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    sma50 = _psh_sma(c, 50)
    i = len(c) - 1
    atr = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    s50 = float(sma50.iloc[i]) if pd.notna(sma50.iloc[i]) else None
    if atr is None or atr <= 0 or s50 is None:
        return None
    closes = [float(x) for x in c.tolist()]
    er10 = _psh_er_at(closes, i, 10)
    if er10 is None or er10 < 0.70:
        return None
    ci = closes[i]
    macro_up = _psh_macro_up(direction)
    if macro_up:
        if ci < s50 + 3.0 * atr:
            return None
        day_ext = float(h.iloc[i])
        stop = day_ext + 0.5 * atr
    else:
        if ci > s50 - 3.0 * atr:
            return None
        day_ext = float(l.iloc[i])
        stop = day_ext - 0.5 * atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PSH15_ASYM_LOTTERY_REVERSAL",
    )


# ── PART F — validation dummies (remove in a later prompt) ───────────────────
def _pdummy_eurusd_monday_long(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """Shared entry for PDUMMY01/02: EURUSD 1d Monday open, 1×ATR stop."""
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
    }


@register_shadow_strategy("PDUMMY01_VALIDATION")
def pdummy01_validation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Trivial long on EURUSD 1d every Monday open, stop = entry − 1×ATR.
    Proves signal → sim → JSONL end-to-end. No custom exit.
    """
    sig = _pdummy_eurusd_monday_long(ctx)
    if sig is None:
        return None
    sig["strategy_id"] = "PDUMMY01_VALIDATION"
    return sig


@register_shadow_strategy("PDUMMY02_TIMEEXIT")
def pdummy02_timeexit(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Same as PDUMMY01 but with time_exit after 5 sessions — proves custom_exit path.
    """
    sig = _pdummy_eurusd_monday_long(ctx)
    if sig is None:
        return None
    sig["strategy_id"] = "PDUMMY02_TIMEEXIT"
    sig["custom_exit"] = {"type": "time_exit", "max_sessions": 5}
    return sig
