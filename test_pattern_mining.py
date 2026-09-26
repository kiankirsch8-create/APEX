"""Tests for offline pattern_mining harness (window seal + thresholds)."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pattern_mining as pm


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _row(
    d: str,
    *,
    zone: float,
    confluence: int,
    timeframe: str = "1d",
    r: float,
    outcome: str | None = None,
) -> dict:
    oc = outcome or ("WIN" if r > 0 else "LOSS")
    return {
        "date": d,
        "job_id": "test",
        "features": {
            "ticker": "EURUSD",
            "timeframe": timeframe,
            "strategy_id": "T01",
            "direction": "LONG",
            "zone_position_pct": zone,
            "strategy_confluence_count": confluence,
            "macro_bias": "TAILWIND",
            "confidence": "MEDIUM",
            "trend_strength": 0.5,
            "conviction_score": 5,
        },
        "labels": {
            "nights_held": 10.0 if r > 0 else 2.0,
            "pnl_r_net": r,
            "outcome": oc,
            "exit_reason": "TRAIL_STOP" if r > 0 else "STOP",
            "hit_tp1": r > 0,
            "hit_tp2": False,
            "hit_tp3": False,
            "peak_profit_dollars": 100.0 if r > 0 else 0.0,
            "candles_to_exit": 10,
            "event_held_through": 0,
        },
    }


def _dated_rows(
    start: date,
    n: int,
    *,
    zone: float,
    confluence: int,
    r: float,
    end_exclusive: date | None = None,
) -> list[dict]:
    """Emit ``n`` weekday rows starting at ``start``, staying before ``end_exclusive``."""
    out: list[dict] = []
    d = start
    while len(out) < n:
        if end_exclusive is not None and d >= end_exclusive:
            raise AssertionError(
                f"need {n} rows before {end_exclusive}, only got {len(out)} from {start}"
            )
        if d.weekday() < 5:
            out.append(_row(d.isoformat(), zone=zone, confluence=confluence, r=r))
        d += timedelta(days=1)
    return out


def test_windows_declared_and_sealed_by_default(tmp_path: Path):
    build_rows = _dated_rows(
        date(2023, 1, 2), 120, zone=20.0, confluence=2, r=0.3, end_exclusive=pm.BUILD_END
    )
    tier_rows = _dated_rows(
        date(2024, 6, 3), 120, zone=20.0, confluence=2, r=0.25, end_exclusive=pm.TIER1_END
    )
    sealed_rows = _dated_rows(
        date(2025, 6, 2), 120, zone=20.0, confluence=2, r=0.2
    )
    path = tmp_path / "entry_features_job.jsonl"
    _write_jsonl(path, build_rows + tier_rows + sealed_rows)

    buckets = pm.load_trades([path], allow_sealed=False)
    assert len(buckets["BUILD"]) == 120
    assert len(buckets["TIER1"]) == 120
    assert len(buckets["SEALED"]) == 0  # sealed refused

    buckets2 = pm.load_trades([path], allow_sealed=True)
    assert len(buckets2["SEALED"]) == 120


def test_forbid_data_write():
    try:
        pm._forbid_data_write(Path("/data/evil.json"))
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "/data" in str(e)


def test_thresholds_require_both_halves_positive():
    # 120 trades: first half +0.3R, second half -0.1R → must fail
    trades = []
    for i in range(60):
        trades.append(
            pm.TradeRow(
                trade_date=date(2023, 1, 1) + timedelta(days=i),
                features={"zone_position_pct": 10},
                labels={"pnl_r_net": 0.3, "outcome": "WIN"},
            )
        )
    for i in range(60):
        trades.append(
            pm.TradeRow(
                trade_date=date(2023, 3, 1) + timedelta(days=i),
                features={"zone_position_pct": 10},
                labels={"pnl_r_net": -0.1, "outcome": "LOSS"},
            )
        )
    st = pm.evaluate_window("BUILD", trades)
    assert st.n == 120
    assert st.r_per_trade == 0.1  # average
    assert st.passes is False  # second half not positive / min R


def test_passes_when_both_halves_and_min_r():
    trades = [
        pm.TradeRow(
            trade_date=date(2023, 1, 1) + timedelta(days=i),
            features={},
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        )
        for i in range(120)
    ]
    st = pm.evaluate_window("BUILD", trades)
    assert st.passes is True
    assert st.r_per_trade >= pm.MIN_R_PER_TRADE


def test_max_conjunction_depth_capped():
    assert pm.MAX_CONJUNCTION_DEPTH == 3
    atoms = [
        pm.Condition("zone_position_pct", "<", 30),
        pm.Condition("strategy_confluence_count", "==", 2),
        pm.Condition("timeframe", "==", "1w"),
        pm.Condition("direction", "==", "LONG"),
    ]
    # Fake build large enough that all atoms match
    build = [
        pm.TradeRow(
            trade_date=date(2023, 1, 1) + timedelta(days=i % 400),
            features={
                "zone_position_pct": 10,
                "strategy_confluence_count": 2,
                "timeframe": "1w",
                "direction": "LONG",
            },
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        )
        for i in range(150)
    ]
    cands = pm.generate_candidates(atoms, build, max_depth=5)  # request 5
    assert all(len(c.conditions) <= 3 for c in cands)
    assert any(len(c.conditions) == 3 for c in cands)


def test_mine_numeric_is_hypothesis_driven():
    assert len(pm.MINE_NUMERIC) == 9
    assert pm.MINE_NUMERIC == (
        "zone_position_pct",
        "strategy_confluence_count",
        "ab_throttle",
        "trend_strength",
        "entry_atr_vs_avg",
        "sys_followthrough_last20",
        "st_dist_to_high_atr",
        "mc_carry_to_vol",
        "cs_dispersion",
    )


def test_max_candidates_refuses_without_truncating():
    # Many viable atoms → combinations exceed a tiny ceiling
    atoms = [pm.Condition(f"f{i}", "==", "x") for i in range(20)]
    build = [
        pm.TradeRow(
            trade_date=date(2023, 1, 1) + timedelta(days=i % 400),
            features={f"f{j}": "x" for j in range(20)},
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        )
        for i in range(150)
    ]
    try:
        pm.generate_candidates(atoms, build, max_depth=3, max_candidates=10)
        assert False, "expected SystemExit"
    except SystemExit as e:
        msg = str(e)
        assert "REFUSED" in msg
        assert "10" in msg
        assert "MINE_NUMERIC" in msg or "max-depth" in msg


def test_atom_masks_precomputed_and_conjunction_and():
    atoms = [
        pm.Condition("zone_position_pct", "<", 30),
        pm.Condition("strategy_confluence_count", "==", 2),
    ]
    build = [
        pm.TradeRow(
            trade_date=date(2023, 1, 1),
            features={"zone_position_pct": 10, "strategy_confluence_count": 2},
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        ),
        pm.TradeRow(
            trade_date=date(2023, 1, 2),
            features={"zone_position_pct": 50, "strategy_confluence_count": 2},
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        ),
        pm.TradeRow(
            trade_date=date(2023, 1, 3),
            features={"zone_position_pct": 10, "strategy_confluence_count": 1},
            labels={"pnl_r_net": 0.2, "outcome": "WIN"},
        ),
    ]
    masks = pm.precompute_atom_masks(atoms, build)
    assert len(masks) == 2
    assert masks[0].tolist() == [True, False, True]
    assert masks[1].tolist() == [True, True, False]
    assert (masks[0] & masks[1]).tolist() == [True, False, False]


def test_report_header_states_expected_false_positives():
    header = pm.format_report_header(40)
    assert "Candidates tested: 40" in header
    assert "after BUILD" in header
    assert "after TIER1" in header
    assert "after SEALED" in header
    # 40 * 0.05 = 2, 40 * 0.0025 = 0.1, 40 * 0.000125 = 0.005
    assert "~2" in header
    assert "0.05^2" in header
    assert "0.05^3" in header


def test_stage_lines_compare_survivors_to_chance():
    line_b = pm.format_stage_line("BUILD", 412, 5000)
    line_t = pm.format_stage_line("TIER1", 31, 5000)
    line_s = pm.format_stage_line("SEALED", 3, 5000)
    assert "BUILD" in line_b and "412" in line_b and "expected by chance ~250" in line_b
    assert "TIER1" in line_t and "31" in line_t and "expected by chance ~12.5" in line_t
    assert "SEALED" in line_s and "3" in line_s and "expected by chance ~0.625" in line_s
    assert pm.expected_false_positives(40, "BUILD") == 2.0
    assert abs(pm.expected_false_positives(40, "TIER1") - 0.1) < 1e-12
    assert abs(pm.expected_false_positives(40, "SEALED") - 0.005) < 1e-12


def test_sorted_by_tier1_not_build():
    # Two candidates: A better on BUILD, B better on TIER1 → B first
    c_a = pm.Candidate((pm.Condition("zone_position_pct", "<", 25),))
    c_b = pm.Candidate((pm.Condition("zone_position_pct", "<", 40),))
    build_a = pm.WindowStats(
        "BUILD", 120, 0.5, 0.6, 0.5, 0.5, 60, 60, passes=True
    )
    build_b = pm.WindowStats(
        "BUILD", 120, 0.2, 0.55, 0.2, 0.2, 60, 60, passes=True
    )
    # Use confirm_tier1 with synthetic tier1 where B matches more R
    tier1 = []
    for i in range(120):
        # zone 30 → matches B (<40) not A (<25)
        tier1.append(
            pm.TradeRow(
                trade_date=date(2024, 7, 1) + timedelta(days=i),
                features={"zone_position_pct": 30.0},
                labels={"pnl_r_net": 0.25, "outcome": "WIN"},
            )
        )
    # Give A 120 matching trades with lower R:
    for i in range(120):
        tier1.append(
            pm.TradeRow(
                trade_date=date(2024, 7, 1) + timedelta(days=i),
                features={"zone_position_pct": 10.0},
                labels={"pnl_r_net": 0.16, "outcome": "WIN"},
            )
        )
    confirmed = pm.confirm_tier1([(c_a, build_a), (c_b, build_b)], tier1)
    assert len(confirmed) >= 1
    for i in range(len(confirmed) - 1):
        assert confirmed[i][2].r_per_trade >= confirmed[i + 1][2].r_per_trade


def test_does_not_import_continuous_backtester():
    """AST check: no import of continuous_backtester (docstring mentions OK)."""
    import ast

    tree = ast.parse(Path(pm.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "continuous_backtester" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            assert mod != "continuous_backtester"


def test_end_to_end_run_mining_without_unseal(tmp_path: Path):
    build_rows = _dated_rows(
        date(2023, 1, 2), 150, zone=15.0, confluence=2, r=0.3, end_exclusive=pm.BUILD_END
    )
    build_noise = _dated_rows(
        date(2023, 8, 1), 50, zone=80.0, confluence=1, r=-0.2, end_exclusive=pm.BUILD_END
    )
    tier_rows = _dated_rows(
        date(2024, 6, 3), 150, zone=15.0, confluence=2, r=0.28, end_exclusive=pm.TIER1_END
    )
    sealed_rows = _dated_rows(date(2025, 7, 1), 150, zone=15.0, confluence=2, r=-0.5)
    path = tmp_path / "entry_features_x.jsonl"
    _write_jsonl(path, build_rows + build_noise + tier_rows + sealed_rows)

    out = tmp_path / "report.json"
    payload = pm.run_mining(input_arg=str(tmp_path), unseal=False, output=out)
    assert payload["unseal"] is False
    assert isinstance(payload["expected_false_positives"], dict)
    assert "BUILD" in payload["expected_false_positives"]
    assert "TIER1" in payload["expected_false_positives"]
    assert "SEALED" in payload["expected_false_positives"]
    n = payload["n_candidates_tested"]
    assert payload["expected_false_positives"]["BUILD"] == round(n * 0.05, 4)
    assert payload["expected_false_positives"]["TIER1"] == round(n * 0.05 ** 2, 4)
    assert payload["expected_false_positives"]["SEALED"] == round(n * 0.05 ** 3, 4)
    assert payload["n_candidates_tested"] >= 1
    assert payload["n_sealed_survivors"] is None
    assert out.is_file()
    # sealed not in survivors without unseal
    for s in payload["survivors"]:
        assert "SEALED" not in s


def test_end_to_end_stage_lines_with_unseal(tmp_path: Path, capsys):
    build_rows = _dated_rows(
        date(2023, 1, 2), 150, zone=15.0, confluence=2, r=0.3, end_exclusive=pm.BUILD_END
    )
    tier_rows = _dated_rows(
        date(2024, 6, 3), 150, zone=15.0, confluence=2, r=0.28, end_exclusive=pm.TIER1_END
    )
    sealed_rows = _dated_rows(date(2025, 7, 1), 150, zone=15.0, confluence=2, r=0.25)
    path = tmp_path / "entry_features_y.jsonl"
    _write_jsonl(path, build_rows + tier_rows + sealed_rows)

    payload = pm.run_mining(input_arg=str(tmp_path), unseal=True, output=None)
    captured = capsys.readouterr()
    assert "BUILD" in captured.out and "expected by chance" in captured.out
    assert "TIER1" in captured.out
    assert "SEALED" in captured.out
    assert payload["n_sealed_survivors"] is not None
    assert payload["n_sealed_survivors"] >= 0