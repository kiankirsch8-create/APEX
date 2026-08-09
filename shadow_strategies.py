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
            if stop_atr_mult is not None:
                try:
                    atr_use = float(context.atr or 0)
                    mult = float(stop_atr_mult)
                    ep = float(sig["entry_price"]) if sig.get("entry_price") is not None else 0.0
                    dirc = str(sig.get("direction") or "").upper()
                    if atr_use <= 0 or ep <= 0 or ep != ep or dirc not in ("LONG", "SHORT"):
                        continue
                    if dirc == "LONG":
                        sig["stop_price"] = ep - mult * atr_use
                    else:
                        sig["stop_price"] = ep + mult * atr_use
                except (TypeError, ValueError):
                    continue
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
    Age is anchored on setup_date (not a brittle absolute bar index).
    """
    key = _psh_pending_key(sid, ctx)
    pend = _PSH_PENDING.get(key)
    if not pend:
        return None
    df = ctx.ohlc()
    arr = _psh_ohlc_arrays(df)
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 0:
        return None
    setup_ds = str(pend.get("setup_date") or "").strip()[:10]
    if not setup_ds:
        _PSH_PENDING.pop(key, None)
        return None
    # Locate setup_date in current OHLC; missing → stale pending (cross-job leak).
    setup_i: int | None = None
    for k in range(len(df)):
        try:
            dk = str(pd.Timestamp(df.index[k]).date())
        except Exception:  # noqa: BLE001
            continue
        if dk == setup_ds:
            setup_i = k
    if setup_i is None:
        _PSH_PENDING.pop(key, None)
        return None
    age = i - setup_i  # bars strictly after setup when age > 0
    if age <= 0:
        return None  # same bar as setup — wait for later sessions
    if age > 5:
        _PSH_PENDING.pop(key, None)
        return None
    # Drop pending if regime is no longer STRONG.
    state = str(ctx.regime_state or "").strip().upper()
    if state not in ("STRONG_UP", "STRONG_DOWN"):
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
    # LONG = buy-stop above; SHORT = sell-stop below.
    filled = False
    fill_px = trigger
    if direction == "LONG":
        if high_i >= trigger:
            filled = True
            fill_px = open_i if open_i > trigger else trigger
    else:
        if low_i <= trigger:
            filled = True
            fill_px = open_i if open_i < trigger else trigger
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
) -> None:
    _PSH_PENDING[_psh_pending_key(sid, ctx)] = {
        "direction": direction,
        "trigger": float(trigger),
        "stop": float(stop),
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
    _psh_set_pending(ctx, sid, direction=direction, trigger=trigger, stop=stop)
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
    _psh_set_pending(ctx, sid, direction=direction, trigger=trigger, stop=stop)
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



# ── PHW FAMILY — helpers + 15 HEADWIND shadow strategies ─────────────────────
# Shadow-only. Gate via _shadow_bias_from_state(..., direction) == "HEADWIND".

_PHW_PENDING: dict[tuple[str, str, str], dict[str, Any]] = {}


def _phw_gate(ctx: ShadowStrategyContext) -> str | None:
    """Return the unique trade direction that maps to HEADWIND, or None."""
    from continuous_backtester import _shadow_bias_from_state

    for direction in ("LONG", "SHORT"):
        if _shadow_bias_from_state(ctx.regime_state, direction) == "HEADWIND":
            return direction
    return None


def _phw_macro_dir(direction: str) -> str:
    """Macro (headwind) direction is the opposite of the trade direction."""
    return "UP" if str(direction).upper() == "SHORT" else "DOWN"


def _phw_range_20(
    h: pd.Series, l: pd.Series, i: int,
) -> tuple[float, float, float, float] | None:
    """20-bar High/Low range over [i-19, i]: (high, low, mid, width)."""
    if i < 19:
        return None
    try:
        hi = float(h.iloc[i - 19 : i + 1].max())
        lo = float(l.iloc[i - 19 : i + 1].min())
    except Exception:  # noqa: BLE001
        return None
    if hi != hi or lo != lo:  # NaN
        return None
    width = hi - lo
    if width < 0:
        return None
    return hi, lo, (hi + lo) / 2.0, width


def _phw_range_prior_20(
    h: pd.Series, l: pd.Series, i: int,
) -> tuple[float, float, float, float] | None:
    """20-bar High/Low range over [i-20, i-1] — EXCLUDES bar i."""
    if i < 20:
        return None
    try:
        hi = float(h.iloc[i - 20 : i].max())
        lo = float(l.iloc[i - 20 : i].min())
    except Exception:  # noqa: BLE001
        return None
    if hi != hi or lo != lo:  # NaN
        return None
    width = hi - lo
    if width < 0:
        return None
    return hi, lo, (hi + lo) / 2.0, width


def _phw_boundary_touches(
    h: pd.Series,
    l: pd.Series,
    c: pd.Series,
    i: int,
    boundary_px: float,
    side: str,
    atr: float,
    lookback: int = 40,
    tol_mult: float = 0.25,
) -> int:
    """
    Count prior sessions in [i-lookback+1, i-1] within tol_mult*ATR of boundary.
    Returns -1 if any close in that window is beyond the boundary.
    """
    if atr <= 0 or i < 1:
        return -1
    start = max(0, i - lookback + 1)
    end = i  # exclusive — prior sessions only
    if start >= end:
        return 0
    tol = tol_mult * atr
    side_u = str(side).strip().lower()
    touches = 0
    for j in range(start, end):
        try:
            cj = float(c.iloc[j])
            hj = float(h.iloc[j])
            lj = float(l.iloc[j])
        except Exception:  # noqa: BLE001
            return -1
        if cj != cj or hj != hj or lj != lj:
            return -1
        if side_u == "upper":
            if cj > boundary_px:
                return -1
            if abs(hj - boundary_px) <= tol:
                touches += 1
        elif side_u == "lower":
            if cj < boundary_px:
                return -1
            if abs(lj - boundary_px) <= tol:
                touches += 1
        else:
            return -1
    return touches


def _phw_pending_key(sid: str, ctx: ShadowStrategyContext) -> tuple[str, str, str]:
    return (sid, ctx.ticker, ctx.timeframe)


def _phw_check_pending_stop(
    ctx: ShadowStrategyContext,
    sid: str,
) -> dict[str, Any] | None:
    """
    Pending stop-order check — same corrected mechanics as PSH, but re-gates
    on HEADWIND for the pending's stored direction.
    """
    key = _phw_pending_key(sid, ctx)
    pend = _PHW_PENDING.get(key)
    if not pend:
        return None
    df = ctx.ohlc()
    arr = _psh_ohlc_arrays(df)
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 0:
        return None
    setup_ds = str(pend.get("setup_date") or "").strip()[:10]
    if not setup_ds:
        _PHW_PENDING.pop(key, None)
        return None
    setup_i: int | None = None
    for k in range(len(df)):
        try:
            dk = str(pd.Timestamp(df.index[k]).date())
        except Exception:  # noqa: BLE001
            continue
        if dk == setup_ds:
            setup_i = k
    if setup_i is None:
        _PHW_PENDING.pop(key, None)
        return None
    age = i - setup_i
    if age <= 0:
        return None
    if age > 5:
        _PHW_PENDING.pop(key, None)
        return None
    # Re-gate: pending direction must still map to HEADWIND.
    from continuous_backtester import _shadow_bias_from_state

    direction = str(pend["direction"])
    if _shadow_bias_from_state(ctx.regime_state, direction) != "HEADWIND":
        _PHW_PENDING.pop(key, None)
        return None
    trigger = float(pend["trigger"])
    stop_px = float(pend["stop"])
    open_i = float(o.iloc[i])
    high_i = float(h.iloc[i])
    low_i = float(l.iloc[i])
    close_i = float(c.iloc[i])
    if direction == "LONG" and close_i < stop_px:
        _PHW_PENDING.pop(key, None)
        return None
    if direction == "SHORT" and close_i > stop_px:
        _PHW_PENDING.pop(key, None)
        return None
    filled = False
    fill_px = trigger
    if direction == "LONG":
        if high_i >= trigger:
            filled = True
            fill_px = open_i if open_i > trigger else trigger
    else:
        if low_i <= trigger:
            filled = True
            fill_px = open_i if open_i < trigger else trigger
    if not filled:
        return None
    _PHW_PENDING.pop(key, None)
    return _psh_signal(
        direction=direction,
        stop_price=stop_px,
        timeframe=ctx.timeframe,
        strategy_id=sid,
        entry_at_next_open=False,
        entry_price=fill_px,
    )


def _phw_set_pending(
    ctx: ShadowStrategyContext,
    sid: str,
    *,
    direction: str,
    trigger: float,
    stop: float,
) -> None:
    _PHW_PENDING[_phw_pending_key(sid, ctx)] = {
        "direction": direction,
        "trigger": float(trigger),
        "stop": float(stop),
        "setup_date": ctx.session_date,
    }


def _phw_atr_at(
    h: pd.Series, l: pd.Series, c: pd.Series, i: int,
) -> float | None:
    atr_s = _psh_atr_series(h, l, c, 14)
    try:
        v = float(atr_s.iloc[i])
    except Exception:  # noqa: BLE001
        return None
    if v != v or v <= 0:
        return None
    return v


# ── PHW01 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW01_VALIDATED_RANGE_FADE")
def phw01_validated_range_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_prior_20(h, l, i)
    if atr is None or rng is None:
        return None
    hi, lo, _mid, width = rng
    if width < 3.0 * atr:
        return None
    if direction == "LONG":
        boundary, side = lo, "lower"
        touches = _phw_boundary_touches(h, l, c, i, boundary, side, atr)
        if touches < 2:
            return None
        if float(l.iloc[i]) >= boundary:
            return None
        if float(c.iloc[i]) < boundary + 0.3 * atr:
            return None
        stop = boundary - 0.75 * atr
    else:
        boundary, side = hi, "upper"
        touches = _phw_boundary_touches(h, l, c, i, boundary, side, atr)
        if touches < 2:
            return None
        if float(h.iloc[i]) <= boundary:
            return None
        if float(c.iloc[i]) > boundary - 0.3 * atr:
            return None
        stop = boundary + 0.75 * atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW01_VALIDATED_RANGE_FADE",
    )


# ── PHW02 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW02_FALSE_BREAK_SPRING")
def phw02_false_break_spring(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 22:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    macro = _phw_macro_dir(direction)
    found = None
    for b in (i - 2, i - 1):
        if b < 20:
            continue
        prior_hi = float(h.iloc[b - 20 : b].max())
        prior_lo = float(l.iloc[b - 20 : b].min())
        cb = float(c.iloc[b])
        if macro == "UP" and cb > prior_hi:
            found = (b, prior_hi, prior_lo)
            break
        if macro == "DOWN" and cb < prior_lo:
            found = (b, prior_hi, prior_lo)
            break
    if found is None:
        return None
    b, prior_hi, prior_lo = found
    ci = float(c.iloc[i])
    if not (prior_lo < ci < prior_hi):
        return None
    if direction == "LONG":
        stop = float(l.iloc[b : i + 1].min())
    else:
        stop = float(h.iloc[b : i + 1].max())
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW02_FALSE_BREAK_SPRING",
    )


# ── PHW03 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW03_BOUNDARY_COMPRESSION_COIL")
def phw03_boundary_compression_coil(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    sid = "PHW03_BOUNDARY_COMPRESSION_COIL"
    hit = _phw_check_pending_stop(ctx, sid)
    if hit is not None:
        return hit
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if not _psh_is_nr7(h, l, i):
        return None
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_prior_20(h, l, i)
    if atr is None or rng is None:
        return None
    hi, lo, _mid, _w = rng
    if direction == "LONG":
        boundary, side = lo, "lower"
        extreme = float(l.iloc[i])
        if abs(extreme - boundary) > 0.5 * atr:
            return None
        touches = _phw_boundary_touches(h, l, c, i, boundary, side, atr)
        if touches < 2:
            return None
        trigger = float(h.iloc[i])  # range-interior side
        stop = float(l.iloc[i])
    else:
        boundary, side = hi, "upper"
        extreme = float(h.iloc[i])
        if abs(extreme - boundary) > 0.5 * atr:
            return None
        touches = _phw_boundary_touches(h, l, c, i, boundary, side, atr)
        if touches < 2:
            return None
        trigger = float(l.iloc[i])
        stop = float(h.iloc[i])
    _phw_set_pending(ctx, sid, direction=direction, trigger=trigger, stop=stop)
    return None


# ── PHW04 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW04_ZSCORE_SNAPBACK")
def phw04_zscore_snapback(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 21:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    sma20 = _psh_sma(c, 20)
    if pd.isna(sma20.iloc[i - 1]):
        return None
    sma_p = float(sma20.iloc[i - 1])
    c_p = float(c.iloc[i - 1])
    atr_p = _phw_atr_at(h, l, c, i - 1)
    if atr_p is None:
        return None
    macro = _phw_macro_dir(direction)
    if macro == "UP":
        if c_p < sma_p + 2.0 * atr_p:
            return None
    else:
        if c_p > sma_p - 2.0 * atr_p:
            return None
    oi, hi_i, li, ci = float(o.iloc[i]), float(h.iloc[i]), float(l.iloc[i]), float(c.iloc[i])
    bar_rng = hi_i - li
    if bar_rng <= 0:
        return None
    if direction == "LONG":
        if not (ci > oi):
            return None
        # Trade-side third = top third of the bar's range.
        if ci < hi_i - bar_rng / 3.0:
            return None
        stop = float(l.iloc[i - 1 : i + 1].min())
    else:
        if not (ci < oi):
            return None
        # Trade-side third = bottom third of the bar's range.
        if ci > li + bar_rng / 3.0:
            return None
        stop = float(h.iloc[i - 1 : i + 1].max())
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW04_ZSCORE_SNAPBACK",
    )


# ── PHW05 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW05_RSI2_CHOP_EXTREME")
def phw05_rsi2_chop_extreme(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 25:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    rsi2 = _psh_rsi(c, 2)
    if pd.isna(rsi2.iloc[i]):
        return None
    r = float(rsi2.iloc[i])
    if direction == "LONG":
        if r > 10:
            return None
    else:
        if r < 90:
            return None
    closes = [float(x) for x in c.tolist()]
    er20 = _psh_er_at(closes, i, 20)
    if er20 is None or er20 > 0.25:
        return None
    # Prior 20-day range over [i-20, i-1]
    if i < 20:
        return None
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    ci = float(c.iloc[i])
    if not (prior_lo < ci < prior_hi):
        return None
    sig = _psh_signal(
        direction=direction, stop_price=ci, timeframe="1d",
        strategy_id="PHW05_RSI2_CHOP_EXTREME",
        custom_exit={"type": "indicator_touch", "indicator": "sma", "period": 10},
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PHW06 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW06_EXHAUSTED_DRIFT_REVERSAL")
def phw06_exhausted_drift_reversal(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_20(h, l, i)
    if atr is None or rng is None:
        return None
    hi, lo, _mid, width = rng
    if width <= 0:
        return None
    for j in range(i - 3, i + 1):
        oj, cj = float(o.iloc[j]), float(c.iloc[j])
        if direction == "LONG":
            if not (cj < oj):
                return None
        else:
            if not (cj > oj):
                return None
    cum = abs(float(c.iloc[i]) - float(c.iloc[i - 4]))
    if cum > 1.5 * atr:
        return None
    ci = float(c.iloc[i])
    if direction == "LONG":
        if ci > lo + 0.25 * width:
            return None
        stop = float(l.iloc[i - 3 : i + 1].min()) - 1.0 * atr
    else:
        if ci < hi - 0.25 * width:
            return None
        stop = float(h.iloc[i - 3 : i + 1].max()) + 1.0 * atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW06_EXHAUSTED_DRIFT_REVERSAL",
    )


# ── PHW07 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW07_EQUILIBRIUM_MAGNET")
def phw07_equilibrium_magnet(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 21:
        return None
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_20(h, l, i)
    if atr is None or rng is None:
        return None
    _hi, _lo, mid, _w = rng
    ci = float(c.iloc[i])
    if abs(ci - mid) < 1.2 * atr:
        return None
    if direction == "LONG":
        if not (ci < mid):
            return None
    else:
        if not (ci > mid):
            return None
    macro = _phw_macro_dir(direction)
    for j in (i - 1, i):
        if j < 20:
            return None
        prior_hi = float(h.iloc[j - 20 : j].max())
        prior_lo = float(l.iloc[j - 20 : j].min())
        if macro == "UP" and float(h.iloc[j]) > prior_hi:
            return None
        if macro == "DOWN" and float(l.iloc[j]) < prior_lo:
            return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    swings = f_lo if direction == "LONG" else f_hi
    if not swings:
        return None
    _, ext = swings[-1]
    stop = float(ext)
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW07_EQUILIBRIUM_MAGNET",
        custom_exit={"type": "price_target", "level": float(mid)},
    )


# ── PHW08 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW08_DOUBLE_TAP_SWEEP")
def phw08_double_tap_sweep(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 30:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    swings = f_lo if direction == "LONG" else f_hi
    if len(swings) < 2:
        return None
    pair = None
    for a in range(len(swings) - 1):
        for b in range(a + 1, len(swings)):
            ia, pa = swings[a]
            ib, pb = swings[b]
            if ib - ia < 5:
                continue
            if abs(pa - pb) <= 0.2 * atr:
                pair = (ia, pa, ib, pb)
    if pair is None:
        return None
    _, p1, _, p2 = pair
    level = (p1 + p2) / 2.0
    high_i, low_i, close_i = highs[i], lows[i], float(c.iloc[i])
    if direction == "LONG":
        beyond = level - low_i
        if beyond <= 0 or beyond > 0.5 * atr:
            return None
        if close_i <= level:
            return None
        stop = low_i
    else:
        beyond = high_i - level
        if beyond <= 0 or beyond > 0.5 * atr:
            return None
        if close_i >= level:
            return None
        stop = high_i
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PHW08_DOUBLE_TAP_SWEEP",
    )


# ── PHW09 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW09_ORDER_BLOCK_ROTATION")
def phw09_order_block_rotation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    # Find most recent impulse ending at or before i-1: move >= 1.5*ATR within <= 3 bars
    impulse = None  # (start_j, end_j)
    for end in range(i - 1, max(2, i - 30) - 1, -1):
        for length in (1, 2, 3):
            start = end - length + 1
            if start < 1:
                continue
            move = float(c.iloc[end]) - float(c.iloc[start - 1])
            if direction == "LONG":
                if move >= 1.5 * atr:
                    impulse = (start, end)
                    break
            else:
                if move <= -1.5 * atr:
                    impulse = (start, end)
                    break
        if impulse is not None:
            break
    if impulse is None:
        return None
    start, end = impulse
    # Order block = last opposite-colored candle strictly before impulse start
    block_i = None
    for j in range(start - 1, -1, -1):
        oj, cj = float(o.iloc[j]), float(c.iloc[j])
        if direction == "LONG":
            if cj < oj:  # bearish
                block_i = j
                break
        else:
            if cj > oj:  # bullish
                block_i = j
                break
    if block_i is None:
        return None
    zone_lo = float(l.iloc[block_i])
    zone_hi = float(h.iloc[block_i])
    hi_i, li, ci = float(h.iloc[i]), float(l.iloc[i]), float(c.iloc[i])
    if not (li <= zone_hi and hi_i >= zone_lo):
        return None
    if direction == "LONG":
        if ci < zone_lo:
            return None
        stop = zone_lo
    else:
        if ci > zone_hi:
            return None
        stop = zone_hi
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW09_ORDER_BLOCK_ROTATION",
    )


# ── PHW10 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW10_HEADWIND_DECAY_RIDE")
def phw10_headwind_decay_ride(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    hist = [x for x in ctx.rate_component_history() if x is not None]
    if len(hist) < 6:
        return None
    older, newer = float(hist[-6]), float(hist[-1])
    if older == 0 or (abs(older) - abs(newer)) < 0.08:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 15:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    ema10 = _psh_ema(c, 10)
    if pd.isna(ema10.iloc[i]) or pd.isna(ema10.iloc[i - 1]):
        return None
    ci, cprev = float(c.iloc[i]), float(c.iloc[i - 1])
    ei, eprev = float(ema10.iloc[i]), float(ema10.iloc[i - 1])
    if direction == "LONG":
        if not (ci > ei and cprev <= eprev):
            return None
    else:
        if not (ci < ei and cprev >= eprev):
            return None
    sig = _psh_signal(
        direction=direction, stop_price=ci, timeframe="1d",
        strategy_id="PHW10_HEADWIND_DECAY_RIDE",
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PHW11 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW11_ER_FLOOR_FADE")
def phw11_er_floor_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 25:
        return None
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_20(h, l, i)
    if atr is None or rng is None:
        return None
    hi, lo, _mid, width = rng
    if width <= 0:
        return None
    closes = [float(x) for x in c.tolist()]
    for j in range(i - 4, i + 1):
        erj = _psh_er_at(closes, j, 20)
        if erj is None or erj > 0.12:
            return None
    ci = float(c.iloc[i])
    oi = float(o.iloc[i])
    if direction == "LONG":
        if ci > lo + 0.15 * width:
            return None
        if not (ci > oi):
            return None
        stop = lo - 0.75 * atr
    else:
        if ci < hi - 0.15 * width:
            return None
        if not (ci < oi):
            return None
        stop = hi + 0.75 * atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW11_ER_FLOOR_FADE",
    )


# ── PHW12 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW12_CONFIDENCE_SLIP_ENTRY")
def phw12_confidence_slip_entry(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    conf = [float(x) for x in ctx.confidence_history() if x is not None]
    if len(conf) < 2 or conf[-1] > 0.50:
        return None
    start = max(0, len(conf) - 8)
    if not any(conf[j] >= 0.70 for j in range(start, len(conf) - 1)):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    ema10 = _psh_ema(c, 10)
    if pd.isna(ema10.iloc[i]):
        return None
    ci, ei = float(c.iloc[i]), float(ema10.iloc[i])
    if direction == "LONG":
        if not (ci > ei):
            return None
    else:
        if not (ci < ei):
            return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    # Trade-side fractal for stop: lows for LONG / highs for SHORT (PSH10 geometry).
    swings = f_lo if direction == "LONG" else f_hi
    if not swings:
        return None
    _, ext = swings[-1]
    stop = float(ext) - atr if direction == "LONG" else float(ext) + atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW12_CONFIDENCE_SLIP_ENTRY",
    )


# ── PHW13 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW13_WEEKLY_RANGE_ROTATION")
def phw13_weekly_range_rotation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if ctx.day_of_week != 4:  # Friday
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    weekly = _psh_build_weekly_from_daily(ctx.ohlc())
    if weekly is None or len(weekly) < 9:
        return None
    w_h = pd.to_numeric(weekly["High"], errors="coerce")
    w_l = pd.to_numeric(weekly["Low"], errors="coerce")
    w_c = pd.to_numeric(weekly["Close"], errors="coerce")
    wi = len(w_c) - 1
    # 8-week range over [wi-8, wi-1]
    r_hi = float(w_h.iloc[wi - 8 : wi].max())
    r_lo = float(w_l.iloc[wi - 8 : wi].min())
    width = r_hi - r_lo
    if width <= 0:
        return None
    c_prev = float(w_c.iloc[wi - 1])
    c_conf = float(w_c.iloc[wi])
    if direction == "LONG":
        if c_prev > r_lo + width / 3.0:
            return None
        if not (c_conf > float(w_h.iloc[wi - 1])):
            return None
        stop = r_lo
    else:
        if c_prev < r_hi - width / 3.0:
            return None
        if not (c_conf < float(w_l.iloc[wi - 1])):
            return None
        stop = r_hi
    atr = None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is not None:
        _o, h, l, c = arr
        atr = _phw_atr_at(h, l, c, len(c) - 1)
    if atr is None:
        return None
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW13_WEEKLY_RANGE_ROTATION",
    )


# ── PHW14 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW14_MIDLINE_RECLAIM_HOLD")
def phw14_midline_reclaim_hold(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 55:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    sma50 = _psh_sma(c, 50)
    if pd.isna(sma50.iloc[i]) or i < 1 or pd.isna(sma50.iloc[i - 1]):
        return None
    # Find reclaim bar r in [i-10, i-1]
    r_found = None
    for r in range(i - 1, max(50, i - 10) - 1, -1):
        if pd.isna(sma50.iloc[r]) or pd.isna(sma50.iloc[r - 1]):
            continue
        cr, sr = float(c.iloc[r]), float(sma50.iloc[r])
        cprev, sprev = float(c.iloc[r - 1]), float(sma50.iloc[r - 1])
        if direction == "LONG":
            if cr > sr and cprev <= sprev:
                # all closes in (r, i-1] stayed beyond SMA50
                ok = True
                for j in range(r + 1, i):
                    if pd.isna(sma50.iloc[j]) or float(c.iloc[j]) <= float(sma50.iloc[j]):
                        ok = False
                        break
                if ok:
                    r_found = r
                    break
        else:
            if cr < sr and cprev >= sprev:
                ok = True
                for j in range(r + 1, i):
                    if pd.isna(sma50.iloc[j]) or float(c.iloc[j]) >= float(sma50.iloc[j]):
                        ok = False
                        break
                if ok:
                    r_found = r
                    break
    if r_found is None:
        return None
    # Today is FIRST touch of SMA50 since r
    si = float(sma50.iloc[i])
    hi_i, li, ci = float(h.iloc[i]), float(l.iloc[i]), float(c.iloc[i])
    if direction == "LONG":
        if not (li <= si):
            return None
        if ci < si:
            return None
        # ensure no earlier touch in (r, i)
        for j in range(r_found + 1, i):
            if float(l.iloc[j]) <= float(sma50.iloc[j]):
                return None
        stop = li - 1.0 * atr
    else:
        if not (hi_i >= si):
            return None
        if ci > si:
            return None
        for j in range(r_found + 1, i):
            if float(h.iloc[j]) >= float(sma50.iloc[j]):
                return None
        stop = hi_i + 1.0 * atr
    return _psh_signal(
        direction=direction, stop_price=stop, timeframe="1d",
        strategy_id="PHW14_MIDLINE_RECLAIM_HOLD",
    )


# ── PHW15 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PHW15_MIDWEEK_REVERSION_HARVEST")
def phw15_midweek_reversion_harvest(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    """
    Tuesday signal; entry Wednesday open.
    custom_exit max_sessions=3: Wed(1) Thu(2) Fri(3) → Friday close.
    """
    if ctx.timeframe != "1d":
        return None
    if ctx.day_of_week != 1:  # Tuesday
        return None
    direction = _phw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 5:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    idx = ctx.ohlc().index
    # Most recent Monday must be bar i-1
    try:
        d_prev = pd.Timestamp(idx[i - 1]).date()
    except Exception:  # noqa: BLE001
        return None
    if d_prev.weekday() != 0:
        return None
    m = i - 1
    if m < 1:
        return None
    # Cumulative Mon+Tue move from prior Friday close (close[m-1])
    move = float(c.iloc[i]) - float(c.iloc[m - 1])
    macro = _phw_macro_dir(direction)
    if macro == "UP":
        if move < 1.2 * atr:
            return None
    else:
        if move > -1.2 * atr:
            return None
    sig = _psh_signal(
        direction=direction, stop_price=float(c.iloc[i]), timeframe="1d",
        strategy_id="PHW15_MIDWEEK_REVERSION_HARVEST",
        custom_exit={"type": "time_exit", "max_sessions": 3},
    )
    sig["stop_atr_mult"] = 1.0
    return sig


# ── PART PNT — NEUTRAL-regime shadow family (15 strategies) ───────────────────

_PNT04_PARTNERS = {
    "EURUSD": "GBPUSD", "GBPUSD": "EURUSD",
    "AUDUSD": "NZDUSD", "NZDUSD": "AUDUSD",
    "EURJPY": "GBPJPY", "GBPJPY": "EURJPY",
    "AUDJPY": "NZDJPY", "NZDJPY": "AUDJPY",
}

_PNT05_TRIANGLES = {
    "EURGBP": ("EURUSD", "GBPUSD", "ratio"),
    "EURAUD": ("EURUSD", "AUDUSD", "ratio"),
    "GBPAUD": ("GBPUSD", "AUDUSD", "ratio"),
    "EURJPY": ("EURUSD", "USDJPY", "product"),
    "GBPJPY": ("GBPUSD", "USDJPY", "product"),
    "AUDJPY": ("AUDUSD", "USDJPY", "product"),
    "NZDJPY": ("NZDUSD", "USDJPY", "product"),
    "CADJPY": ("USDJPY", "USDCAD", "quotient"),
}


def _pnt_gate(ctx: ShadowStrategyContext) -> bool:
    """True only when both LONG and SHORT map to NEUTRAL for the pair state."""
    from continuous_backtester import _shadow_bias_from_state

    return (
        _shadow_bias_from_state(ctx.regime_state, "LONG") == "NEUTRAL"
        and _shadow_bias_from_state(ctx.regime_state, "SHORT") == "NEUTRAL"
    )


def _pnt_partner_score_neutral(ticker: str) -> bool:
    """True if partner's latest raw score is inside the mild band (|s| < 0.25)."""
    from regime_engine import get_score_history

    hist = get_score_history(ticker)
    val = None
    for x in reversed(hist):
        if x is not None:
            val = float(x)
            break
    if val is None:
        return False
    return abs(val) < 0.25


def _pnt_normalize_close_index(s: pd.Series) -> pd.Series:
    out = pd.to_numeric(s, errors="coerce").copy()
    try:
        out.index = pd.to_datetime(out.index).normalize()
    except Exception:  # noqa: BLE001
        return out
    # Keep last value if duplicate calendar dates appear.
    out = out[~out.index.duplicated(keep="last")]
    return out


def _pnt_aligned_closes(
    ctx: ShadowStrategyContext, partner: str, min_rows: int,
) -> tuple[pd.Series, pd.Series] | None:
    a = ctx.ohlc()
    if a is None or a.empty or "Close" not in a.columns:
        return None
    b = ctx.get_pair_history(partner)
    if b is None or b.empty or "Close" not in b.columns:
        return None
    ca = _pnt_normalize_close_index(a["Close"])
    cb = _pnt_normalize_close_index(b["Close"])
    joined = pd.concat([ca.rename("a"), cb.rename("b")], axis=1, join="inner")
    joined = joined.dropna().sort_index()
    if len(joined) < int(min_rows):
        return None
    return joined["a"], joined["b"]


def _pnt_r20(closes: pd.Series) -> pd.Series:
    prev = closes.shift(20)
    return (closes - prev) / prev


def _pnt_price_target_sane(direction: str, level: float, close: float) -> bool:
    if direction == "SHORT":
        return level < close
    if direction == "LONG":
        return level > close
    return False


# ── PNT01 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT01_DUAL_BOUNDARY_ROTATION")
def pnt01_dual_boundary_rotation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    rng = _phw_range_prior_20(h, l, i)
    if atr is None or rng is None:
        return None
    hi, lo, _mid, width = rng
    if width < 2.5 * atr:
        return None
    # Prefer LONG if both sides somehow pass.
    long_ok = False
    short_ok = False
    long_stop = short_stop = None
    touches_lo = _phw_boundary_touches(
        h, l, c, i, lo, "lower", atr, lookback=50, tol_mult=0.2,
    )
    if touches_lo != -1 and touches_lo >= 3:
        if float(l.iloc[i]) < lo and float(c.iloc[i]) >= lo + 0.3 * atr:
            long_ok = True
            long_stop = lo - 0.75 * atr
    touches_hi = _phw_boundary_touches(
        h, l, c, i, hi, "upper", atr, lookback=50, tol_mult=0.2,
    )
    if touches_hi != -1 and touches_hi >= 3:
        if float(h.iloc[i]) > hi and float(c.iloc[i]) <= hi - 0.3 * atr:
            short_ok = True
            short_stop = hi + 0.75 * atr
    if long_ok and long_stop is not None:
        return _psh_signal(
            direction="LONG", stop_price=float(long_stop), timeframe="1d",
            strategy_id="PNT01_DUAL_BOUNDARY_ROTATION",
        )
    if short_ok and short_stop is not None:
        return _psh_signal(
            direction="SHORT", stop_price=float(short_stop), timeframe="1d",
            strategy_id="PNT01_DUAL_BOUNDARY_ROTATION",
        )
    return None


# ── PNT02 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT02_UNDERSHOOT_FAILURE_SWING")
def pnt02_undershoot_failure_swing(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    if prior_hi != prior_hi or prior_lo != prior_lo:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    close_i = float(c.iloc[i])
    long_sig = None
    short_sig = None
    # SHORT: failure at upper extreme
    for idx_f in range(len(f_hi) - 1, -1, -1):
        fi, fp = f_hi[idx_f]
        if not (prior_hi - 1.0 * atr <= fp <= prior_hi - 0.3 * atr):
            continue
        P = None
        for lj, lp in reversed(f_lo):
            if lj < fi:
                P = (lj, lp)
                break
        if P is None:
            continue
        mid = (fp + P[1]) / 2.0
        if close_i < mid:
            short_sig = _psh_signal(
                direction="SHORT", stop_price=float(fp), timeframe="1d",
                strategy_id="PNT02_UNDERSHOOT_FAILURE_SWING",
            )
        break
    # LONG: mirror at lower extreme
    for idx_f in range(len(f_lo) - 1, -1, -1):
        fi, fp = f_lo[idx_f]
        if not (prior_lo + 0.3 * atr <= fp <= prior_lo + 1.0 * atr):
            continue
        P = None
        for hj, hp in reversed(f_hi):
            if hj < fi:
                P = (hj, hp)
                break
        if P is None:
            continue
        mid = (fp + P[1]) / 2.0
        if close_i > mid:
            long_sig = _psh_signal(
                direction="LONG", stop_price=float(fp), timeframe="1d",
                strategy_id="PNT02_UNDERSHOOT_FAILURE_SWING",
            )
        break
    if long_sig is not None:
        return long_sig
    return short_sig


# ── PNT03 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT03_COMPRESSION_BOX_STRADDLE")
def pnt03_compression_box_straddle(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    # Need enough bars for BB(20) + 60-width window ending at i-2
    if i < 20 + 59:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    upper, _mid, lower = _psh_bbands(c, 20, 2.0)
    width = upper - lower
    for j in (i - 2, i - 1, i):
        if j < 59:
            return None
        wj = float(width.iloc[j]) if pd.notna(width.iloc[j]) else None
        if wj is None:
            return None
        window = width.iloc[j - 59 : j + 1]
        if window.isna().any() or len(window) < 60:
            return None
        wmin = float(window.min())
        if wj > wmin + 1e-12:
            return None
    if i < 10:
        return None
    box_hi = float(h.iloc[i - 10 : i].max())
    box_lo = float(l.iloc[i - 10 : i].min())
    if box_hi != box_hi or box_lo != box_lo:
        return None
    close_i = float(c.iloc[i])
    if close_i > box_hi:
        return _psh_signal(
            direction="LONG", stop_price=float(box_lo), timeframe="1d",
            strategy_id="PNT03_COMPRESSION_BOX_STRADDLE",
        )
    if close_i < box_lo:
        return _psh_signal(
            direction="SHORT", stop_price=float(box_hi), timeframe="1d",
            strategy_id="PNT03_COMPRESSION_BOX_STRADDLE",
        )
    return None


# ── PNT04 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT04_CORRELATED_PAIR_DIVERGENCE")
def pnt04_correlated_pair_divergence(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    partner = _PNT04_PARTNERS.get(str(ctx.ticker).upper())
    if partner is None:
        return None
    if not _pnt_partner_score_neutral(partner):
        return None
    pair_data = _pnt_aligned_closes(ctx, partner, min_rows=81)
    if pair_data is None:
        return None
    traded, partner_c = pair_data
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    ra = _pnt_r20(traded)
    rb = _pnt_r20(partner_c)
    spread = ra - rb
    sp = spread.dropna()
    if len(sp) < 60:
        return None
    window = sp.iloc[-60:]
    mu = float(window.mean())
    sigma = float(window.std(ddof=0))
    if sigma <= 0 or sigma != sigma or mu != mu:
        return None
    # Pearson corr of last 60 aligned 1-day pct returns
    r1a = traded.pct_change()
    r1b = partner_c.pct_change()
    rjoin = pd.concat([r1a.rename("a"), r1b.rename("b")], axis=1).dropna()
    if len(rjoin) < 60:
        return None
    rtail = rjoin.iloc[-60:]
    corr = float(rtail["a"].corr(rtail["b"]))
    if corr != corr or corr < 0.70:
        return None
    spread_now = float(sp.iloc[-1])
    z = (spread_now - mu) / sigma
    if abs(z) < 2.0:
        return None
    direction = "SHORT" if z > 0 else "LONG"
    sign_z = 1.0 if z > 0 else -1.0
    last_dt = sp.index[-1]
    try:
        rb_now = float(rb.loc[last_dt])
    except Exception:  # noqa: BLE001
        return None
    if rb_now != rb_now:
        return None
    spread_star = mu + 0.5 * sigma * sign_z
    r20_star = spread_star + rb_now
    # Anchor = traded aligned close 20 rows back (R20 denominator).
    try:
        pos = traded.index.get_loc(last_dt)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(pos, slice):
        return None
    if not isinstance(pos, (int,)):
        try:
            pos = int(pos[-1])  # type: ignore[index]
        except Exception:  # noqa: BLE001
            return None
    pos = int(pos)
    if pos < 20:
        return None
    anchor = float(traded.iloc[pos - 20])
    if anchor != anchor or anchor == 0:
        return None
    level = anchor * (1.0 + r20_star)
    close_now = float(traded.iloc[pos])
    if not _pnt_price_target_sane(direction, level, close_now):
        return None
    sig = _psh_signal(
        direction=direction,
        stop_price=float(close_now),
        timeframe="1d",
        strategy_id="PNT04_CORRELATED_PAIR_DIVERGENCE",
        custom_exit={"type": "price_target", "level": float(level)},
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PNT05 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT05_TRIANGLE_CONSISTENCY_SNAP")
def pnt05_triangle_consistency_snap(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    tri = _PNT05_TRIANGLES.get(str(ctx.ticker).upper())
    if tri is None:
        return None
    legA, legB, kind = tri
    if not _pnt_partner_score_neutral(legA):
        return None
    if not _pnt_partner_score_neutral(legB):
        return None
    a = _pnt_aligned_closes(ctx, legA, 61)
    b = _pnt_aligned_closes(ctx, legB, 61)
    if a is None or b is None:
        return None
    traded_a, legA_c = a
    traded_b, legB_c = b
    joined = pd.concat(
        [
            traded_a.rename("cross"),
            legA_c.rename("legA"),
            legB_c.rename("legB"),
        ],
        axis=1,
        join="inner",
    ).dropna().sort_index()
    if len(joined) < 61:
        return None
    kind_u = str(kind).strip().lower()
    if kind_u in ("ratio", "quotient"):
        implied = joined["legA"] / joined["legB"]
    elif kind_u == "product":
        implied = joined["legA"] * joined["legB"]
    else:
        return None
    dev = joined["cross"] - implied
    if len(dev) < 61:
        return None
    last60 = dev.iloc[-60:]
    if last60.isna().any():
        return None
    sigma = float(last60.std(ddof=0))
    if sigma <= 0 or sigma != sigma:
        return None
    # sigma from last 60; abs(dev_now) vs that sigma — prompt: std of last 60
    # (does not include requiring 61 for sigma window beyond the series length)
    # Use last60 as specified.
    # Actually re-read: "sigma = std (ddof=0) of the last 60 dev values"
    # and we have >= 61 rows so current + 60 prior exists; last 60 of full series
    # includes current. That's fine / matches "last 60".
    dev_now = float(dev.iloc[-1])
    if abs(dev_now) < 1.5 * sigma:
        return None
    direction = "SHORT" if dev_now > 0 else "LONG"
    sign_d = 1.0 if dev_now > 0 else -1.0
    implied_now = float(implied.iloc[-1])
    level = implied_now + 0.5 * sigma * sign_d
    close_now = float(joined["cross"].iloc[-1])
    if not _pnt_price_target_sane(direction, level, close_now):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    stop_px = close_now - 1.0 * atr if direction == "LONG" else close_now + 1.0 * atr
    sig = _psh_signal(
        direction=direction,
        stop_price=float(stop_px),
        timeframe="1d",
        strategy_id="PNT05_TRIANGLE_CONSISTENCY_SNAP",
        custom_exit={"type": "price_target", "level": float(level)},
    )
    sig["stop_atr_mult"] = 1.0
    return sig


# ── PNT06 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT06_STRENGTH_LADDER_FADE")
def pnt06_strength_ladder_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    rets = ctx.get_universe_returns(10)
    strength: dict[str, float] = {}
    counts: dict[str, int] = {}
    for pair, ret in rets.items():
        p = str(pair).strip().upper()
        if len(p) != 6 or not p.isalpha():
            continue
        try:
            r = float(ret)
        except (TypeError, ValueError):
            continue
        if r != r:
            continue
        base, quote = p[:3], p[3:]
        strength[base] = strength.get(base, 0.0) + r
        counts[base] = counts.get(base, 0) + 1
        strength[quote] = strength.get(quote, 0.0) - r
        counts[quote] = counts.get(quote, 0) + 1
    if len(strength) < 6:
        return None
    avgs = {k: strength[k] / counts[k] for k in strength if counts.get(k, 0) > 0}
    if len(avgs) < 6:
        return None
    vals = list(avgs.values())
    mu = float(sum(vals) / len(vals))
    var = float(sum((v - mu) ** 2 for v in vals) / len(vals))
    sigma = var ** 0.5
    if sigma <= 0:
        return None
    zmap = {k: (avgs[k] - mu) / sigma for k in avgs}
    tku = str(ctx.ticker).strip().upper()
    if len(tku) != 6:
        return None
    base, quote = tku[:3], tku[3:]
    if base not in zmap or quote not in zmap:
        return None
    bz = float(zmap[base])
    qz = float(zmap[quote])
    if abs(bz) >= abs(qz):
        extreme_z = bz
        if abs(extreme_z) < 1.5:
            return None
        direction = "SHORT" if extreme_z > 0 else "LONG"
    else:
        extreme_z = qz
        if abs(extreme_z) < 1.5:
            return None
        direction = "LONG" if extreme_z > 0 else "SHORT"
    open_i = float(o.iloc[i])
    close_i = float(c.iloc[i])
    if direction == "SHORT" and not (close_i < open_i):
        return None
    if direction == "LONG" and not (close_i > open_i):
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    if direction == "LONG":
        if not f_lo:
            return None
        _fi, fp = f_lo[-1]
        stop = float(fp) - 1.25 * atr
    else:
        if not f_hi:
            return None
        _fi, fp = f_hi[-1]
        stop = float(fp) + 1.25 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PNT06_STRENGTH_LADDER_FADE",
    )


# ── PNT07 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT07_DOUBLE_SIDED_SWEEP")
def pnt07_double_sided_sweep(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)

    def _equal_pair(swings: list[tuple[int, float]]) -> float | None:
        best = None  # (end_idx, mean_level)
        for a in range(len(swings) - 1):
            for b in range(a + 1, len(swings)):
                ia, pa = swings[a]
                ib, pb = swings[b]
                if ib - ia < 5:
                    continue
                if abs(pa - pb) <= 0.2 * atr:
                    mean_lvl = (pa + pb) / 2.0
                    if best is None or ib >= best[0]:
                        best = (ib, mean_lvl)
        return None if best is None else float(best[1])

    lower_level = _equal_pair(f_lo)
    upper_level = _equal_pair(f_hi)
    if lower_level is None or upper_level is None:
        return None
    low_i = float(l.iloc[i])
    high_i = float(h.iloc[i])
    close_i = float(c.iloc[i])
    long_ok = False
    short_ok = False
    beyond_lo = lower_level - low_i
    if 0 < beyond_lo <= 0.5 * atr and close_i > lower_level:
        long_ok = True
    beyond_hi = high_i - upper_level
    if 0 < beyond_hi <= 0.5 * atr and close_i < upper_level:
        short_ok = True
    if long_ok and short_ok:
        return None
    if long_ok:
        return _psh_signal(
            direction="LONG", stop_price=float(low_i), timeframe="1d",
            strategy_id="PNT07_DOUBLE_SIDED_SWEEP",
            custom_exit={"type": "price_target", "level": float(upper_level)},
        )
    if short_ok:
        return _psh_signal(
            direction="SHORT", stop_price=float(high_i), timeframe="1d",
            strategy_id="PNT07_DOUBLE_SIDED_SWEEP",
            custom_exit={"type": "price_target", "level": float(lower_level)},
        )
    return None


# ── PNT08 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT08_INSIDE_CLUSTER_TRAP")
def pnt08_inside_cluster_trap(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 23:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    # Maximal consecutive inside days ending at i-2, k in [3, 6]
    k = 0
    j = i - 2
    while j >= 1 and k < 6:
        if _psh_is_inside_day(o, h, l, c, j):
            k += 1
            j -= 1
        else:
            break
    if k < 3:
        return None
    start = i - 2 - k + 1
    cluster_hi = float(h.iloc[start : i - 1].max())  # [start, i-2]
    cluster_lo = float(l.iloc[start : i - 1].min())
    if cluster_hi != cluster_hi or cluster_lo != cluster_lo:
        return None
    # Position range over [i-21, i-2] — excludes break bar (i-1) and today
    prior_hi = float(h.iloc[i - 21 : i - 1].max())
    prior_lo = float(l.iloc[i - 21 : i - 1].min())
    if prior_hi != prior_hi or prior_lo != prior_lo:
        return None
    span = prior_hi - prior_lo
    if span <= 0:
        return None
    mid_c = (cluster_hi + cluster_lo) / 2.0
    third = span / 3.0
    if not (prior_lo + third <= mid_c <= prior_hi - third):
        return None
    close_break = float(c.iloc[i - 1])
    break_above = close_break > cluster_hi
    break_below = close_break < cluster_lo
    if break_above == break_below:
        return None
    close_i = float(c.iloc[i])
    if not (cluster_lo <= close_i <= cluster_hi):
        return None
    if break_above:
        direction = "SHORT"
        stop = float(h.iloc[i - 1])
    else:
        direction = "LONG"
        stop = float(l.iloc[i - 1])
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PNT08_INSIDE_CLUSTER_TRAP",
    )


# ── PNT09 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT09_EQUILIBRIUM_CRACK")
def pnt09_equilibrium_crack(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    hist = [float(x) for x in ctx.score_history() if x is not None]
    if len(hist) < 11:
        return None
    s0 = hist[-11]
    s_now = hist[-1]
    if abs(s0) > 0.05:
        return None
    if abs(s_now) - abs(s0) < 0.12:
        return None
    if s_now == 0:
        return None
    sign_now = 1.0 if s_now > 0 else -1.0
    for v in hist[-11:]:
        if v == 0:
            continue
        if (1.0 if v > 0 else -1.0) != sign_now:
            return None
    direction = "LONG" if s_now > 0 else "SHORT"
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    close_i = float(c.iloc[i])
    if direction == "LONG":
        if close_i <= prior_hi:
            return None
        stop_px = close_i - 1.25 * atr
    else:
        if close_i >= prior_lo:
            return None
        stop_px = close_i + 1.25 * atr
    sig = _psh_signal(
        direction=direction, stop_price=float(stop_px), timeframe="1d",
        strategy_id="PNT09_EQUILIBRIUM_CRACK",
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PNT10 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT10_ER_AWAKENING")
def pnt10_er_awakening(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    eh = [float(x) for x in ctx.er_history() if x is not None]
    if len(eh) < 11:
        return None
    er_now = eh[-1]
    if er_now < 0.35:
        return None
    if min(eh[-11:-1]) > 0.15:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    close_i = float(c.iloc[i])
    close_prev20 = float(c.iloc[i - 20])
    direction = "LONG" if close_i > close_prev20 else "SHORT"
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    if direction == "LONG":
        if close_i <= prior_hi:
            return None
        stop_px = close_i - 1.25 * atr
    else:
        if close_i >= prior_lo:
            return None
        stop_px = close_i + 1.25 * atr
    sig = _psh_signal(
        direction=direction, stop_price=float(stop_px), timeframe="1d",
        strategy_id="PNT10_ER_AWAKENING",
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PNT11 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT11_DWELL_MATURITY_BREAK")
def pnt11_dwell_maturity_break(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    if int(ctx.regime_days_in_state) < 60:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 30:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    atr_s = _psh_atr_series(h, l, c, 14)
    atr_now = float(atr_s.iloc[i]) if pd.notna(atr_s.iloc[i]) else None
    if atr_now is None:
        return None
    window = atr_s.iloc[max(0, i - 59) : i + 1].dropna()
    if len(window) < 60:
        return None
    if atr_now > float(window.min()) + 1e-12:
        return None
    prior_hi = float(h.iloc[i - 30 : i].max())
    prior_lo = float(l.iloc[i - 30 : i].min())
    close_i = float(c.iloc[i])
    if close_i > prior_hi:
        direction = "LONG"
        stop = float(l.iloc[i]) - 1.0 * atr
    elif close_i < prior_lo:
        direction = "SHORT"
        stop = float(h.iloc[i]) + 1.0 * atr
    else:
        return None
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PNT11_DWELL_MATURITY_BREAK",
    )


# ── PNT12 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT12_AUTOCORR_FADE")
def pnt12_autocorr_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 22:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    o1, c1 = float(o.iloc[i - 1]), float(c.iloc[i - 1])
    o0, c0 = float(o.iloc[i]), float(c.iloc[i])
    up1 = c1 > o1
    dn1 = c1 < o1
    up0 = c0 > o0
    dn0 = c0 < o0
    if not ((up1 and up0) or (dn1 and dn0)):
        return None
    if abs(c0 - float(c.iloc[i - 2])) < 1.2 * atr:
        return None
    prior_hi = float(h.iloc[i - 22 : i - 1].max())  # [i-22, i-2]
    prior_lo = float(l.iloc[i - 22 : i - 1].min())
    hi_pair = max(float(h.iloc[i - 1]), float(h.iloc[i]))
    lo_pair = min(float(l.iloc[i - 1]), float(l.iloc[i]))
    if hi_pair > prior_hi or lo_pair < prior_lo:
        return None
    direction = "SHORT" if (up1 and up0) else "LONG"
    stop_px = c0 - 1.0 * atr if direction == "LONG" else c0 + 1.0 * atr
    sig = _psh_signal(
        direction=direction,
        stop_price=float(stop_px),
        timeframe="1d",
        strategy_id="PNT12_AUTOCORR_FADE",
        custom_exit={"type": "time_exit", "max_sessions": 3},
    )
    sig["stop_atr_mult"] = 1.0
    return sig


# ── PNT13 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT13_GAP_FILL_HARVEST")
def pnt13_gap_fill_harvest(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 21:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    open_i = float(o.iloc[i])
    close_prev = float(c.iloc[i - 1])
    if abs(open_i - close_prev) < 0.25 * atr:
        return None
    prior_hi = float(h.iloc[i - 21 : i].max())  # [i-21, i-1]
    prior_lo = float(l.iloc[i - 21 : i].min())
    if not (prior_lo < open_i < prior_hi):
        return None
    low_i = float(l.iloc[i])
    high_i = float(h.iloc[i])
    gap_up = open_i > close_prev
    gap_down = open_i < close_prev
    if gap_up:
        if low_i <= close_prev:
            return None
        direction = "SHORT"
        stop = high_i + 1.0 * atr
    elif gap_down:
        if high_i >= close_prev:
            return None
        direction = "LONG"
        stop = low_i - 1.0 * atr
    else:
        return None
    return _psh_signal(
        direction=direction,
        stop_price=float(stop),
        timeframe="1d",
        strategy_id="PNT13_GAP_FILL_HARVEST",
        custom_exit={"type": "price_target", "level": float(close_prev)},
    )


# ── PNT14 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT14_MONTH_OPEN_REVERSION")
def pnt14_month_open_reversion(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    try:
        today = date.fromisoformat(ctx.session_date)
    except ValueError:
        return None
    tdays = _psh_weekday_trading_days(today.year, today.month)
    if not tdays or today != tdays[-1]:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 2:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    move = float(c.iloc[i]) - float(c.iloc[i - 2])
    if abs(move) < 1.2 * atr:
        return None
    direction = "SHORT" if move > 0 else "LONG"
    close_i = float(c.iloc[i])
    stop_px = close_i - 1.0 * atr if direction == "LONG" else close_i + 1.0 * atr
    sig = _psh_signal(
        direction=direction,
        stop_price=float(stop_px),
        timeframe="1d",
        strategy_id="PNT14_MONTH_OPEN_REVERSION",
        custom_exit={"type": "time_exit", "max_sessions": 5},
    )
    sig["stop_atr_mult"] = 1.0
    return sig


# ── PNT15 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PNT15_WEEKLY_COMPRESSION_ROTATION")
def pnt15_weekly_compression_rotation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    if not _pnt_gate(ctx):
        return None
    if ctx.day_of_week != 4:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    weekly = _psh_build_weekly_from_daily(ctx.ohlc())
    if weekly is None or len(weekly) < 19:
        return None
    wi = len(weekly) - 1  # last completed week (W-FRI includes today on Friday)
    # Compression: for each k in [wi-3, wi]: range_k < mean(ranges [k-10, k-1])
    wh = weekly["High"]
    wl = weekly["Low"]
    wc = weekly["Close"]
    for k in range(wi - 3, wi + 1):
        if k < 10:
            return None
        rk = float(wh.iloc[k]) - float(wl.iloc[k])
        prior = [(float(wh.iloc[j]) - float(wl.iloc[j])) for j in range(k - 10, k)]
        if len(prior) < 10:
            return None
        mean_prior = sum(prior) / 10.0
        if rk >= mean_prior:
            return None
    if wi < 7:
        return None
    r_hi = float(wh.iloc[wi - 7 : wi + 1].max())
    r_lo = float(wl.iloc[wi - 7 : wi + 1].min())
    mid = (r_hi + r_lo) / 2.0
    span = r_hi - r_lo
    if span <= 0:
        return None
    close_w = float(wc.iloc[wi])
    if close_w <= r_lo + span / 3.0:
        direction = "LONG"
        stop = r_lo
    elif close_w >= r_hi - span / 3.0:
        direction = "SHORT"
        stop = r_hi
    else:
        return None
    return _psh_signal(
        direction=direction,
        stop_price=float(stop),
        timeframe="1d",
        strategy_id="PNT15_WEEKLY_COMPRESSION_ROTATION",
        custom_exit={"type": "price_target", "level": float(mid)},
    )



# ── PART PTW — TAILWIND-regime shadow family (15 strategies) ──────────────────

def _ptw_gate(ctx: ShadowStrategyContext) -> str | None:
    """Return the unique trade direction that maps to TAILWIND, or None."""
    from continuous_backtester import _shadow_bias_from_state

    for direction in ("LONG", "SHORT"):
        if _shadow_bias_from_state(ctx.regime_state, direction) == "TAILWIND":
            return direction
    return None


def _ptw_entry_idx(ctx: ShadowStrategyContext, i: int) -> int:
    days = int(ctx.regime_days_in_state or 0)
    return max(0, i - max(days - 1, 0))


def _ptw_regime_extreme(
    h: pd.Series, l: pd.Series, i: int, entry_idx: int, direction: str,
) -> tuple[float, int] | None:
    """Regime extreme E and LAST index attaining it over [entry_idx, i]."""
    if entry_idx < 0 or i < entry_idx:
        return None
    try:
        if direction == "LONG":
            E = float(h.iloc[entry_idx : i + 1].max())
            if E != E:
                return None
            e_idx = None
            for j in range(i, entry_idx - 1, -1):
                v = float(h.iloc[j])
                if v == v and abs(v - E) <= 1e-12:
                    e_idx = j
                    break
            if e_idx is None:
                return None
            return E, e_idx
        E = float(l.iloc[entry_idx : i + 1].min())
        if E != E:
            return None
        e_idx = None
        for j in range(i, entry_idx - 1, -1):
            v = float(l.iloc[j])
            if v == v and abs(v - E) <= 1e-12:
                e_idx = j
                break
        if e_idx is None:
            return None
        return E, e_idx
    except Exception:  # noqa: BLE001
        return None


def _ptw_pullback(
    h: pd.Series,
    l: pd.Series,
    c: pd.Series,
    i: int,
    atr: float,
    direction: str,
    entry_idx: int,
    min_depth_mult: float,
) -> tuple[float, int, float, float] | None:
    """
    Pullback after regime extreme.
    LONG: E=max High, P=min Low after e_idx, depth=E-P.
    SHORT: E=min Low, P=max High after e_idx, depth=P-E.
    Returns (E, e_idx, P, mid) or None.
    """
    _ = c
    ext = _ptw_regime_extreme(h, l, i, entry_idx, direction)
    if ext is None:
        return None
    E, e_idx = ext
    if e_idx >= i:
        return None
    try:
        if direction == "LONG":
            P = float(l.iloc[e_idx + 1 : i + 1].min())
            if P != P:
                return None
            depth = E - P
        else:
            P = float(h.iloc[e_idx + 1 : i + 1].max())
            if P != P:
                return None
            depth = P - E
    except Exception:  # noqa: BLE001
        return None
    if depth < float(min_depth_mult) * atr:
        return None
    return E, e_idx, P, (E + P) / 2.0


def _ptw_pullback_resumption(
    ctx: ShadowStrategyContext,
    direction: str,
    h: pd.Series,
    l: pd.Series,
    c: pd.Series,
    i: int,
    atr: float,
    entry_idx: int,
) -> tuple[float, float] | None:
    """Shared PTW01-style pullback + fractal hold + midpoint resumption. Returns (E, P)."""
    pb = _ptw_pullback(h, l, c, i, atr, direction, entry_idx, min_depth_mult=1.0)
    if pb is None:
        return None
    E, e_idx, P, mid = pb
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    if direction == "LONG":
        prior = None
        for fi, fp in reversed(f_lo):
            if fi < e_idx:
                prior = float(fp)
                break
        if prior is None or not (P > prior):
            return None
    else:
        prior = None
        for fi, fp in reversed(f_hi):
            if fi < e_idx:
                prior = float(fp)
                break
        if prior is None or not (P < prior):
            return None
    # Open from context OHLC (same bar index as closed-candle series).
    ohlc = ctx.ohlc()
    try:
        open_i = float(pd.to_numeric(ohlc["Open"], errors="coerce").iloc[i])
    except Exception:  # noqa: BLE001
        return None
    if open_i != open_i:
        return None
    close_i = float(c.iloc[i])
    if direction == "LONG":
        if not (close_i > open_i and close_i > mid):
            return None
    else:
        if not (close_i < open_i and close_i < mid):
            return None
    return E, P


# ── PTW01 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW01_PRIME_WINDOW_PULLBACK")
def ptw01_prime_window_pullback(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    dwell = int(ctx.regime_days_in_state)
    if dwell < 10 or dwell > 30:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    entry_idx = _ptw_entry_idx(ctx, i)
    res = _ptw_pullback_resumption(ctx, direction, h, l, c, i, atr, entry_idx)
    if res is None:
        return None
    _E, P = res
    stop = P - 0.75 * atr if direction == "LONG" else P + 0.75 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW01_PRIME_WINDOW_PULLBACK",
    )


# ── PTW02 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW02_CONFIRMED_BIRTH_BREAK")
def ptw02_confirmed_birth_break(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    dwell = int(ctx.regime_days_in_state)
    if dwell < 10 or dwell > 20:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    close_i = float(c.iloc[i])
    if direction == "LONG":
        if close_i <= prior_hi:
            return None
    else:
        if close_i >= prior_lo:
            return None
    sig = _psh_signal(
        direction=direction, stop_price=float(close_i), timeframe="1d",
        strategy_id="PTW02_CONFIRMED_BIRTH_BREAK",
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PTW03 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW03_LATE_CYCLE_TIGHTENER")
def ptw03_late_cycle_tightener(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    dwell = int(ctx.regime_days_in_state)
    if dwell < 30 or dwell > 50:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    entry_idx = _ptw_entry_idx(ctx, i)
    res = _ptw_pullback_resumption(ctx, direction, h, l, c, i, atr, entry_idx)
    if res is None:
        return None
    _E, P = res
    stop = P - 0.40 * atr if direction == "LONG" else P + 0.40 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW03_LATE_CYCLE_TIGHTENER",
    )


# ── PTW04 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW04_ST_GRADUATION_GUARD")
def ptw04_st_graduation_guard(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    s = ctx.regime_raw_score
    if s is None:
        return None
    s = float(s)
    if abs(s) < 0.25 or abs(s) > 0.50:
        return None
    if direction == "LONG" and not (s > 0):
        return None
    if direction == "SHORT" and not (s < 0):
        return None
    a0 = ctx.atr_at_regime_entry
    if a0 is None or float(a0) <= 0:
        return None
    a0 = float(a0)
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    if atr > 1.15 * a0:
        return None
    entry_idx = _ptw_entry_idx(ctx, i)
    res = _ptw_pullback_resumption(ctx, direction, h, l, c, i, atr, entry_idx)
    if res is None:
        return None
    _E, P = res
    stop = P - 0.75 * atr if direction == "LONG" else P + 0.75 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW04_ST_GRADUATION_GUARD",
    )


# ── PTW05 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW05_HIGHER_LOW_LADDER")
def ptw05_higher_low_ladder(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    entry_idx = _ptw_entry_idx(ctx, i)
    highs = [float(x) for x in h.tolist()]
    lows = [float(x) for x in l.tolist()]
    f_hi, f_lo = _psh_fractal_swings(highs, lows)
    close_i = float(c.iloc[i])
    if direction == "LONG":
        swings = [(fi, fp) for fi, fp in f_lo if fi >= entry_idx]
        if len(swings) < 3:
            return None
        r1, r2, r3 = swings[-3], swings[-2], swings[-1]
        if not (r1[1] < r2[1] < r3[1]):
            return None
        idx_r2, idx_r3 = r1[0], r3[0]
        # interm over (idx_r2, idx_r3] where r2 is middle
        idx_r2 = r2[0]
        if idx_r3 <= idx_r2:
            return None
        interm = float(h.iloc[idx_r2 + 1 : idx_r3 + 1].max())
        if close_i <= interm:
            return None
        stop = float(r3[1]) - 0.25 * atr
    else:
        swings = [(fi, fp) for fi, fp in f_hi if fi >= entry_idx]
        if len(swings) < 3:
            return None
        r1, r2, r3 = swings[-3], swings[-2], swings[-1]
        if not (r1[1] > r2[1] > r3[1]):
            return None
        idx_r2, idx_r3 = r2[0], r3[0]
        if idx_r3 <= idx_r2:
            return None
        interm = float(l.iloc[idx_r2 + 1 : idx_r3 + 1].min())
        if close_i >= interm:
            return None
        stop = float(r3[1]) + 0.25 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW05_HIGHER_LOW_LADDER",
    )


# ── PTW06 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW06_MEASURED_LEG_REPEAT")
def ptw06_measured_leg_repeat(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 21:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    found = None  # (s, e, O, X, A)
    e_lo = max(1, i - 20)
    for e in range(i - 1, e_lo - 1, -1):
        s_lo = max(0, e - 8)
        for s in range(e - 1, s_lo - 1, -1):
            if s < 0 or e <= s:
                continue
            close_e = float(c.iloc[e])
            close_s = float(c.iloc[s])
            if direction == "LONG":
                if not (close_e > close_s):
                    continue
                O = float(l.iloc[s : e + 1].min())
                X = float(h.iloc[s : e + 1].max())
                A = X - O
            else:
                if not (close_e < close_s):
                    continue
                X = float(l.iloc[s : e + 1].min())
                O = float(h.iloc[s : e + 1].max())
                A = O - X
            if A != A or A < 2.0 * atr:
                continue
            found = (s, e, O, X, A)
            break
        if found is not None:
            break
    if found is None:
        return None
    _s, e, O, X, A = found
    if e >= i:
        return None
    close_i = float(c.iloc[i])
    if direction == "LONG":
        P = float(l.iloc[e + 1 : i + 1].min())
        if P != P or not (P > O) or (X - P) > 0.5 * A:
            return None
        if close_i <= X:
            return None
        stop = P
        level = P + A
    else:
        P = float(h.iloc[e + 1 : i + 1].max())
        if P != P or not (P < O) or (P - X) > 0.5 * A:
            return None
        if close_i >= X:
            return None
        stop = P
        level = P - A
    return _psh_signal(
        direction=direction,
        stop_price=float(stop),
        timeframe="1d",
        strategy_id="PTW06_MEASURED_LEG_REPEAT",
        custom_exit={"type": "price_target", "level": float(level)},
    )


# ── PTW07 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW07_ORDER_BLOCK_CONTINUATION")
def ptw07_order_block_continuation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    # Most recent impulse in TREND direction ending at or before i-1
    impulse = None
    for end in range(i - 1, max(2, i - 30) - 1, -1):
        for length in (1, 2, 3):
            start = end - length + 1
            if start < 1:
                continue
            move = float(c.iloc[end]) - float(c.iloc[start - 1])
            if direction == "LONG":
                if move >= 1.5 * atr:
                    impulse = (start, end)
                    break
            else:
                if move <= -1.5 * atr:
                    impulse = (start, end)
                    break
        if impulse is not None:
            break
    if impulse is None:
        return None
    start, _end = impulse
    block_i = None
    for j in range(start - 1, -1, -1):
        oj, cj = float(o.iloc[j]), float(c.iloc[j])
        if direction == "LONG":
            if cj < oj:  # opposite = bearish
                block_i = j
                break
        else:
            if cj > oj:  # opposite = bullish
                block_i = j
                break
    if block_i is None:
        return None
    zone_lo = float(l.iloc[block_i])
    zone_hi = float(h.iloc[block_i])
    hi_i, li, ci = float(h.iloc[i]), float(l.iloc[i]), float(c.iloc[i])
    if not (li <= zone_hi and hi_i >= zone_lo):
        return None
    if direction == "LONG":
        if ci < zone_lo:
            return None
        stop = zone_lo
    else:
        if ci > zone_hi:
            return None
        stop = zone_hi
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW07_ORDER_BLOCK_CONTINUATION",
    )


# ── PTW08 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW08_WEEKLY_DAILY_SYNC")
def ptw08_weekly_daily_sync(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    weekly = _psh_build_weekly_from_daily(ctx.ohlc())
    if weekly is None or weekly.empty:
        return None
    if ctx.day_of_week != 4:
        weekly = weekly.iloc[:-1]
    if len(weekly) < 10:
        return None
    wi = len(weekly) - 1
    if wi < 8:
        return None
    wh = weekly["High"]
    wl = weekly["Low"]
    wc = weekly["Close"]
    close_w = float(wc.iloc[wi])
    if direction == "LONG":
        prior_hi = float(wh.iloc[wi - 8 : wi].max())
        if close_w <= prior_hi:
            return None
    else:
        prior_lo = float(wl.iloc[wi - 8 : wi].min())
        if close_w >= prior_lo:
            return None
    entry_idx = _ptw_entry_idx(ctx, i)
    pb = _ptw_pullback(h, l, c, i, atr, direction, entry_idx, min_depth_mult=0.75)
    if pb is None:
        return None
    _E, _e_idx, P, _mid = pb
    open_i = float(o.iloc[i])
    close_i = float(c.iloc[i])
    if direction == "LONG":
        if not (close_i > open_i):
            return None
        stop = P - 1.0 * atr
    else:
        if not (close_i < open_i):
            return None
        stop = P + 1.0 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW08_WEEKLY_DAILY_SYNC",
    )


# ── PTW09 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW09_SCORE_MOMENTUM_RIDE")
def ptw09_score_momentum_ride(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    hist = [float(x) for x in ctx.score_history() if x is not None]
    if len(hist) < 6:
        return None
    dir_sign = 1.0 if direction == "LONG" else -1.0
    if (hist[-1] - hist[-6]) * dir_sign < 0.08:
        return None
    if abs(hist[-1]) < 0.25 or abs(hist[-1]) > 0.55:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    ema10 = _psh_ema(c, 10)
    if pd.isna(ema10.iloc[i]):
        return None
    close_i = float(c.iloc[i])
    open_i = float(o.iloc[i])
    ei = float(ema10.iloc[i])
    if direction == "LONG":
        if not (close_i > ei and close_i > open_i):
            return None
    else:
        if not (close_i < ei and close_i < open_i):
            return None
    sig = _psh_signal(
        direction=direction, stop_price=float(close_i), timeframe="1d",
        strategy_id="PTW09_SCORE_MOMENTUM_RIDE",
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PTW10 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW10_CONFIDENCE_FLOOR_ENTRY")
def ptw10_confidence_floor_entry(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    conf = [float(x) for x in ctx.confidence_history() if x is not None]
    if len(conf) < 3 or any(v < 0.75 for v in conf[-3:]):
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    entry_idx = _ptw_entry_idx(ctx, i)
    res = _ptw_pullback_resumption(ctx, direction, h, l, c, i, atr, entry_idx)
    if res is None:
        return None
    _E, P = res
    stop = P - 0.75 * atr if direction == "LONG" else P + 0.75 * atr
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW10_CONFIDENCE_FLOOR_ENTRY",
    )


# ── PTW11 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW11_SHALLOW_GRIND_RIDER")
def ptw11_shallow_grind_rider(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 11:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    # Grind window [i-10, i-1]
    start = i - 10
    if direction == "LONG":
        up_count = sum(
            1 for j in range(start, i) if float(c.iloc[j]) > float(o.iloc[j])
        )
        if up_count < 7:
            return None
        peak = None
        max_adv = 0.0
        for j in range(start, i):
            hj = float(h.iloc[j])
            lj = float(l.iloc[j])
            peak = hj if peak is None else max(peak, hj)
            max_adv = max(max_adv, peak - lj)
        if max_adv > 0.75 * atr:
            return None
    else:
        dn_count = sum(
            1 for j in range(start, i) if float(c.iloc[j]) < float(o.iloc[j])
        )
        if dn_count < 7:
            return None
        trough = None
        max_adv = 0.0
        for j in range(start, i):
            hj = float(h.iloc[j])
            lj = float(l.iloc[j])
            trough = lj if trough is None else min(trough, lj)
            max_adv = max(max_adv, hj - trough)
        if max_adv > 0.75 * atr:
            return None
    if not _psh_is_inside_day(o, h, l, c, i - 1):
        return None
    if direction == "LONG":
        if float(h.iloc[i]) <= float(h.iloc[i - 1]):
            return None
        stop = float(l.iloc[i - 1])
    else:
        if float(l.iloc[i]) >= float(l.iloc[i - 1]):
            return None
        stop = float(h.iloc[i - 1])
    return _psh_signal(
        direction=direction, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW11_SHALLOW_GRIND_RIDER",
    )


# ── PTW12 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW12_GAP_CONTINUATION")
def ptw12_gap_continuation(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 1:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    open_i = float(o.iloc[i])
    close_prev = float(c.iloc[i - 1])
    close_i = float(c.iloc[i])
    gap = open_i - close_prev
    if direction == "LONG":
        if gap < 0.3 * atr:
            return None
        if not (close_i > open_i):
            return None
    else:
        if gap > -0.3 * atr:
            return None
        if not (close_i < open_i):
            return None
    return _psh_signal(
        direction=direction, stop_price=float(close_prev), timeframe="1d",
        strategy_id="PTW12_GAP_CONTINUATION",
    )


# ── PTW13 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW13_EARLY_BIRD_STAGER")
def ptw13_early_bird_stager(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    dwell = int(ctx.regime_days_in_state)
    if dwell < 5 or dwell > 10:
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    closes = [float(x) for x in c.tolist()]
    er = _psh_er_at(closes, i, 20)
    if er is None or er < 0.30:
        return None
    prior_hi = float(h.iloc[i - 20 : i].max())
    prior_lo = float(l.iloc[i - 20 : i].min())
    close_i = float(c.iloc[i])
    if direction == "LONG":
        if close_i <= prior_hi:
            return None
    else:
        if close_i >= prior_lo:
            return None
    sig = _psh_signal(
        direction=direction, stop_price=float(close_i), timeframe="1d",
        strategy_id="PTW13_EARLY_BIRD_STAGER",
    )
    sig["stop_atr_mult"] = 1.0
    return sig


# ── PTW14 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW14_WEEKEND_CARRY_HOLD")
def ptw14_weekend_carry_hold(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    direction = _ptw_gate(ctx)
    if direction is None:
        return None
    if ctx.day_of_week != 3:  # Thursday
        return None
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    _o, h, l, c = arr
    i = len(c) - 1
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    idx = ctx.ohlc().index
    mon_i = None
    for k in range(i, max(-1, i - 4) - 1, -1):
        try:
            dk = pd.Timestamp(idx[k]).date()
        except Exception:  # noqa: BLE001
            continue
        if dk.weekday() == 0:
            mon_i = k
            break
    if mon_i is None:
        return None
    move = float(c.iloc[i]) - float(c.iloc[mon_i])
    if direction == "LONG":
        if move < 1.0 * atr:
            return None
    else:
        if move > -1.0 * atr:
            return None
    sig = _psh_signal(
        direction=direction,
        stop_price=float(c.iloc[i]),
        timeframe="1d",
        strategy_id="PTW14_WEEKEND_CARRY_HOLD",
        custom_exit={"type": "time_exit", "max_sessions": 3},
    )
    sig["stop_atr_mult"] = 1.25
    return sig


# ── PTW15 ────────────────────────────────────────────────────────────────────
@register_shadow_strategy("PTW15_STALE_TREND_FADE")
def ptw15_stale_trend_fade(ctx: ShadowStrategyContext) -> dict[str, Any] | None:
    if ctx.timeframe != "1d":
        return None
    trend_d = _ptw_gate(ctx)
    if trend_d is None:
        return None
    dwell = int(ctx.regime_days_in_state)
    if dwell < 50:
        return None
    trade_d = "SHORT" if trend_d == "LONG" else "LONG"
    arr = _psh_ohlc_arrays(ctx.ohlc())
    if arr is None:
        return None
    o, h, l, c = arr
    i = len(c) - 1
    if i < 20:
        return None
    atr = _phw_atr_at(h, l, c, i)
    if atr is None:
        return None
    atr_prev = _phw_atr_at(h, l, c, i - 1)
    if atr_prev is None:
        return None
    sma20 = _psh_sma(c, 20)
    if pd.isna(sma20.iloc[i - 1]):
        return None
    close_prev = float(c.iloc[i - 1])
    sma_prev = float(sma20.iloc[i - 1])
    if trend_d == "LONG":
        if close_prev < sma_prev + 2.0 * atr_prev:
            return None
    else:
        if close_prev > sma_prev - 2.0 * atr_prev:
            return None
    open_i = float(o.iloc[i])
    close_i = float(c.iloc[i])
    high_i = float(h.iloc[i])
    low_i = float(l.iloc[i])
    bar_range = high_i - low_i
    if bar_range <= 0:
        return None
    if trend_d == "LONG":
        if not (close_i < open_i):
            return None
        if close_i > low_i + bar_range / 3.0:
            return None
        stop = float(h.iloc[i - 1 : i + 1].max())
    else:
        if not (close_i > open_i):
            return None
        if close_i < high_i - bar_range / 3.0:
            return None
        stop = float(l.iloc[i - 1 : i + 1].min())
    return _psh_signal(
        direction=trade_d, stop_price=float(stop), timeframe="1d",
        strategy_id="PTW15_STALE_TREND_FADE",
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
