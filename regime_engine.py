"""
Pair-level regime engine (directionless).

Standalone module — not wired into macro_manager / backtester / live yet.
Describes the PAIR state with hysteresis + minimum dwell; never labels a trade.

(PR #104 — reaffirmed on main for Railway redeploy)
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Sequence

# ── Tunable constants (single place) ─────────────────────────────────────────
ER_WINDOW = 20
HISTORY_MAX = 60

WEIGHT_RATE = 0.60
WEIGHT_ER = 0.40

RATE_LEVEL_WEIGHT = 0.70
RATE_CHANGE_WEIGHT = 0.30
RATE_LEVEL_SCALE = 3.0
RATE_CHANGE_SCALE = 0.5

ENTER_STRONG = 0.60
EXIT_STRONG = 0.40
ENTER_MILD = 0.25
EXIT_MILD = 0.15

MIN_DWELL_DAYS = 5

CONF_ER_WEIGHT = 0.40
CONF_SCORE_WEIGHT = 0.30
CONF_DWELL_WEIGHT = 0.30
CONF_ER_REF = 0.40
CONF_SCORE_REF = 0.60
CONF_DWELL_REF = 20.0

STATE_STRONG_UP = "STRONG_UP"
STATE_UP = "UP"
STATE_NEUTRAL = "NEUTRAL"
STATE_DOWN = "DOWN"
STATE_STRONG_DOWN = "STRONG_DOWN"

VALID_STATES = frozenset(
    {STATE_STRONG_UP, STATE_UP, STATE_NEUTRAL, STATE_DOWN, STATE_STRONG_DOWN}
)

# Bounded per-ticker histories (read-only to strategies via accessors).
_raw_score_history: dict[str, deque[float | None]] = {}
_er_history: dict[str, deque[float | None]] = {}
_confidence_history: dict[str, deque[float | None]] = {}
# ATR(14) snapshot at the start of the current regime episode.
_atr_at_regime_entry: dict[str, float | None] = {}
# Last session date that appended score/ER/confidence for a ticker.
_last_history_session: dict[str, str] = {}


def _hist_deque(store: dict[str, deque[float | None]], ticker: str) -> deque[float | None]:
    tku = (ticker or "").strip().upper()
    dq = store.get(tku)
    if dq is None:
        dq = deque(maxlen=HISTORY_MAX)
        store[tku] = dq
    return dq


def get_score_history(ticker: str) -> list[float | None]:
    """Last ≤60 raw_score values for ticker (oldest→newest)."""
    return list(_hist_deque(_raw_score_history, ticker))


def get_er_history(ticker: str) -> list[float | None]:
    """Last ≤60 efficiency-ratio values for ticker (oldest→newest)."""
    return list(_hist_deque(_er_history, ticker))


def get_confidence_history(ticker: str) -> list[float | None]:
    """Last ≤60 confidence values for ticker (oldest→newest)."""
    return list(_hist_deque(_confidence_history, ticker))


def get_atr_at_regime_entry(ticker: str) -> float | None:
    """ATR(14) captured when the current regime episode began."""
    return _atr_at_regime_entry.get((ticker or "").strip().upper())


def _record_histories(
    ticker: str,
    *,
    raw_score: float,
    er: float | None,
    confidence: float,
    changed: bool,
    atr: float | None,
    prev_atr_entry: float | None,
    append_series: bool = True,
) -> float | None:
    """Update atr_at_regime_entry; optionally append score/ER/confidence series."""
    tku = (ticker or "").strip().upper()
    if append_series:
        _hist_deque(_raw_score_history, tku).append(float(raw_score))
        _hist_deque(_er_history, tku).append(None if er is None else float(er))
        _hist_deque(_confidence_history, tku).append(float(confidence))

    atr_f: float | None
    try:
        atr_f = float(atr) if atr is not None else None
        if atr_f is not None and (math.isnan(atr_f) or math.isinf(atr_f) or atr_f <= 0):
            atr_f = None
    except (TypeError, ValueError):
        atr_f = None

    if changed:
        atr_entry = atr_f
    elif prev_atr_entry is not None:
        try:
            atr_entry = float(prev_atr_entry)
        except (TypeError, ValueError):
            atr_entry = atr_f if atr_f is not None else _atr_at_regime_entry.get(tku)
    else:
        # First observation of an episode — seed with current ATR when available.
        atr_entry = atr_f if atr_f is not None else _atr_at_regime_entry.get(tku)

    _atr_at_regime_entry[tku] = atr_entry
    return atr_entry


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def efficiency_ratio(closes: Sequence[float], n: int = ER_WINDOW) -> float | None:
    """Kaufman-style efficiency ratio on the last ``n`` steps. Pure; 0..1 or None."""
    if n <= 0:
        return None
    try:
        series = [float(c) for c in closes]
    except (TypeError, ValueError):
        return None
    if len(series) < n + 1:
        return None
    net = abs(series[-1] - series[-1 - n])
    path = 0.0
    for i in range(len(series) - n, len(series)):
        path += abs(series[i] - series[i - 1])
    if path <= 0.0:
        return 0.0
    return float(net / path)


def rate_component(
    rate_diff: float,
    rate_diff_4w_ago: float | None,
) -> tuple[float, bool]:
    """
    Blend LEVEL and CHANGE of the rate differential into -1..1.

    Returns (component, rate_change_available).
    When ``rate_diff_4w_ago`` is unavailable, uses level only.
    """
    level = math.tanh(float(rate_diff) / RATE_LEVEL_SCALE)
    if rate_diff_4w_ago is None:
        return _clamp(level, -1.0, 1.0), False
    change = math.tanh((float(rate_diff) - float(rate_diff_4w_ago)) / RATE_CHANGE_SCALE)
    blended = RATE_LEVEL_WEIGHT * level + RATE_CHANGE_WEIGHT * change
    return _clamp(blended, -1.0, 1.0), True


def load_rate_diffs(
    ticker: str,
    *,
    as_of_date: Any = None,
) -> tuple[float, float]:
    """Convenience wrapper: current and 4-week-ago differentials via macro_manager.

    Lazy-imports ``get_rate_differential`` so this module stays importable for
    pure helpers (``efficiency_ratio``, hysteresis) without pandas installed.
    """
    # Reuse existing CB rate lookup — do not rewrite.
    from macro_manager import get_rate_differential

    t = (ticker or "").strip().upper()
    base, quote = t[:3], t[3:6]
    level = float(get_rate_differential(base, quote, weeks_ago=0, as_of_date=as_of_date))
    ago = float(get_rate_differential(base, quote, weeks_ago=4, as_of_date=as_of_date))
    return level, ago


def _classify_with_hysteresis(raw_score: float, prev_state: str) -> str:
    """Map raw_score → state, keeping prev when score sits between exit and enter bands."""
    prev = prev_state if prev_state in VALID_STATES else STATE_NEUTRAL
    raw = float(raw_score)

    if prev == STATE_STRONG_UP:
        if raw >= EXIT_STRONG:
            return STATE_STRONG_UP
    elif prev == STATE_UP:
        if raw >= ENTER_STRONG:
            return STATE_STRONG_UP
        if raw >= EXIT_MILD:
            return STATE_UP
    elif prev == STATE_STRONG_DOWN:
        if raw <= -EXIT_STRONG:
            return STATE_STRONG_DOWN
    elif prev == STATE_DOWN:
        if raw <= -ENTER_STRONG:
            return STATE_STRONG_DOWN
        if raw <= -EXIT_MILD:
            return STATE_DOWN
    elif prev == STATE_NEUTRAL:
        # Between mild exit/enter bands → stay neutral (handled by enter thresholds below).
        pass

    if raw >= ENTER_STRONG:
        return STATE_STRONG_UP
    if raw >= ENTER_MILD:
        return STATE_UP
    if raw <= -ENTER_STRONG:
        return STATE_STRONG_DOWN
    if raw <= -ENTER_MILD:
        return STATE_DOWN
    return STATE_NEUTRAL


def compute_pair_regime(
    ticker: str,
    closes: Sequence[float],
    rate_diff: float,
    rate_diff_4w: float | None,
    prev_state: dict[str, Any] | None,
    atr: float | None = None,
    session_date: str | None = None,
) -> dict[str, Any]:
    """
    Directionless pair regime. Describes the PAIR, never a trade direction.

    ``prev_state`` is the previous return dict (or None on first scan).
    Optional ``atr`` (ATR14 as of this scan) seeds ``atr_at_regime_entry``
    when the state changes.
    Optional ``session_date`` (YYYY-MM-DD): append score/ER/confidence at most
    once per ticker per session day. When None, append on every successful call
    (unit tests / ad-hoc). Provisional no-closes (er=None) never appends.
    """
    prev = prev_state if isinstance(prev_state, dict) else {}
    prev_label = str(prev.get("state") or STATE_NEUTRAL).strip().upper()
    if prev_label not in VALID_STATES:
        prev_label = STATE_NEUTRAL
    try:
        prev_days = int(prev.get("days_in_state", 0) or 0)
    except (TypeError, ValueError):
        prev_days = 0
    prev_atr_entry = prev.get("atr_at_regime_entry")
    if prev_atr_entry is None:
        prev_atr_entry = get_atr_at_regime_entry(ticker)

    er = efficiency_ratio(closes, n=ER_WINDOW)
    rate_c, rate_change_available = rate_component(rate_diff, rate_diff_4w)
    tku = str(ticker or "").strip().upper()

    if er is None:
        # Insufficient price history — hold prior label, zero score contribution.
        # Never append score/ER/confidence histories on provisional no-closes.
        state = prev_label
        raw_score = 0.0
        price_direction = str(prev.get("price_direction") or "DOWN")
        days_in_state = prev_days + 1 if prev_state is not None else 1
        changed = False
        confidence = _clamp(
            CONF_ER_WEIGHT * 0.0
            + CONF_SCORE_WEIGHT * 0.0
            + CONF_DWELL_WEIGHT * min(days_in_state / CONF_DWELL_REF, 1.0),
            0.0,
            1.0,
        )
        atr_entry = _record_histories(
            tku,
            raw_score=raw_score,
            er=None,
            confidence=confidence,
            changed=changed,
            atr=atr,
            prev_atr_entry=float(prev_atr_entry) if prev_atr_entry is not None else None,
            append_series=False,
        )
        return {
            "ticker": tku,
            "state": state,
            "raw_score": round(raw_score, 6),
            "er": None,
            "price_direction": price_direction,
            "confidence": round(confidence, 6),
            "days_in_state": days_in_state,
            "changed_this_scan": changed,
            "rate_component": round(rate_c, 6),
            "rate_change_available": rate_change_available,
            "atr_at_regime_entry": atr_entry,
        }

    try:
        series = [float(c) for c in closes]
    except (TypeError, ValueError):
        series = []
    if len(series) >= ER_WINDOW + 1 and series[-1] > series[-1 - ER_WINDOW]:
        price_direction = "UP"
        er_signed = float(er)
    else:
        price_direction = "DOWN"
        er_signed = -float(er)

    raw_score = WEIGHT_RATE * rate_c + WEIGHT_ER * er_signed
    raw_score = _clamp(raw_score, -1.0, 1.0)

    candidate = _classify_with_hysteresis(raw_score, prev_label)

    # Minimum dwell: keep prior state for the first MIN_DWELL_DAYS scans.
    if prev_state is not None and prev_days < MIN_DWELL_DAYS and candidate != prev_label:
        state = prev_label
        changed = False
        days_in_state = prev_days + 1
    elif candidate == prev_label:
        state = candidate
        changed = False
        days_in_state = (prev_days + 1) if prev_state is not None else 1
    else:
        state = candidate
        changed = True
        days_in_state = 1

    confidence = _clamp(
        CONF_ER_WEIGHT * min(float(er) / CONF_ER_REF, 1.0)
        + CONF_SCORE_WEIGHT * min(abs(raw_score) / CONF_SCORE_REF, 1.0)
        + CONF_DWELL_WEIGHT * min(days_in_state / CONF_DWELL_REF, 1.0),
        0.0,
        1.0,
    )

    append_series = True
    ds = str(session_date or "").strip()[:10]
    if ds and _last_history_session.get(tku) == ds:
        append_series = False

    atr_entry = _record_histories(
        tku,
        raw_score=raw_score,
        er=float(er),
        confidence=confidence,
        changed=changed,
        atr=atr,
        prev_atr_entry=float(prev_atr_entry) if prev_atr_entry is not None else None,
        append_series=append_series,
    )
    if append_series and ds:
        _last_history_session[tku] = ds

    return {
        "ticker": tku,
        "state": state,
        "raw_score": round(raw_score, 6),
        "er": round(float(er), 6),
        "price_direction": price_direction,
        "confidence": round(confidence, 6),
        "days_in_state": days_in_state,
        "changed_this_scan": changed,
        "rate_component": round(rate_c, 6),
        "rate_change_available": rate_change_available,
        "atr_at_regime_entry": atr_entry,
    }


if __name__ == "__main__":
    # Clean uptrend: net ≈ path → ER ~ 1.0
    trend = [float(i) for i in range(30)]
    # Zigzag chop: net small, path large → ER ~ 0.1 or lower
    zigzag: list[float] = []
    x = 100.0
    for i in range(30):
        x += 1.0 if i % 2 == 0 else -1.0
        zigzag.append(x)

    er_trend = efficiency_ratio(trend, n=ER_WINDOW)
    er_chop = efficiency_ratio(zigzag, n=ER_WINDOW)
    print(f"efficiency_ratio(clean trend, n={ER_WINDOW}) = {er_trend}")
    print(f"efficiency_ratio(zigzag chop, n={ER_WINDOW}) = {er_chop}")
    print("(expect trend ~1.0, chop ~0.0–0.1)")
