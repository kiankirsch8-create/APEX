"""Group F / PR14 — server-side chrono compact analysis."""
from __future__ import annotations

import json
from pathlib import Path

import chrono_analysis as ca
import continuous_backtester as cb
from utils import DATA_DIR, save_json


def _write_mini_job(job_id: str) -> Path:
    """Synthetic completed job with daily_pnl + trades + decision skips."""
    daily = []
    capital = 10_000.0
    # ~40 trading days with mild uptrend + a few drawdowns
    pnls = (
        [80.0, 40.0, -30.0, 50.0, -20.0] * 6
        + [120.0, -90.0, 60.0, 30.0, -40.0] * 2
    )
    for i, pnl in enumerate(pnls):
        capital += pnl
        d = f"2022-{(i // 20) + 1:02d}-{(i % 20) + 1:02d}"
        # Fix invalid dates simply: use sequential from 2022-01-03 weekdays-ish
        from datetime import date, timedelta

        dd = date(2022, 1, 3) + timedelta(days=i)
        daily.append(
            {
                "date": dd.isoformat(),
                "pnl": pnl,
                "trades": 1 if pnl != 0 else 0,
                "skipped": 0,
                "capital": round(capital, 2),
                "wins": 1 if pnl > 0 else 0,
                "losses": 1 if pnl < 0 else 0,
            }
        )

    trades = []
    for i, day in enumerate(daily):
        outcome = "WIN" if day["pnl"] > 0 else "LOSS"
        thr = 0.18 if i % 7 == 0 else 1.0
        trades.append(
            {
                "date": day["date"],
                "ticker": "EURUSD" if i % 2 == 0 else "GBPUSD",
                "timeframe": "1d" if i % 3 else "1w",
                "direction": "SHORT" if i % 5 == 0 else "LONG",
                "outcome": outcome,
                "pnl_dollars": day["pnl"],
                "skipped": False,
                "ab_throttle": thr,
                "confidence": "HIGH" if i % 2 else "MEDIUM",
                "macro_bias": "STRONG_TAILWIND" if i % 4 == 0 else "NEUTRAL",
                "period_mode": "BAD" if i % 9 == 0 else "NEUTRAL",
                "strategy_id": "T01",
            }
        )

    skips = [
        {
            "date": daily[0]["date"],
            "ticker": "EURUSD",
            "timeframe": "1d",
            "direction": "LONG",
            "skipped": True,
            "outcome": "SKIPPED",
            "skip_reason": "[BAD PERIOD] FRAGILE strategy T01 paused — bad period mode active",
            "virtual_outcome": "WIN",
            "virtual_pnl_r": 1.5,
            "period_mode": "BAD",
            "macro_bias": "NEUTRAL",
        },
        {
            "date": daily[1]["date"],
            "ticker": "GBPUSD",
            "timeframe": "1d",
            "direction": "SHORT",
            "skipped": True,
            "outcome": "SKIPPED",
            "skip_reason": "Counter-trend blocked: weekly uptrend",
            "virtual_outcome": "LOSS",
            "virtual_pnl_r": -1.0,
            "period_mode": "NEUTRAL",
            "macro_bias": "NEUTRAL",
        },
        {
            "date": daily[2]["date"],
            "ticker": "USDSEK",
            "timeframe": "1d",
            "direction": "NONE",
            "skipped": True,
            "outcome": "SKIPPED",
            "skip_reason": "[BLOCKED PAIR] USDSEK in BLOCKED_PAIRS list",
            # no virtual — must be excluded from mining groups as decision skip
        },
        {
            "date": daily[3]["date"],
            "ticker": "EURUSD",
            "timeframe": "1d",
            "direction": "LONG",
            "skipped": True,
            "outcome": "SKIPPED",
            "skip_reason": "Insufficient OHLC history or forward window",
        },
    ]

    path = DATA_DIR / f"chrono_results_{job_id}.json"
    save_json(
        path,
        {
            "job_id": job_id,
            "status": "complete",
            "start_date": daily[0]["date"],
            "end_date": daily[-1]["date"],
            "current_date": daily[-1]["date"],
            "capital": capital,
            "daily_pnl": daily,
            "all_trades": trades,
            "skipped": skips,
            "days_processed": len(daily),
        },
    )
    return path


def test_run_params_group_f() -> None:
    assert cb.CHRONO_START_DATE == "2018-01-01"
    assert cb.CHRONO_END_DATE.startswith("2026-")
    assert cb.COMPOUNDING_ENABLED is True
    assert "GBPAUD" in cb.EXCLUDED_PAIRS
    assert "NZDJPY" in cb.EXCLUDED_PAIRS
    assert "GBPNZD" in cb.BLOCKED_PAIRS


def test_analysis_emits_compact_files_and_matches_raw() -> None:
    jid = "anlztest"
    _write_mini_job(jid)
    manifest = ca.run_chrono_analysis(jid, ftmo_step_days=3)
    assert manifest["job_id"] == jid
    out_dir = ca.analysis_dir(jid)
    for key, fname in manifest["files"].items():
        fp = out_dir / fname
        assert fp.is_file(), fname
        assert fp.stat().st_size < 2_000_000  # compact

    job = ca.load_chrono_job_for_analysis(jid)
    assert job is not None

    # Skip mining matches direct count of decision skips with virtuals
    skips = job["skipped"]
    mining = json.loads((out_dir / "skip_mining.json").read_text())
    assert mining["n_decision_skips"] == 2  # BAD + counter-trend; not blocked/no-data
    assert mining["n_with_virtual_outcome"] == 2
    assert len(mining["groups"]) >= 2
    # Recompute from raw for parity
    direct = ca.analyze_skip_mining(skips)
    assert direct["n_decision_skips"] == mining["n_decision_skips"]
    assert direct["n_with_virtual_outcome"] == mining["n_with_virtual_outcome"]

    # Slice: shorts count matches raw
    closed = [t for t in job["all_trades"] if t.get("outcome") in ("WIN", "LOSS")]
    n_short = sum(1 for t in closed if t.get("direction") == "SHORT")
    slices = json.loads((out_dir / "slice_stats.json").read_text())
    short_slice = next(s for s in slices["slices"] if s["slice"] == "dir:SHORT")
    assert short_slice["n"] == n_short

    # Period quarters non-empty
    periods = json.loads((out_dir / "period_breakdown.json").read_text())
    assert periods["quarters"]
    assert periods["n_closed_trades"] == len(closed)

    # Funded sim produces pass rate
    funded = json.loads((out_dir / "funded_challenge.json").read_text())
    assert funded["n_sims"] > 0
    assert "pass_rate_pct" in funded
    assert "by_quarter" in funded
    assert "fail_reasons" in funded


def test_ftmo_direct_match_on_series() -> None:
    daily = [
        {"date": "2023-01-02", "pnl": 100.0, "capital": 10100.0, "trades": 1},
        {"date": "2023-01-03", "pnl": 100.0, "capital": 10200.0, "trades": 1},
        {"date": "2023-01-04", "pnl": -600.0, "capital": 9600.0, "trades": 1},  # ~5.9% of day start
    ]
    # From day 0 with 10k: day3 loss from 10200 is 600/10200≈5.9% > 5% daily → fail
    one = ca._run_ftmo_from_day(
        ca.daily_returns_from_pnl(daily),
        0,
        starting_balance=10_000.0,
        rules=ca.FTMO_PHASE1,
    )
    assert one["result"] == "failed"
    assert one["fail_reason"] == "daily_loss"

    # Climb to +10% without breach
    climb = []
    cap = 10_000.0
    from datetime import date, timedelta

    d0 = date(2023, 2, 1)
    for i in range(15):
        pnl = 80.0
        cap += pnl
        climb.append({"date": (d0 + timedelta(days=i)).isoformat(), "pnl": pnl, "capital": cap, "trades": 1})
    ok = ca._run_ftmo_from_day(
        ca.daily_returns_from_pnl(climb),
        0,
        starting_balance=10_000.0,
        rules=ca.FTMO_PHASE1,
    )
    assert ok["result"] == "passed"
    assert ok["days_taken"] > 0
