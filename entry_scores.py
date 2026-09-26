"""
Write-only entry-time numeric scores for chrono / continuous backtests.

Measured at ENTRY TIME only (no lookahead). Never generate entries or change
sizing / exits — logged so post-hoc analysis can test which ideas sort winners
from losers for sizing (like A+B).
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from high_impact_events_data import HIGH_IMPACT_EVENTS
from macro_manager import _CB_RATE_TIMELINE, _cb_rates_on, get_rate_differential
from utils import log

# ── Score catalog (26 names; some may be permanently unavailable) ─────────────

ENTRY_SCORE_KEYS: tuple[str, ...] = (
    # A. Cross-sectional
    "cs_ccy_strength_base",
    "cs_ccy_strength_quote",
    "cs_ccy_rank_spread",
    "cs_rank_velocity_base",
    "cs_dispersion",
    "cs_basket_divergence",
    "cs_triangular_gap",
    "cs_contagion_lag",
    # B. Macro dynamics
    "mc_rate_diff_delta20",
    "mc_real_rate_diff",
    "mc_carry_to_vol",
    "mc_curve_slope_diff",
    "mc_policy_age_days",
    "mc_level_change_agree",
    # C. Statistical arbitrage
    "sa_coint_z",
    "sa_coint_pvalue",
    "sa_halflife_days",
    "sa_basket_z",
    # D. Event response
    "ev_last_reaction_atr",
    "ev_reaction_reversed",
    "ev_days_since_event",
    "ev_calendar_density",
    # E. Structure
    "st_dist_to_high_atr",
    "st_dist_to_low_atr",
    "st_trend_age_days",
    "st_range_compression",
)

# Scores that cannot be computed from currently wired pre-entry data.
# Warned once at run start; always written as null in the scores map.
UNAVAILABLE_ENTRY_SCORES: dict[str, str] = {
    "mc_real_rate_diff": (
        "no CPI / inflation level series wired (FRED stub only; CB rates have no real-rate overlay)"
    ),
    "mc_curve_slope_diff": (
        "no 10Y–2Y sovereign yield series available in macro_manager / FRED wiring"
    ),
}

# G8 currencies for cross-sectional rank (classic major set).
_G8: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF")

# Majors used as USD hubs for triangular / strength construction.
_USD_HUB_PAIRS: dict[str, str] = {
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "JPY": "USDJPY",  # quote = JPY
    "CAD": "USDCAD",
    "CHF": "USDCHF",
}

DailyOhlcGetter = Callable[[str, date], pd.DataFrame | None]

_EVENTS_BY_DATE: dict[str, list[tuple[str, str, str]]] = {}
_EVENTS_BY_DATE_CUR: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
_warned_unavailable = False

# Per-run accumulators for scores_summary (also recomputed from trade rows at end).
_SCORE_OBS: dict[str, list[float]] = {k: [] for k in ENTRY_SCORE_KEYS}
_SCORE_MISSING: dict[str, int] = {k: 0 for k in ENTRY_SCORE_KEYS}


def reset_entry_score_stats() -> None:
    """Clear per-run observation buffers (call at chrono / loop start)."""
    global _SCORE_OBS, _SCORE_MISSING, _warned_unavailable
    _SCORE_OBS = {k: [] for k in ENTRY_SCORE_KEYS}
    _SCORE_MISSING = {k: 0 for k in ENTRY_SCORE_KEYS}
    _warned_unavailable = False


def log_unavailable_entry_scores() -> None:
    """Warn once at run start naming scores that cannot be computed from available data."""
    global _warned_unavailable
    if _warned_unavailable:
        return
    _warned_unavailable = True
    for name, reason in UNAVAILABLE_ENTRY_SCORES.items():
        log(
            f"[ENTRY SCORES] Unavailable score '{name}': {reason} — will write null",
            level="warning",
        )


def _init_event_indexes() -> None:
    if _EVENTS_BY_DATE:
        return
    for ds, cur, etype in HIGH_IMPACT_EVENTS:
        _EVENTS_BY_DATE.setdefault(ds, []).append((ds, cur, etype))
        _EVENTS_BY_DATE_CUR.setdefault((ds, str(cur).upper()), []).append((ds, cur, etype))


_init_event_indexes()


def _parse_date(d: date | str | None) -> date | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d).strip()[:10])
    except ValueError:
        return None


def _currencies(ticker: str) -> tuple[str, str] | None:
    t = (ticker or "").strip().upper()
    if len(t) == 6 and t.isalpha():
        return t[:3], t[3:]
    return None


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _empty_scores() -> dict[str, float | None]:
    return {k: None for k in ENTRY_SCORE_KEYS}


def _atr14(df: pd.DataFrame) -> float | None:
    if df is None or getattr(df, "empty", True) or len(df) < 15:
        return None
    try:
        h = pd.to_numeric(df["High"], errors="coerce").astype(float)
        l = pd.to_numeric(df["Low"], errors="coerce").astype(float)
        c = pd.to_numeric(df["Close"], errors="coerce").astype(float)
    except Exception:  # noqa: BLE001
        return None
    prev = c.shift(1)
    tr = (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)
    atr = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    v = _finite(atr.iloc[-1])
    return v if v is not None and v > 0 else None


def _closes(df: pd.DataFrame) -> np.ndarray:
    s = pd.to_numeric(df["Close"], errors="coerce").dropna().astype(float)
    return s.to_numpy(dtype=float)


def _slice_as_of(df: pd.DataFrame | None, as_of: date) -> pd.DataFrame | None:
    if df is None or getattr(df, "empty", True):
        return None
    out = df
    try:
        if not isinstance(out.index, pd.DatetimeIndex):
            out = out.copy()
            out.index = pd.to_datetime(out.index)
        day_end = pd.Timestamp(as_of) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        out = out[out.index <= day_end]
    except Exception:  # noqa: BLE001
        return None
    return None if out is None or out.empty else out


def _ret_n(closes: np.ndarray, n: int) -> float | None:
    if closes is None or len(closes) < n + 1:
        return None
    a = float(closes[-(n + 1)])
    b = float(closes[-1])
    if a <= 0 or b <= 0:
        return None
    return (b / a) - 1.0


def _realized_vol_20(closes: np.ndarray) -> float | None:
    if closes is None or len(closes) < 21:
        return None
    window = closes[-21:]
    if np.any(window <= 0):
        return None
    lr = np.diff(np.log(window))
    if len(lr) < 5:
        return None
    vol = float(np.std(lr, ddof=1))
    return vol if vol > 1e-12 else None


# ── A. Cross-sectional ────────────────────────────────────────────────────────


def _pair_return_as_of(
    pair: str,
    as_of: date,
    n: int,
    get_daily: DailyOhlcGetter,
) -> float | None:
    df = _slice_as_of(get_daily(pair, as_of), as_of)
    if df is None:
        return None
    return _ret_n(_closes(df), n)


def _ccy_strength_map(
    as_of: date,
    n: int,
    get_daily: DailyOhlcGetter,
    universe: tuple[str, ...],
) -> dict[str, float] | None:
    """
    Aggregate n-day move per G8 currency from all forex pairs in ``universe``.
    Sign: + for base leg appreciation contribution, − for quote.
    """
    acc: dict[str, list[float]] = {c: [] for c in _G8}
    for pair in universe:
        curs = _currencies(pair)
        if curs is None:
            continue
        base, quote = curs
        if base not in acc and quote not in acc:
            continue
        if base not in _G8 or quote not in _G8:
            continue
        r = _pair_return_as_of(pair, as_of, n, get_daily)
        if r is None:
            continue
        acc[base].append(r)
        acc[quote].append(-r)
    out: dict[str, float] = {}
    for c, vals in acc.items():
        if not vals:
            return None
        out[c] = float(sum(vals) / len(vals))
    return out


def _rank_map(strength: Mapping[str, float]) -> dict[str, int]:
    """Rank 1 = strongest (highest aggregate move), 8 = weakest."""
    ordered = sorted(strength.items(), key=lambda kv: kv[1], reverse=True)
    return {ccy: i + 1 for i, (ccy, _) in enumerate(ordered)}


def _usd_rate(ccy: str, as_of: date, get_daily: DailyOhlcGetter) -> float | None:
    """Units of USD per 1 unit of ``ccy`` (EUR→EURUSD; JPY→1/USDJPY)."""
    c = (ccy or "").strip().upper()
    if c == "USD":
        return 1.0
    hub = _USD_HUB_PAIRS.get(c)
    if not hub:
        return None
    df = _slice_as_of(get_daily(hub, as_of), as_of)
    if df is None or df.empty:
        return None
    px = _finite(pd.to_numeric(df["Close"], errors="coerce").iloc[-1])
    if px is None or px <= 0:
        return None
    if hub.startswith("USD"):
        return 1.0 / px
    return px


def _cross_sectional_scores(
    ticker: str,
    as_of: date,
    get_daily: DailyOhlcGetter,
    universe: tuple[str, ...],
) -> dict[str, float | None]:
    out: dict[str, float | None] = {k: None for k in ENTRY_SCORE_KEYS if k.startswith("cs_")}
    curs = _currencies(ticker)
    if curs is None:
        return out
    base, quote = curs

    strength = _ccy_strength_map(as_of, 20, get_daily, universe)
    if strength is None:
        return out
    ranks = _rank_map(strength)
    out["cs_ccy_strength_base"] = float(ranks.get(base)) if base in ranks else None
    out["cs_ccy_strength_quote"] = float(ranks.get(quote)) if quote in ranks else None
    if base in ranks and quote in ranks:
        out["cs_ccy_rank_spread"] = float(ranks[base] - ranks[quote])

    strength_5 = _ccy_strength_map(as_of - timedelta(days=5), 20, get_daily, universe)
    if strength_5 is not None and base in strength and base in strength_5:
        ranks_5 = _rank_map(strength_5)
        if base in ranks and base in ranks_5:
            out["cs_rank_velocity_base"] = float(ranks[base] - ranks_5[base])

    vals = list(strength.values())
    out["cs_dispersion"] = float(max(vals) - min(vals))

    pair_ret = _pair_return_as_of(ticker, as_of, 20, get_daily)
    implied = strength.get(base, 0.0) - strength.get(quote, 0.0)
    if pair_ret is not None:
        out["cs_basket_divergence"] = float(pair_ret - implied)

    # Triangular gap vs USD-hub implied cross, in ATR units.
    ub = _usd_rate(base, as_of, get_daily)
    uq = _usd_rate(quote, as_of, get_daily)
    df_pair = _slice_as_of(get_daily(ticker, as_of), as_of)
    atr = _atr14(df_pair) if df_pair is not None else None
    if ub is not None and uq is not None and uq > 0 and df_pair is not None and atr:
        implied_px = ub / uq
        actual = _finite(pd.to_numeric(df_pair["Close"], errors="coerce").iloc[-1])
        if actual is not None and actual > 0:
            out["cs_triangular_gap"] = float((actual - implied_px) / atr)

    # Contagion lag: this pair's 20d move vs mean of other pairs sharing strongest leg.
    if base in ranks and quote in ranks:
        strong_leg = base if ranks[base] <= ranks[quote] else quote
        peer_rets: list[float] = []
        self_ret = pair_ret
        for pair in universe:
            curs_p = _currencies(pair)
            if curs_p is None or pair == ticker:
                continue
            if strong_leg not in curs_p:
                continue
            # Signed from the strong leg's perspective.
            r = _pair_return_as_of(pair, as_of, 20, get_daily)
            if r is None:
                continue
            b2, q2 = curs_p
            peer_rets.append(r if b2 == strong_leg else -r)
        if self_ret is not None and peer_rets:
            self_signed = self_ret if base == strong_leg else -self_ret
            out["cs_contagion_lag"] = float(self_signed - (sum(peer_rets) / len(peer_rets)))

    return out


# ── B. Macro ──────────────────────────────────────────────────────────────────


def _policy_age_days(base: str, quote: str, as_of: date) -> float | None:
    merged: dict[str, float] = {}
    last_change: date | None = None
    for eff, patch in _CB_RATE_TIMELINE:
        if eff > as_of:
            break
        for ccy in (base, quote):
            if ccy in patch:
                new_r = float(patch[ccy])
                if ccy not in merged or merged[ccy] != new_r:
                    last_change = eff
                merged[ccy] = new_r
    # Also reflect CENTRAL_BANK_RATES overlay via _cb_rates_on final state,
    # but only timeline steps mark change dates.
    if last_change is None:
        # Ensure currencies exist in rate table at all.
        rates = _cb_rates_on(as_of)
        if base not in rates and quote not in rates:
            return None
        return None
    return float((as_of - last_change).days)


def _macro_scores(
    ticker: str,
    as_of: date,
    get_daily: DailyOhlcGetter,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {k: None for k in ENTRY_SCORE_KEYS if k.startswith("mc_")}
    for k in UNAVAILABLE_ENTRY_SCORES:
        if k.startswith("mc_"):
            out[k] = None
    curs = _currencies(ticker)
    if curs is None:
        return out
    base, quote = curs

    rd_now = get_rate_differential(base, quote, as_of_date=as_of)
    rd_20 = get_rate_differential(base, quote, as_of_date=as_of - timedelta(days=20))
    delta20 = float(rd_now - rd_20)
    out["mc_rate_diff_delta20"] = delta20

    df = _slice_as_of(get_daily(ticker, as_of), as_of)
    vol = _realized_vol_20(_closes(df)) if df is not None else None
    if vol is not None and vol > 0:
        out["mc_carry_to_vol"] = float(rd_now) / vol

    age = _policy_age_days(base, quote, as_of)
    if age is not None:
        out["mc_policy_age_days"] = age

    # Agree if rate level and 20d change share sign (both zero → agree).
    if rd_now == 0.0 and delta20 == 0.0:
        out["mc_level_change_agree"] = 1.0
    elif rd_now == 0.0 or delta20 == 0.0:
        out["mc_level_change_agree"] = 0.0
    else:
        out["mc_level_change_agree"] = 1.0 if (rd_now > 0) == (delta20 > 0) else 0.0

    return out


# ── C. Statistical arbitrage ──────────────────────────────────────────────────


def _eg_ols_spread(y: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, float, float] | None:
    """
    Engle–Granger: OLS y ~ a + b x, ADF on residuals, OU half-life from residual AR(1).
    Returns (resid, adf_pvalue_approx, half_life_days) or None.
    """
    n = min(len(y), len(x))
    if n < 40:
        return None
    y = np.asarray(y[-n:], dtype=float)
    x = np.asarray(x[-n:], dtype=float)
    if np.any(~np.isfinite(y)) or np.any(~np.isfinite(x)):
        return None
    X = np.column_stack([np.ones(n), x])
    try:
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = y - X @ beta
    # ADF (no const on residual diffs): Δr = γ r_{t-1} + e
    r_lag = resid[:-1]
    dr = np.diff(resid)
    denom = float(np.dot(r_lag, r_lag))
    if denom <= 1e-18:
        return None
    gamma = float(np.dot(r_lag, dr) / denom)
    e = dr - gamma * r_lag
    dof = max(1, len(e) - 1)
    s2 = float(np.dot(e, e) / dof)
    se = math.sqrt(s2 / denom) if s2 > 0 else None
    if se is None or se <= 0:
        return None
    adf_stat = gamma / se
    pval = _mackinnon_p_eg(adf_stat, n)
    if gamma >= 0 or abs(1.0 + gamma) < 1e-12:
        hl = float("nan")
    else:
        hl = -math.log(2.0) / math.log(1.0 + gamma)
        if not math.isfinite(hl) or hl <= 0 or hl > 1e6:
            hl = float("nan")
    return resid, pval, hl


def _mackinnon_p_eg(adf_stat: float, n: int) -> float:
    """
    Rough MacKinnon-style p-value for Engle–Granger residual ADF (no deterministic).
    Uses critical-value interpolation at common levels; not a full tau distribution.
    """
    # Approximate finite-sample critical values (tau, no const) for n≈100.
    # More negative ⇒ smaller p.
    # CV approx: 1%≈-3.90, 5%≈-3.34, 10%≈-3.04 (EG residual, n~100).
    adj = 0.0 if n >= 100 else 0.15 * (100 - n) / 100.0
    c01, c05, c10 = -3.90 - adj, -3.34 - adj, -3.04 - adj
    t = float(adf_stat)
    if t <= c01:
        return 0.005
    if t <= c05:
        # interpolate 0.01–0.05
        return 0.01 + 0.04 * (t - c01) / (c05 - c01)
    if t <= c10:
        return 0.05 + 0.05 * (t - c05) / (c10 - c05)
    if t <= -2.5:
        return 0.10 + 0.20 * (t - c10) / (-2.5 - c10)
    if t <= -1.5:
        return 0.30 + 0.40 * (t + 2.5) / 1.0
    return min(0.99, 0.70 + 0.29 * min(1.0, (t + 1.5) / 3.0))


def _align_closes(
    a: pd.DataFrame,
    b: pd.DataFrame,
    window: int = 120,
) -> tuple[np.ndarray, np.ndarray] | None:
    try:
        ca = pd.to_numeric(a["Close"], errors="coerce")
        cb = pd.to_numeric(b["Close"], errors="coerce")
        joined = pd.concat([ca.rename("a"), cb.rename("b")], axis=1).dropna()
    except Exception:  # noqa: BLE001
        return None
    if len(joined) < max(40, window // 2):
        return None
    joined = joined.tail(window)
    ya = np.log(joined["a"].to_numpy(dtype=float))
    yb = np.log(joined["b"].to_numpy(dtype=float))
    if np.any(~np.isfinite(ya)) or np.any(~np.isfinite(yb)):
        return None
    return ya, yb


def _stat_arb_scores(
    ticker: str,
    as_of: date,
    get_daily: DailyOhlcGetter,
    universe: tuple[str, ...],
) -> dict[str, float | None]:
    out: dict[str, float | None] = {k: None for k in ENTRY_SCORE_KEYS if k.startswith("sa_")}
    curs = _currencies(ticker)
    if curs is None:
        return out
    base, quote = curs
    self_df = _slice_as_of(get_daily(ticker, as_of), as_of)
    if self_df is None or len(self_df) < 60:
        return out

    best: tuple[float, np.ndarray, float, float] | None = None  # p, resid, p, hl
    for partner in universe:
        if partner == ticker:
            continue
        pc = _currencies(partner)
        if pc is None:
            continue
        # Prefer partners sharing a leg; still allow others.
        pdf = _slice_as_of(get_daily(partner, as_of), as_of)
        if pdf is None:
            continue
        aligned = _align_closes(self_df, pdf, 120)
        if aligned is None:
            continue
        y, x = aligned
        pack = _eg_ols_spread(y, x)
        if pack is None:
            continue
        resid, pval, hl = pack
        if best is None or pval < best[0]:
            best = (pval, resid, pval, hl)

    if best is not None:
        _p, resid, pval, hl = best
        mu = float(np.mean(resid))
        sd = float(np.std(resid, ddof=1))
        if sd > 1e-12:
            out["sa_coint_z"] = float((resid[-1] - mu) / sd)
        out["sa_coint_pvalue"] = float(pval)
        if math.isfinite(hl):
            out["sa_halflife_days"] = float(hl)

    # Basket z: residual of this pair vs equal-weight same-base synthetic basket.
    same_base = [p for p in universe if p != ticker and _currencies(p) and _currencies(p)[0] == base]
    if same_base:
        self_c = _closes(self_df)
        if len(self_c) >= 60:
            basket_cols: list[pd.Series] = []
            idx = self_df.index
            for p in same_base:
                pdf = _slice_as_of(get_daily(p, as_of), as_of)
                if pdf is None:
                    continue
                try:
                    s = pd.to_numeric(pdf["Close"], errors="coerce")
                    basket_cols.append(s.reindex(idx))
                except Exception:  # noqa: BLE001
                    continue
            if basket_cols:
                mat = pd.concat(basket_cols, axis=1).dropna(how="any")
                sc = pd.to_numeric(self_df["Close"], errors="coerce").reindex(mat.index).dropna()
                mat = mat.loc[sc.index]
                if len(sc) >= 40:
                    # Log-price residual vs mean log basket.
                    y = np.log(sc.to_numpy(dtype=float))
                    bask = np.log(mat.to_numpy(dtype=float)).mean(axis=1)
                    pack = _eg_ols_spread(y, bask)
                    if pack is not None:
                        resid, _, _ = pack
                        mu = float(np.mean(resid))
                        sd = float(np.std(resid, ddof=1))
                        if sd > 1e-12:
                            out["sa_basket_z"] = float((resid[-1] - mu) / sd)

    return out


# ── D. Event response ─────────────────────────────────────────────────────────


def _events_for_legs(base: str, quote: str, d: date) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for cur in (base, quote):
        out.extend(_EVENTS_BY_DATE_CUR.get((d.isoformat(), cur), []))
    return out


def _last_event_before(base: str, quote: str, as_of: date, horizon: int = 365) -> date | None:
    for offset in range(0, horizon + 1):
        probe = as_of - timedelta(days=offset)
        if _events_for_legs(base, quote, probe):
            return probe
    return None


def _events_in_next_n(base: str, quote: str, as_of: date, n: int) -> int:
    count = 0
    for offset in range(1, n + 1):  # next 7 days (exclude today as "future")
        probe = as_of + timedelta(days=offset)
        count += len(_events_for_legs(base, quote, probe))
    return count


def _event_scores(
    ticker: str,
    as_of: date,
    get_daily: DailyOhlcGetter,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {k: None for k in ENTRY_SCORE_KEYS if k.startswith("ev_")}
    curs = _currencies(ticker)
    if curs is None:
        return out
    base, quote = curs

    last_ev = _last_event_before(base, quote, as_of)
    if last_ev is not None:
        out["ev_days_since_event"] = float((as_of - last_ev).days)
        df = _slice_as_of(get_daily(ticker, as_of), as_of)
        if df is not None and len(df) >= 16:
            # Reaction on event day: need bars up through as_of; locate event day close.
            try:
                idx = pd.to_datetime(df.index).normalize()
                ev_ts = pd.Timestamp(last_ev)
                # Bars on or before event day.
                mask_ev = idx <= ev_ts
                sub = df.loc[mask_ev]
                if len(sub) >= 16:
                    atr = _atr14(sub)
                    closes = pd.to_numeric(sub["Close"], errors="coerce")
                    if atr and len(closes) >= 2:
                        c0 = _finite(closes.iloc[-1])
                        c_prev = _finite(closes.iloc[-2])
                        if c0 is not None and c_prev is not None:
                            move = c0 - c_prev
                            out["ev_last_reaction_atr"] = float(move / atr)
                            # Fully retraced by entry if price returned to/through pre-event close.
                            c_now = _finite(pd.to_numeric(df["Close"], errors="coerce").iloc[-1])
                            if c_now is not None:
                                if move > 0:
                                    reversed_ = 1.0 if c_now <= c_prev else 0.0
                                elif move < 0:
                                    reversed_ = 1.0 if c_now >= c_prev else 0.0
                                else:
                                    reversed_ = 1.0
                                out["ev_reaction_reversed"] = reversed_
            except Exception:  # noqa: BLE001
                pass

    out["ev_calendar_density"] = float(_events_in_next_n(base, quote, as_of, 7))
    return out


# ── E. Structure ──────────────────────────────────────────────────────────────


def _structure_scores(
    ticker: str,
    as_of: date,
    get_daily: DailyOhlcGetter,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {k: None for k in ENTRY_SCORE_KEYS if k.startswith("st_")}
    df = _slice_as_of(get_daily(ticker, as_of), as_of)
    if df is None or len(df) < 50:
        return out
    atr = _atr14(df)
    try:
        close = pd.to_numeric(df["Close"], errors="coerce").astype(float)
        high = pd.to_numeric(df["High"], errors="coerce").astype(float)
        low = pd.to_numeric(df["Low"], errors="coerce").astype(float)
    except Exception:  # noqa: BLE001
        return out
    c_now = _finite(close.iloc[-1])
    if c_now is None or not atr:
        return out

    hi20 = _finite(high.iloc[-20:].max())
    lo20 = _finite(low.iloc[-20:].min())
    if hi20 is not None:
        out["st_dist_to_high_atr"] = float((hi20 - c_now) / atr)
    if lo20 is not None:
        out["st_dist_to_low_atr"] = float((c_now - lo20) / atr)

    # Trend = sign(close - SMA20); age = bars since last flip.
    sma = close.rolling(20).mean()
    diff = close - sma
    signs = np.sign(diff.to_numpy(dtype=float))
    age = 0
    cur = signs[-1]
    if cur == 0 or not math.isfinite(float(cur)):
        out["st_trend_age_days"] = 0.0
    else:
        for i in range(len(signs) - 1, -1, -1):
            s = signs[i]
            if not math.isfinite(float(s)) or s == 0 or s != cur:
                break
            age += 1
        out["st_trend_age_days"] = float(age)

    # 10d range / mean daily range over 50d
    if len(df) >= 50:
        range_10 = float(high.iloc[-10:].max() - low.iloc[-10:].min())
        daily_range = (high - low).iloc[-50:]
        avg_range = float(daily_range.mean())
        if avg_range > 1e-12 and math.isfinite(range_10):
            out["st_range_compression"] = float(range_10 / avg_range)

    return out


# ── Public API ────────────────────────────────────────────────────────────────


def compute_entry_scores(
    ticker: str,
    entry_date: date | str,
    *,
    get_daily_ohlc: DailyOhlcGetter,
    universe: tuple[str, ...] | list[str] | None = None,
) -> dict[str, float | None]:
    """
    Compute all entry-time scores for ``ticker`` as of ``entry_date``.

    Uses only data available at entry (OHLC ≤ entry_date, CB rates ≤ entry_date,
    static calendar). Unavailable scores are null.
    """
    as_of = _parse_date(entry_date)
    scores = _empty_scores()
    if as_of is None:
        return scores
    tkr = (ticker or "").strip().upper()
    if not tkr or _currencies(tkr) is None:
        return scores

    uni_list = [str(x).strip().upper() for x in (universe or ()) if str(x).strip()]
    uni = tuple(p for p in uni_list if _currencies(p) is not None)
    if not uni:
        uni = (tkr,)

    try:
        scores.update(_cross_sectional_scores(tkr, as_of, get_daily_ohlc, uni))
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY SCORES] cross-sectional failed {tkr} {as_of}: {e}", level="warning")
    try:
        scores.update(_macro_scores(tkr, as_of, get_daily_ohlc))
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY SCORES] macro failed {tkr} {as_of}: {e}", level="warning")
    try:
        scores.update(_stat_arb_scores(tkr, as_of, get_daily_ohlc, uni))
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY SCORES] stat-arb failed {tkr} {as_of}: {e}", level="warning")
    try:
        scores.update(_event_scores(tkr, as_of, get_daily_ohlc))
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY SCORES] event failed {tkr} {as_of}: {e}", level="warning")
    try:
        scores.update(_structure_scores(tkr, as_of, get_daily_ohlc))
    except Exception as e:  # noqa: BLE001
        log(f"[ENTRY SCORES] structure failed {tkr} {as_of}: {e}", level="warning")

    # Force permanently unavailable keys to null.
    for k in UNAVAILABLE_ENTRY_SCORES:
        scores[k] = None

    # Round numerics for stable JSON.
    for k, v in list(scores.items()):
        if v is None:
            continue
        fv = _finite(v)
        if fv is None:
            scores[k] = None
        else:
            scores[k] = round(fv, 6)

    return scores


def record_score_observations(scores: Mapping[str, Any]) -> None:
    """Accumulate one trade's scores into the run buffers."""
    for k in ENTRY_SCORE_KEYS:
        v = scores.get(k) if scores else None
        fv = _finite(v)
        if fv is None:
            _SCORE_MISSING[k] = int(_SCORE_MISSING.get(k, 0) or 0) + 1
        else:
            _SCORE_OBS.setdefault(k, []).append(fv)


def attach_entry_scores(
    row: dict[str, Any],
    *,
    get_daily_ohlc: DailyOhlcGetter,
    universe: tuple[str, ...] | list[str] | None = None,
) -> dict[str, float | None]:
    """Compute scores for a trade row, write ``row['scores']``, record stats."""
    ticker = str(row.get("ticker") or "")
    entry_date = row.get("date") or row.get("entry_date")
    scores = compute_entry_scores(
        ticker,
        entry_date,
        get_daily_ohlc=get_daily_ohlc,
        universe=universe,
    )
    row["scores"] = scores
    record_score_observations(scores)
    return scores


def build_scores_summary(
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Per-score summary: count, min, median, max, unavailable.

    If ``trades`` is provided, recompute from each row's ``scores`` map;
    otherwise use the in-memory run buffers.
    """
    obs: dict[str, list[float]] = {k: [] for k in ENTRY_SCORE_KEYS}
    missing: dict[str, int] = {k: 0 for k in ENTRY_SCORE_KEYS}

    if trades is not None:
        n_trades = 0
        for t in trades:
            if not isinstance(t, dict):
                continue
            if t.get("skipped"):
                continue
            if str(t.get("outcome") or "").strip().upper() not in ("WIN", "LOSS"):
                continue
            n_trades += 1
            sc = t.get("scores")
            if not isinstance(sc, dict):
                for k in ENTRY_SCORE_KEYS:
                    missing[k] += 1
                continue
            for k in ENTRY_SCORE_KEYS:
                fv = _finite(sc.get(k))
                if fv is None:
                    missing[k] += 1
                else:
                    obs[k].append(fv)
    else:
        obs = {k: list(v) for k, v in _SCORE_OBS.items()}
        missing = dict(_SCORE_MISSING)

    summary: dict[str, dict[str, Any]] = {}
    for k in ENTRY_SCORE_KEYS:
        vals = obs.get(k) or []
        if vals:
            summary[k] = {
                "count": len(vals),
                "min": round(min(vals), 6),
                "median": round(float(statistics.median(vals)), 6),
                "max": round(max(vals), 6),
                "unavailable": int(missing.get(k, 0) or 0),
            }
        else:
            summary[k] = {
                "count": 0,
                "min": None,
                "median": None,
                "max": None,
                "unavailable": int(missing.get(k, 0) or 0),
            }
    return summary
