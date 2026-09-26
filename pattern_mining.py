"""
Offline pattern mining over entry_features_*.jsonl (PR K snapshots).

Discipline (non-negotiable — last round produced zero deployable strategies):
  91 shadow strategies were fit on one dataset; ~8 "passed" a both-halves test
  while chance alone predicts ~10. Every survivor was noise. The failure was
  testing many candidates against one dataset with no untouched data left.

This harness:
  * Declares three windows up front and NEVER mixes them.
  * Searches only on BUILD; confirms on TIER1; SEALED is readable only with
    an explicit ``--unseal`` (logged loudly).
  * Uses pre-registered thresholds (not tuned after seeing results).
  * Reports n_candidates and expected false positives (n * 0.05).
  * Caps conjunction depth at three conditions.
  * Does NOT import continuous_backtester and NEVER writes under /data.

Usage:
  python pattern_mining.py --input /path/to/entry_features_dir_or_glob
  python pattern_mining.py --input ./snapshots --unseal --output ./survivors.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# Feature/label catalogs only — never continuous_backtester.
from entry_features import ENTRY_FEATURE_KEYS, ENTRY_LABEL_KEYS

# ── Windows (declared up front, never mixed) ─────────────────────────────────

BUILD_END = date(2024, 6, 1)  # BUILD = trades strictly before this
TIER1_END = date(2025, 6, 1)  # TIER1 = [BUILD_END, TIER1_END)
# SEALED = trades on/after TIER1_END

WINDOW_BUILD = "BUILD"
WINDOW_TIER1 = "TIER1"
WINDOW_SEALED = "SEALED"

# ── Pre-registered thresholds (hardcoded — do not tune post-hoc) ─────────────

MIN_TRADES_PER_WINDOW = 100
MIN_R_PER_TRADE = 0.15  # swap alone ~0.07R; system ~-0.038R net → +0.05R is worthless
FALSE_POSITIVE_RATE = 0.05  # for expected-FP header
MAX_CONJUNCTION_DEPTH = 3

# Fields mined for atomic conditions (actionable subset — not every score).
MINE_NUMERIC: tuple[str, ...] = (
    "zone_position_pct",
    "trend_strength",
    "macro_score",
    "macro_rate_diff",
    "conviction_score",
    "st_layer2_score",
    "strategy_confluence_count",
    "entry_atr_vs_avg",
    "risk_pct_of_price",
    "ab_throttle",
    "sys_winrate_last20",
    "sys_followthrough_last20",
    "strat_health_last3",
    "st_health_last3",
    "weekly_candle_age",
    "event_days_to_next",
    "event_days_since_last",
    "cs_dispersion",
    "cs_ccy_rank_spread",
    "st_dist_to_high_atr",
    "st_dist_to_low_atr",
    "st_range_compression",
    "mc_carry_to_vol",
    "mc_rate_diff_delta20",
)

MINE_CATEGORICAL: tuple[str, ...] = (
    "timeframe",
    "direction",
    "macro_bias",
    "confidence",
    "period_mode",
    "market_phase",
    "volatility_regime",
    "zone_label",
    "session",
    "event_entry_window",
    "st_boost_tier",
)

# Quantile cut-points for numeric threshold proposals on BUILD.
NUMERIC_QUANTILES: tuple[float, ...] = (0.25, 0.50, 0.75)


@dataclass(frozen=True)
class Condition:
    field: str
    op: str  # "<", "<=", ">", ">=", "=="
    value: Any

    def matches(self, features: Mapping[str, Any]) -> bool:
        raw = features.get(self.field)
        if raw is None:
            return False
        if self.op == "==":
            return str(raw).strip().upper() == str(self.value).strip().upper()
        try:
            x = float(raw)
            v = float(self.value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(x) or not math.isfinite(v):
            return False
        if self.op == "<":
            return x < v
        if self.op == "<=":
            return x <= v
        if self.op == ">":
            return x > v
        if self.op == ">=":
            return x >= v
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}

    def __str__(self) -> str:
        if self.op == "==":
            return f'({self.field} == {json.dumps(self.value)})'
        return f"({self.field} {self.op} {self.value})"


@dataclass(frozen=True)
class Candidate:
    conditions: tuple[Condition, ...]

    def matches(self, features: Mapping[str, Any]) -> bool:
        return all(c.matches(features) for c in self.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {"conditions": [c.to_dict() for c in self.conditions], "expr": str(self)}

    def __str__(self) -> str:
        return " AND ".join(str(c) for c in self.conditions)


@dataclass
class TradeRow:
    trade_date: date
    features: dict[str, Any]
    labels: dict[str, Any]

    @property
    def r(self) -> float:
        try:
            return float(self.labels.get("pnl_r_net") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def won(self) -> bool:
        return str(self.labels.get("outcome") or "").strip().upper() == "WIN"


@dataclass
class WindowStats:
    window: str
    n: int
    r_per_trade: float
    win_rate: float
    first_half_r: float | None
    second_half_r: float | None
    first_half_n: int
    second_half_n: int
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    passes: bool = False


# ── Logging (stdout only — never touch /data) ────────────────────────────────


def _log(msg: str, *, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


def _parse_date(s: Any) -> date | None:
    if s is None:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None


def window_name_for(d: date) -> str:
    if d < BUILD_END:
        return WINDOW_BUILD
    if d < TIER1_END:
        return WINDOW_TIER1
    return WINDOW_SEALED


def _forbid_data_write(path: Path | None) -> None:
    """Refuse any output path under /data (Railway volume)."""
    if path is None:
        return
    resolved = path.resolve()
    data_root = Path("/data").resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError:
        return
    raise SystemExit(
        f"REFUSED: output path {resolved} is under /data — "
        f"pattern_mining must never write to the Railway volume"
    )


# ── Load ─────────────────────────────────────────────────────────────────────


def iter_entry_feature_records(paths: Sequence[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        if not path.is_file():
            continue
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    _log(f"skip {path}:{line_no} JSON error: {e}", level="WARN")
                    continue
                if isinstance(obj, dict):
                    yield obj


def resolve_input_paths(input_arg: str) -> list[Path]:
    """Accept a directory, a glob, or a single JSONL file."""
    raw = Path(input_arg)
    if raw.is_dir():
        return sorted(raw.glob("entry_features_*.jsonl"))
    if any(ch in input_arg for ch in "*?["):
        return sorted(Path().glob(input_arg))
    if raw.is_file():
        return [raw]
    # fallback: treat as glob from CWD
    return sorted(Path().glob(input_arg))


def load_trades(
    paths: Sequence[Path],
    *,
    allow_sealed: bool,
) -> dict[str, list[TradeRow]]:
    """
    Load trades into BUILD / TIER1 / (optionally) SEALED buckets.

    SEALED rows are discarded unless ``allow_sealed`` is True. Peeking without
    ``--unseal`` is refused at the call site; this function also hard-skips.
    """
    buckets: dict[str, list[TradeRow]] = {
        WINDOW_BUILD: [],
        WINDOW_TIER1: [],
        WINDOW_SEALED: [],
    }
    sealed_seen = 0
    for obj in iter_entry_feature_records(paths):
        d = _parse_date(obj.get("date"))
        if d is None:
            continue
        feats = obj.get("features")
        labs = obj.get("labels")
        if not isinstance(feats, dict) or not isinstance(labs, dict):
            continue
        # Guard: labels must not appear inside features.
        leak = set(feats.keys()) & set(ENTRY_LABEL_KEYS)
        if leak:
            _log(f"skip row {d}: label keys in features {sorted(leak)}", level="WARN")
            continue
        w = window_name_for(d)
        if w == WINDOW_SEALED:
            sealed_seen += 1
            if not allow_sealed:
                continue
        buckets[w].append(TradeRow(trade_date=d, features=dict(feats), labels=dict(labs)))

    for w in buckets:
        buckets[w].sort(key=lambda t: t.trade_date)

    _log(
        f"loaded BUILD={len(buckets[WINDOW_BUILD])} TIER1={len(buckets[WINDOW_TIER1])} "
        f"SEALED_on_disk={sealed_seen} "
        f"(sealed_loaded={len(buckets[WINDOW_SEALED]) if allow_sealed else 0}, "
        f"unseal={allow_sealed})"
    )
    if sealed_seen and not allow_sealed:
        _log(
            f"SEALED window has {sealed_seen} trades on disk — NOT LOADED. "
            f"Pass --unseal to read them (one-shot, end of analysis only).",
            level="WARN",
        )
    return buckets


# ── Stats / thresholds ───────────────────────────────────────────────────────


def _half_split(trades: Sequence[TradeRow]) -> tuple[list[TradeRow], list[TradeRow]]:
    if not trades:
        return [], []
    mid = len(trades) // 2
    return list(trades[:mid]), list(trades[mid:])


def _r_per_trade(trades: Sequence[TradeRow]) -> float:
    if not trades:
        return 0.0
    return sum(t.r for t in trades) / len(trades)


def _win_rate(trades: Sequence[TradeRow]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.won) / len(trades)


def _equity_curve(trades: Sequence[TradeRow]) -> list[dict[str, Any]]:
    cum = 0.0
    out: list[dict[str, Any]] = []
    for t in trades:
        cum += t.r
        out.append({"date": t.trade_date.isoformat(), "cum_r": round(cum, 6)})
    return out


def evaluate_window(window: str, trades: Sequence[TradeRow]) -> WindowStats:
    n = len(trades)
    r_pt = _r_per_trade(trades)
    wr = _win_rate(trades)
    h1, h2 = _half_split(trades)
    r1 = _r_per_trade(h1) if h1 else None
    r2 = _r_per_trade(h2) if h2 else None
    passes = (
        n >= MIN_TRADES_PER_WINDOW
        and r_pt >= MIN_R_PER_TRADE
        and r1 is not None
        and r2 is not None
        and r1 > 0.0
        and r2 > 0.0
    )
    return WindowStats(
        window=window,
        n=n,
        r_per_trade=round(r_pt, 6),
        win_rate=round(wr, 6),
        first_half_r=None if r1 is None else round(r1, 6),
        second_half_r=None if r2 is None else round(r2, 6),
        first_half_n=len(h1),
        second_half_n=len(h2),
        equity_curve=_equity_curve(trades),
        passes=passes,
    )


def select_trades(candidate: Candidate, trades: Sequence[TradeRow]) -> list[TradeRow]:
    return [t for t in trades if candidate.matches(t.features)]


# ── Candidate generation (BUILD only) ────────────────────────────────────────


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    w = pos - lo
    return float(sorted_vals[lo]) * (1.0 - w) + float(sorted_vals[hi]) * w


def atomic_conditions_from_build(build: Sequence[TradeRow]) -> list[Condition]:
    """Propose atomic predicates from BUILD feature marginals only."""
    atoms: list[Condition] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(cond: Condition) -> None:
        key = (cond.field, cond.op, repr(cond.value))
        if key in seen:
            return
        seen.add(key)
        atoms.append(cond)

    for field_name in MINE_NUMERIC:
        vals: list[float] = []
        for t in build:
            raw = t.features.get(field_name)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                vals.append(v)
        if len(vals) < MIN_TRADES_PER_WINDOW:
            continue
        vals.sort()
        for q in NUMERIC_QUANTILES:
            cut = round(_quantile(vals, q), 6)
            if not math.isfinite(cut):
                continue
            _add(Condition(field_name, "<", cut))
            _add(Condition(field_name, ">", cut))

    for field_name in MINE_CATEGORICAL:
        counts: dict[str, int] = {}
        for t in build:
            raw = t.features.get(field_name)
            if raw is None:
                continue
            key = str(raw).strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        for val, n in counts.items():
            if n >= MIN_TRADES_PER_WINDOW:
                _add(Condition(field_name, "==", val))

    return atoms


def generate_candidates(
    atoms: Sequence[Condition],
    build: Sequence[TradeRow],
    *,
    max_depth: int = MAX_CONJUNCTION_DEPTH,
) -> list[Candidate]:
    """
    All conjunctions of depth 1..max_depth whose BUILD support ≥ min trades.

    Depth is hard-capped at three — deeper conjunctions almost always overfit.
    """
    depth = max(1, min(int(max_depth), MAX_CONJUNCTION_DEPTH))
    # Pre-filter atoms that alone have enough support (speeds pairs/triples).
    viable_atoms: list[Condition] = []
    for atom in atoms:
        n = sum(1 for t in build if atom.matches(t.features))
        if n >= MIN_TRADES_PER_WINDOW:
            viable_atoms.append(atom)

    candidates: list[Candidate] = []
    for k in range(1, depth + 1):
        for combo in itertools.combinations(viable_atoms, k):
            # Skip contradictory same-field pairs (a < 1 AND a > 2 etc. still ok;
            # identical field with == different values is wasted).
            fields = [c.field for c in combo]
            eqs = [c for c in combo if c.op == "=="]
            if len({(c.field, str(c.value).upper()) for c in eqs}) < len(eqs):
                continue
            # Two == on same field with different values → empty.
            eq_fields = [c.field for c in eqs]
            if len(eq_fields) != len(set(eq_fields)):
                continue
            cand = Candidate(conditions=tuple(combo))
            n = sum(1 for t in build if cand.matches(t.features))
            if n >= MIN_TRADES_PER_WINDOW:
                candidates.append(cand)
    return candidates


# ── Search / confirm ─────────────────────────────────────────────────────────


def search_build(
    candidates: Sequence[Candidate],
    build: Sequence[TradeRow],
) -> list[tuple[Candidate, WindowStats]]:
    survivors: list[tuple[Candidate, WindowStats]] = []
    for cand in candidates:
        matched = select_trades(cand, build)
        stats = evaluate_window(WINDOW_BUILD, matched)
        if stats.passes:
            survivors.append((cand, stats))
    return survivors


def confirm_tier1(
    build_survivors: Sequence[tuple[Candidate, WindowStats]],
    tier1: Sequence[TradeRow],
) -> list[tuple[Candidate, WindowStats, WindowStats]]:
    confirmed: list[tuple[Candidate, WindowStats, WindowStats]] = []
    for cand, build_stats in build_survivors:
        matched = select_trades(cand, tier1)
        tier_stats = evaluate_window(WINDOW_TIER1, matched)
        if tier_stats.passes:
            confirmed.append((cand, build_stats, tier_stats))
    # Sort by TIER1 performance (R/trade), not BUILD.
    confirmed.sort(key=lambda x: x[2].r_per_trade, reverse=True)
    return confirmed


def evaluate_sealed(
    confirmed: Sequence[tuple[Candidate, WindowStats, WindowStats]],
    sealed: Sequence[TradeRow],
) -> list[dict[str, Any]]:
    """One-shot SEALED evaluation — call only after --unseal."""
    out: list[dict[str, Any]] = []
    for cand, build_stats, tier_stats in confirmed:
        matched = select_trades(cand, sealed)
        sealed_stats = evaluate_window(WINDOW_SEALED, matched)
        out.append(
            {
                "candidate": cand.to_dict(),
                "BUILD": _stats_dict(build_stats),
                "TIER1": _stats_dict(tier_stats),
                "SEALED": _stats_dict(sealed_stats),
            }
        )
    return out


def _stats_dict(st: WindowStats) -> dict[str, Any]:
    return {
        "window": st.window,
        "n": st.n,
        "r_per_trade": st.r_per_trade,
        "win_rate": st.win_rate,
        "first_half_r": st.first_half_r,
        "second_half_r": st.second_half_r,
        "first_half_n": st.first_half_n,
        "second_half_n": st.second_half_n,
        "passes_thresholds": st.passes,
        "equity_curve": st.equity_curve,
    }


# ── Reporting ────────────────────────────────────────────────────────────────


def format_report_header(n_candidates: int) -> str:
    expected_fp = n_candidates * FALSE_POSITIVE_RATE
    return (
        f"=== PATTERN MINING REPORT ===\n"
        f"Windows: BUILD < {BUILD_END.isoformat()} | "
        f"TIER1 [{BUILD_END.isoformat()}, {TIER1_END.isoformat()}) | "
        f"SEALED >= {TIER1_END.isoformat()}\n"
        f"Thresholds (pre-registered): min_trades={MIN_TRADES_PER_WINDOW}, "
        f"min_r_per_trade={MIN_R_PER_TRADE}, both halves of each window must be > 0 R\n"
        f"Candidates tested: {n_candidates}\n"
        f"Expected false positives at α={FALSE_POSITIVE_RATE}: "
        f"~{expected_fp:.1f}  "
        f"(n_candidates * {FALSE_POSITIVE_RATE})\n"
        f"Max conjunction depth: {MAX_CONJUNCTION_DEPTH}\n"
        f"Sorted by: TIER1 r_per_trade (NOT BUILD)\n"
    )


def format_survivor(
    idx: int,
    cand: Candidate,
    build_stats: WindowStats,
    tier_stats: WindowStats,
    sealed_stats: WindowStats | None = None,
) -> str:
    lines = [
        f"--- survivor #{idx} ---",
        f"condition: {cand}",
        (
            f"BUILD:  n={build_stats.n}  R/trade={build_stats.r_per_trade:.4f}  "
            f"WR={build_stats.win_rate:.1%}  "
            f"halves=({build_stats.first_half_r}, {build_stats.second_half_r})"
        ),
        (
            f"TIER1:  n={tier_stats.n}  R/trade={tier_stats.r_per_trade:.4f}  "
            f"WR={tier_stats.win_rate:.1%}  "
            f"halves=({tier_stats.first_half_r}, {tier_stats.second_half_r})"
        ),
    ]
    if sealed_stats is not None:
        lines.append(
            f"SEALED: n={sealed_stats.n}  R/trade={sealed_stats.r_per_trade:.4f}  "
            f"WR={sealed_stats.win_rate:.1%}  "
            f"halves=({sealed_stats.first_half_r}, {sealed_stats.second_half_r})  "
            f"passes={sealed_stats.passes}"
        )
    # Compact equity endpoints
    if tier_stats.equity_curve:
        lines.append(
            f"TIER1 equity: start_date={tier_stats.equity_curve[0]['date']}  "
            f"end_cum_r={tier_stats.equity_curve[-1]['cum_r']}"
        )
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def run_mining(
    *,
    input_arg: str,
    unseal: bool = False,
    output: Path | None = None,
    max_depth: int = MAX_CONJUNCTION_DEPTH,
) -> dict[str, Any]:
    if unseal:
        _log(
            "***** --unseal PASSED: SEALED window WILL BE READ. "
            "This must be the FINAL step of analysis. *****",
            level="WARN",
        )

    _forbid_data_write(output)

    paths = resolve_input_paths(input_arg)
    if not paths:
        raise SystemExit(f"No entry_features_*.jsonl files found for input={input_arg!r}")

    buckets = load_trades(paths, allow_sealed=unseal)
    build = buckets[WINDOW_BUILD]
    tier1 = buckets[WINDOW_TIER1]
    sealed = buckets[WINDOW_SEALED]

    if len(build) < MIN_TRADES_PER_WINDOW:
        raise SystemExit(
            f"BUILD has only {len(build)} trades (need ≥ {MIN_TRADES_PER_WINDOW})"
        )

    atoms = atomic_conditions_from_build(build)
    candidates = generate_candidates(atoms, build, max_depth=max_depth)
    n_cand = len(candidates)

    header = format_report_header(n_cand)
    print(header)

    build_hits = search_build(candidates, build)
    _log(f"BUILD survivors (pre-TIER1): {len(build_hits)}")

    confirmed = confirm_tier1(build_hits, tier1)
    _log(f"TIER1-confirmed survivors: {len(confirmed)}")

    print(
        f"\nSurvivors after BUILD+TIER1: {len(confirmed)}  "
        f"(remember: expect ~{n_cand * FALSE_POSITIVE_RATE:.1f} false positives "
        f"from {n_cand} candidates)\n"
    )

    sealed_results: list[dict[str, Any]] | None = None
    if unseal:
        sealed_results = evaluate_sealed(confirmed, sealed)

    for i, (cand, bstat, tstat) in enumerate(confirmed, start=1):
        sstat = None
        if sealed_results is not None:
            sstat = WindowStats(
                window=WINDOW_SEALED,
                n=int(sealed_results[i - 1]["SEALED"]["n"]),
                r_per_trade=float(sealed_results[i - 1]["SEALED"]["r_per_trade"]),
                win_rate=float(sealed_results[i - 1]["SEALED"]["win_rate"]),
                first_half_r=sealed_results[i - 1]["SEALED"]["first_half_r"],
                second_half_r=sealed_results[i - 1]["SEALED"]["second_half_r"],
                first_half_n=int(sealed_results[i - 1]["SEALED"]["first_half_n"]),
                second_half_n=int(sealed_results[i - 1]["SEALED"]["second_half_n"]),
                equity_curve=list(sealed_results[i - 1]["SEALED"]["equity_curve"]),
                passes=bool(sealed_results[i - 1]["SEALED"]["passes_thresholds"]),
            )
        print(format_survivor(i, cand, bstat, tstat, sstat))
        print()

    payload: dict[str, Any] = {
        "thresholds": {
            "min_trades_per_window": MIN_TRADES_PER_WINDOW,
            "min_r_per_trade": MIN_R_PER_TRADE,
            "must_be_positive_in": "both halves of each window",
            "false_positive_rate": FALSE_POSITIVE_RATE,
            "max_conjunction_depth": MAX_CONJUNCTION_DEPTH,
        },
        "windows": {
            "BUILD": f"< {BUILD_END.isoformat()}",
            "TIER1": f"[{BUILD_END.isoformat()}, {TIER1_END.isoformat()})",
            "SEALED": f">= {TIER1_END.isoformat()}",
        },
        "n_candidates_tested": n_cand,
        "expected_false_positives": round(n_cand * FALSE_POSITIVE_RATE, 2),
        "n_build_survivors": len(build_hits),
        "n_tier1_confirmed": len(confirmed),
        "unseal": bool(unseal),
        "survivors": [],
    }
    for cand, bstat, tstat in confirmed:
        row = {
            "candidate": cand.to_dict(),
            "BUILD": _stats_dict(bstat),
            "TIER1": _stats_dict(tstat),
        }
        payload["survivors"].append(row)
    if sealed_results is not None:
        for i, row in enumerate(payload["survivors"]):
            row["SEALED"] = sealed_results[i]["SEALED"]

    if output is not None:
        _forbid_data_write(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        _log(f"wrote report to {output}")

    return payload


def main(argv: Sequence[str] | None = None) -> int:
    # Refuse being imported as part of the live/chrono stack by accident.
    if "continuous_backtester" in sys.modules:
        _log(
            "continuous_backtester is already imported in this process — "
            "pattern_mining is offline-only; refuse to run inside that stack",
            level="ERROR",
        )
        return 2

    parser = argparse.ArgumentParser(
        description=(
            "Offline pattern mining on entry_features JSONL. "
            "SEALED window is locked unless --unseal is passed."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Directory of entry_features_*.jsonl, a glob, or a single file",
    )
    parser.add_argument(
        "--unseal",
        action="store_true",
        help="EXPLICIT: allow reading the SEALED window (final step only)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON report path (must NOT be under /data)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=MAX_CONJUNCTION_DEPTH,
        help=f"Conjunction depth cap (max {MAX_CONJUNCTION_DEPTH})",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if int(args.max_depth) > MAX_CONJUNCTION_DEPTH:
        _log(
            f"--max-depth {args.max_depth} capped to {MAX_CONJUNCTION_DEPTH} "
            f"(deeper conjunctions are refused)",
            level="WARN",
        )

    run_mining(
        input_arg=str(args.input),
        unseal=bool(args.unseal),
        output=args.output,
        max_depth=min(int(args.max_depth), MAX_CONJUNCTION_DEPTH),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
