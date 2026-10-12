"""
Server-side chrono analysis — compact exports that stay on Railway.

Reads a completed (or in-progress) chrono job's main JSON + optional sidecar
JSONL files and writes small result files under DATA_DIR. Never requires
shipping the full run file off the volume.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from utils import DATA_DIR, load_json, log, save_json, utcnow_iso

# FTMO Challenge Phase-1 style rules (static overall DD from start balance).
FTMO_PHASE1 = {
    "name": "FTMO Phase-1",
    "profit_target_pct": 10.0,
    "daily_loss_pct": 5.0,
    "max_drawdown_pct": 10.0,
    "trailing_drawdown": False,
}

# Decision-skip reason markers (aligned with Group D virtual-skip classifier).
_DECISION_SKIP_MARKERS = (
    "[BAD PERIOD]",
    "[ST_BAD_LONG_BLOCK]",
    "COUNTER-TREND",
    "TREND BLOCK",
    "[TREND BLOCK]",
    "[STALE_TAILWIND_VETO]",
    "[NEUTRAL+RANGING SKIP]",
    "[AB_THROTTLE_SKIP]",
    "[FILTER SKIP]",
    "[PRE_EVENT_24H]",
)


def analysis_dir(job_id: str) -> Path:
    d = DATA_DIR / f"chrono_analysis_{job_id.strip()}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def analysis_manifest_path(job_id: str) -> Path:
    return analysis_dir(job_id) / "manifest.json"


def _chrono_results_path(job_id: str) -> Path:
    return DATA_DIR / f"chrono_results_{job_id.strip()}.json"


def _sidecar_trades_path(job_id: str) -> Path:
    return DATA_DIR / f"chrono_trades_{job_id.strip()}.jsonl"


def _sidecar_skips_path(job_id: str) -> Path:
    return DATA_DIR / f"chrono_skips_{job_id.strip()}.jsonl"


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def load_chrono_job_for_analysis(job_id: str) -> dict[str, Any] | None:
    """
    Load chrono job metadata + trades/skips from embedded arrays and/or sidecars.

    Prefer sidecars when present (Group B); fall back to embedded ``all_trades`` /
    ``skipped`` on older runs.
    """
    jid = job_id.strip()
    path = _chrono_results_path(jid)
    if not path.is_file():
        return None
    data = load_json(path, default=None)
    if not isinstance(data, dict):
        return None

    trades_path = _sidecar_trades_path(jid)
    skips_path = _sidecar_skips_path(jid)
    if trades_path.is_file():
        trades = list(_iter_jsonl(trades_path))
    else:
        trades = [x for x in (data.get("all_trades") or []) if isinstance(x, dict)]
    if skips_path.is_file():
        skips = list(_iter_jsonl(skips_path))
    else:
        skips = [x for x in (data.get("skipped") or []) if isinstance(x, dict)]

    out = dict(data)
    out["all_trades"] = trades
    out["skipped"] = skips
    out["_analysis_sources"] = {
        "main": str(path),
        "trades": str(trades_path) if trades_path.is_file() else "embedded",
        "skips": str(skips_path) if skips_path.is_file() else "embedded",
        "n_trades": len(trades),
        "n_skips": len(skips),
    }
    return out


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        return None


def _quarter_key(d: date | str) -> str:
    if isinstance(d, str):
        dd = _parse_date(d)
        if dd is None:
            return "unknown"
        d = dd
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def daily_returns_from_pnl(daily_pnl: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalise daily_pnl into a sorted return series."""
    rows: list[dict[str, Any]] = []
    for r in daily_pnl:
        if not isinstance(r, Mapping):
            continue
        ds = str(r.get("date") or "")[:10]
        if not ds:
            continue
        try:
            pnl = float(r.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        try:
            capital = float(r.get("capital", 0) or 0)
        except (TypeError, ValueError):
            capital = 0.0
        try:
            trades = int(r.get("trades", 0) or 0)
        except (TypeError, ValueError):
            trades = 0
        rows.append(
            {
                "date": ds,
                "pnl": round(pnl, 4),
                "capital": round(capital, 2),
                "trades": trades,
                "wins": int(r.get("wins", 0) or 0),
                "losses": int(r.get("losses", 0) or 0),
            }
        )
    rows.sort(key=lambda x: x["date"])
    return rows


def _run_ftmo_from_day(
    series: list[dict[str, Any]],
    start_idx: int,
    *,
    starting_balance: float,
    rules: Mapping[str, Any],
) -> dict[str, Any]:
    """Walk forward from ``start_idx`` applying Phase-1 style rules on daily PnL."""
    profit_target_pct = float(rules.get("profit_target_pct") or 10.0)
    daily_loss_pct = float(rules.get("daily_loss_pct") or 5.0)
    max_dd_pct = float(rules.get("max_drawdown_pct") or 10.0)
    trailing = bool(rules.get("trailing_drawdown"))

    capital = float(starting_balance)
    peak = float(starting_balance)
    target = starting_balance * (1.0 + profit_target_pct / 100.0)
    static_floor = starting_balance * (1.0 - max_dd_pct / 100.0)
    status = "running"
    fail_reason = ""
    pass_date = ""
    days = 0
    start_date = series[start_idx]["date"] if start_idx < len(series) else ""

    for i in range(start_idx, len(series)):
        row = series[i]
        days += 1
        day_anchor = capital
        pnl = float(row["pnl"])
        # Scale daily dollar PnL from the chrono curve to this sim's capital base.
        # Chrono series already embeds compounding; use relative day return vs prior capital.
        prior_cap = float(row.get("capital") or 0) - pnl
        if prior_cap > 0 and math.isfinite(prior_cap):
            day_ret = pnl / prior_cap
            day_pnl = day_anchor * day_ret
        else:
            day_pnl = pnl * (capital / 10_000.0) if capital else 0.0

        capital += day_pnl
        if capital > peak:
            peak = capital
        day_loss = max(0.0, -day_pnl)
        daily_limit = day_anchor * (daily_loss_pct / 100.0)

        if day_loss >= daily_limit - 1e-9 and day_loss > 0:
            status = "failed"
            fail_reason = "daily_loss"
            break
        floor = peak * (1.0 - max_dd_pct / 100.0) if trailing else static_floor
        if capital <= floor + 1e-9:
            status = "failed"
            fail_reason = "max_drawdown"
            break
        if capital >= target - 1e-9:
            status = "passed"
            pass_date = str(row["date"])
            break

    if status == "running":
        status = "incomplete"
        fail_reason = "window_ended"

    return {
        "start_date": start_date,
        "result": status,
        "fail_reason": fail_reason,
        "pass_date": pass_date,
        "days_taken": days,
        "end_capital": round(capital, 2),
        "peak_capital": round(peak, 2),
    }


def analyze_funded_challenge(
    daily_pnl: list[Mapping[str, Any]],
    *,
    starting_balance: float = 10_000.0,
    rules: Mapping[str, Any] | None = None,
    step_days: int = 5,
) -> dict[str, Any]:
    """
    Rolling-start FTMO Phase-1 style sim on the daily return series.

    Starts every ``step_days`` trading days; returns pass rate, fail reasons,
    median days-to-pass, and per-quarter aggregates. Compact — no per-day logs.
    """
    rules = dict(rules or FTMO_PHASE1)
    series = daily_returns_from_pnl(list(daily_pnl))
    if len(series) < 5:
        return {
            "rules": rules,
            "starting_balance": starting_balance,
            "n_sims": 0,
            "pass_rate_pct": None,
            "fail_reasons": {},
            "median_days_to_pass": None,
            "by_quarter": {},
            "note": "insufficient daily_pnl",
        }

    sims: list[dict[str, Any]] = []
    for i in range(0, len(series), max(1, int(step_days))):
        # Need enough runway ahead; still run short tails as incomplete.
        sims.append(
            _run_ftmo_from_day(
                series,
                i,
                starting_balance=starting_balance,
                rules=rules,
            )
        )

    passed = [s for s in sims if s.get("result") == "passed"]
    failed = [s for s in sims if s.get("result") == "failed"]
    fail_reasons: dict[str, int] = defaultdict(int)
    for s in failed:
        fail_reasons[str(s.get("fail_reason") or "unknown")] += 1

    days_pass = [int(s.get("days_taken") or 0) for s in passed if s.get("days_taken")]
    median_days = float(statistics.median(days_pass)) if days_pass else None

    by_q: dict[str, dict[str, Any]] = {}
    for s in sims:
        q = _quarter_key(str(s.get("start_date") or ""))
        bucket = by_q.setdefault(
            q,
            {"sims": 0, "passed": 0, "failed": 0, "incomplete": 0, "days_to_pass": []},
        )
        bucket["sims"] += 1
        res = str(s.get("result") or "")
        if res == "passed":
            bucket["passed"] += 1
            bucket["days_to_pass"].append(int(s.get("days_taken") or 0))
        elif res == "failed":
            bucket["failed"] += 1
        else:
            bucket["incomplete"] += 1

    by_quarter_out: dict[str, Any] = {}
    for q, b in sorted(by_q.items()):
        n = int(b["sims"])
        by_quarter_out[q] = {
            "sims": n,
            "passed": int(b["passed"]),
            "failed": int(b["failed"]),
            "incomplete": int(b["incomplete"]),
            "pass_rate_pct": round(100.0 * b["passed"] / n, 2) if n else None,
            "median_days_to_pass": (
                float(statistics.median(b["days_to_pass"])) if b["days_to_pass"] else None
            ),
        }

    n = len(sims)
    return {
        "rules": rules,
        "starting_balance": starting_balance,
        "step_days": int(step_days),
        "n_sims": n,
        "passed": len(passed),
        "failed": len(failed),
        "incomplete": n - len(passed) - len(failed),
        "pass_rate_pct": round(100.0 * len(passed) / n, 2) if n else None,
        "fail_reasons": dict(sorted(fail_reasons.items(), key=lambda kv: -kv[1])),
        "median_days_to_pass": median_days,
        "by_quarter": by_quarter_out,
    }


def _is_decision_skip(reason: str) -> bool:
    r = str(reason or "").strip().upper()
    if not r:
        return False
    if "[BLOCKED PAIR]" in r or "BLOCKED_PAIRS" in r:
        return False
    if any(
        tok in r
        for tok in (
            "INSUFFICIENT",
            "NO DATA",
            "NO_DATA",
            "OHLC",
            "API",
            "FORWARD SIMULATION FAILED",
            "FORWARD WINDOW",
            "PAST BARS",
            "EXECUTION ABORT",
            "SCAN PRODUCED NO RESULT",
            "BACKTEST ERROR",
        )
    ):
        return False
    return any(tok in r for tok in _DECISION_SKIP_MARKERS)


def analyze_skip_mining(skips: list[Mapping[str, Any]]) -> dict[str, Any]:
    """
    Genuine decision-skips with virtual outcomes, grouped by skip_reason + context.

    Rows without virtual fields are still counted if they look like decision skips
    (pre–Group D runs); win-rate uses only rows that carry virtual_outcome.
    """
    groups: dict[str, dict[str, Any]] = {}
    n_decision = 0
    n_with_virtual = 0

    for sk in skips:
        if not isinstance(sk, Mapping):
            continue
        reason = str(sk.get("skip_reason") or "")[:200]
        if not _is_decision_skip(reason):
            continue
        n_decision += 1
        # Group key: normalized reason prefix + direction + timeframe + period_mode
        reason_key = reason.split("—")[0].split(":")[0].strip()[:80]
        direction = str(sk.get("direction") or "NONE").upper()
        tf = str(sk.get("timeframe") or "").lower()
        period = str(sk.get("period_mode") or "").upper() or "NA"
        mb = str(sk.get("macro_bias") or "").upper() or "NA"
        gkey = f"{reason_key}|{direction}|{tf}|{period}|{mb}"

        g = groups.setdefault(
            gkey,
            {
                "skip_reason": reason_key,
                "direction": direction,
                "timeframe": tf,
                "period_mode": period,
                "macro_bias": mb,
                "n": 0,
                "n_virtual": 0,
                "virtual_wins": 0,
                "virtual_losses": 0,
                "sum_virtual_pnl_r": 0.0,
                "sample_tickers": [],
            },
        )
        g["n"] += 1
        tickers = g["sample_tickers"]
        tku = str(sk.get("ticker") or "").upper()
        if tku and tku not in tickers and len(tickers) < 8:
            tickers.append(tku)

        vo = str(sk.get("virtual_outcome") or "").upper()
        if vo in ("WIN", "LOSS"):
            n_with_virtual += 1
            g["n_virtual"] += 1
            if vo == "WIN":
                g["virtual_wins"] += 1
            else:
                g["virtual_losses"] += 1
            try:
                g["sum_virtual_pnl_r"] += float(sk.get("virtual_pnl_r") or 0)
            except (TypeError, ValueError):
                pass

    out_groups: list[dict[str, Any]] = []
    for g in groups.values():
        nv = int(g["n_virtual"])
        wr = round(100.0 * g["virtual_wins"] / nv, 2) if nv else None
        out_groups.append(
            {
                "skip_reason": g["skip_reason"],
                "direction": g["direction"],
                "timeframe": g["timeframe"],
                "period_mode": g["period_mode"],
                "macro_bias": g["macro_bias"],
                "n": g["n"],
                "n_virtual": nv,
                "virtual_win_rate_pct": wr,
                "avg_virtual_pnl_r": round(g["sum_virtual_pnl_r"] / nv, 4) if nv else None,
                "sample_tickers": g["sample_tickers"],
            }
        )
    out_groups.sort(key=lambda x: (-int(x["n"]), str(x["skip_reason"])))

    return {
        "n_skips_total": len(skips),
        "n_decision_skips": n_decision,
        "n_with_virtual_outcome": n_with_virtual,
        "groups": out_groups,
    }


def analyze_period_breakdown(
    daily_pnl: list[Mapping[str, Any]],
    trades: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Per-quarter equity, trade activity, win rate."""
    series = daily_returns_from_pnl(list(daily_pnl))
    q_daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in series:
        q_daily[_quarter_key(r["date"])].append(r)

    closed = [
        t
        for t in trades
        if isinstance(t, Mapping)
        and not t.get("skipped")
        and str(t.get("outcome") or "").upper() in ("WIN", "LOSS")
    ]
    q_trades: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in closed:
        q_trades[_quarter_key(str(t.get("date") or ""))].append(t)

    quarters: dict[str, Any] = {}
    for q in sorted(set(q_daily) | set(q_trades)):
        days = q_daily.get(q) or []
        tr = q_trades.get(q) or []
        wins = sum(1 for t in tr if str(t.get("outcome")).upper() == "WIN")
        losses = len(tr) - wins
        pnl_sum = sum(float(t.get("pnl_dollars") or 0) for t in tr)
        start_cap = float(days[0]["capital"]) - float(days[0]["pnl"]) if days else None
        end_cap = float(days[-1]["capital"]) if days else None
        quarters[q] = {
            "trading_days": len(days),
            "trades": len(tr),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / len(tr), 2) if tr else None,
            "pnl_dollars": round(pnl_sum, 2),
            "start_capital": round(start_cap, 2) if start_cap is not None else None,
            "end_capital": round(end_cap, 2) if end_cap is not None else None,
            "equity_change_pct": (
                round(100.0 * (end_cap - start_cap) / start_cap, 2)
                if start_cap and start_cap > 0 and end_cap is not None
                else None
            ),
        }

    return {"quarters": quarters, "n_closed_trades": len(closed)}


def _slice_stats(rows: list[Mapping[str, Any]], label: str) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"slice": label, "n": 0, "win_rate_pct": None, "pnl_dollars": 0.0, "avg_pnl": None}
    wins = sum(1 for r in rows if str(r.get("outcome")).upper() == "WIN")
    pnl = sum(float(r.get("pnl_dollars") or 0) for r in rows)
    return {
        "slice": label,
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(100.0 * wins / n, 2),
        "pnl_dollars": round(pnl, 2),
        "avg_pnl": round(pnl / n, 2),
    }


def analyze_slice_stats(trades: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Standard consistency slices: shorts, throttle, timeframe, confidence, etc."""
    closed = [
        t
        for t in trades
        if isinstance(t, Mapping)
        and not t.get("skipped")
        and str(t.get("outcome") or "").upper() in ("WIN", "LOSS")
    ]
    slices: list[dict[str, Any]] = []

    # Direction
    for d in ("LONG", "SHORT"):
        slices.append(_slice_stats([t for t in closed if str(t.get("direction")).upper() == d], f"dir:{d}"))

    # Throttle
    throttled = []
    unthrottled = []
    for t in closed:
        try:
            thr = float(t.get("ab_throttle") or 1.0)
        except (TypeError, ValueError):
            thr = 1.0
        if thr < 1.0 - 1e-9:
            throttled.append(t)
        else:
            unthrottled.append(t)
    slices.append(_slice_stats(throttled, "throttle:throttled"))
    slices.append(_slice_stats(unthrottled, "throttle:full_size"))

    # Timeframe
    by_tf: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in closed:
        by_tf[str(t.get("timeframe") or "?").lower()].append(t)
    for tf, rows in sorted(by_tf.items()):
        slices.append(_slice_stats(list(rows), f"tf:{tf}"))

    # Confidence
    by_conf: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in closed:
        by_conf[str(t.get("confidence") or "?").upper()].append(t)
    for c, rows in sorted(by_conf.items()):
        slices.append(_slice_stats(list(rows), f"confidence:{c}"))

    # Macro bias
    by_mb: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in closed:
        by_mb[str(t.get("macro_bias") or "?").upper()].append(t)
    for mb, rows in sorted(by_mb.items()):
        slices.append(_slice_stats(list(rows), f"macro:{mb}"))

    # Period mode
    by_pm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in closed:
        by_pm[str(t.get("period_mode") or "?").upper()].append(t)
    for pm, rows in sorted(by_pm.items()):
        slices.append(_slice_stats(list(rows), f"period:{pm}"))

    return {"n_closed_trades": len(closed), "slices": slices}


def run_chrono_analysis(
    job_id: str,
    *,
    starting_balance: float | None = None,
    ftmo_step_days: int = 5,
) -> dict[str, Any]:
    """
    Run all compact analyses for ``job_id`` and write result files.

    Returns the manifest (paths + top-level stats). Raises ``FileNotFoundError``
    if the job is missing.
    """
    jid = job_id.strip()
    job = load_chrono_job_for_analysis(jid)
    if job is None:
        raise FileNotFoundError(f"chrono job not found: {jid}")

    daily = list(job.get("daily_pnl") or [])
    trades = list(job.get("all_trades") or [])
    skips = list(job.get("skipped") or [])
    try:
        bal = float(starting_balance if starting_balance is not None else job.get("capital") or 10_000.0)
    except (TypeError, ValueError):
        bal = 10_000.0
    # Use start capital for challenge sims when available.
    if daily:
        try:
            first = daily[0]
            start_cap = float(first.get("capital") or 0) - float(first.get("pnl") or 0)
            if start_cap > 0:
                bal = start_cap
        except (TypeError, ValueError, IndexError, AttributeError):
            pass

    out_dir = analysis_dir(jid)
    funded = analyze_funded_challenge(daily, starting_balance=bal, step_days=ftmo_step_days)
    skips_out = analyze_skip_mining(skips)
    periods = analyze_period_breakdown(daily, trades)
    slices = analyze_slice_stats(trades)

    files = {
        "funded_challenge": out_dir / "funded_challenge.json",
        "skip_mining": out_dir / "skip_mining.json",
        "period_breakdown": out_dir / "period_breakdown.json",
        "slice_stats": out_dir / "slice_stats.json",
    }
    save_json(files["funded_challenge"], funded)
    save_json(files["skip_mining"], skips_out)
    save_json(files["period_breakdown"], periods)
    save_json(files["slice_stats"], slices)

    manifest: dict[str, Any] = {
        "job_id": jid,
        "generated_at": utcnow_iso(),
        "job_status": job.get("status"),
        "start_date": job.get("start_date"),
        "end_date": job.get("end_date"),
        "current_date": job.get("current_date"),
        "capital": job.get("capital"),
        "sources": job.get("_analysis_sources"),
        "files": {k: str(v.name) for k, v in files.items()},
        "file_bytes": {k: int(v.stat().st_size) for k, v in files.items() if v.is_file()},
        "summary": {
            "funded_pass_rate_pct": funded.get("pass_rate_pct"),
            "funded_n_sims": funded.get("n_sims"),
            "decision_skips": skips_out.get("n_decision_skips"),
            "skips_with_virtual": skips_out.get("n_with_virtual_outcome"),
            "quarters": len((periods.get("quarters") or {})),
            "closed_trades": slices.get("n_closed_trades"),
        },
    }
    save_json(analysis_manifest_path(jid), manifest)
    log(
        f"[Chrono Analysis] job={jid} wrote {len(files)} compact files "
        f"under {out_dir} (pass_rate={funded.get('pass_rate_pct')})",
        level="info",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Server-side chrono compact analysis")
    p.add_argument("job_id", help="Chrono job id")
    p.add_argument("--step-days", type=int, default=5, help="FTMO rolling-start step")
    args = p.parse_args(argv)
    try:
        manifest = run_chrono_analysis(args.job_id, ftmo_step_days=args.step_days)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
