"""Research harness: plug-in loading, ledger bookkeeping, gate logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PLUGIN = '''
from scalping.strategies.base import Strategy
from scalping.strategies.timeframe import make_timeframe

class Probe(Strategy):
    name = "probe_strategy"
    def __init__(self, band_points: float = 10.0):
        self.band_points = band_points
    def parameters(self):
        return {"band_points": self.band_points}
    def on_bar(self, ctx):
        return []

ProbeM15 = make_timeframe(Probe, 15)
'''


def test_plugins_register_plain_and_timeframe_variants(tmp_path):
    (tmp_path / "probe.py").write_text(PLUGIN)
    code = ("from scalping.strategies import get_strategy as g;"
            "print(g('probe_strategy').name, g('probe_strategy@M15')().parameters()['band_points'])")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env={**os.environ, "SCALPING_PLUGIN_DIRS": str(tmp_path)}, check=True).stdout.split()
    assert out == ["probe_strategy", "38.73"]  # 10 * sqrt(15), scaled by the wrapper


def test_plugin_cannot_shadow_a_harness_strategy(tmp_path):
    (tmp_path / "bad.py").write_text(PLUGIN.replace("probe_strategy", "bollinger_reversion"))
    proc = subprocess.run([sys.executable, "-c", "import scalping.strategies"], cwd=REPO, capture_output=True,
                          text=True, env={**os.environ, "SCALPING_PLUGIN_DIRS": str(tmp_path)})
    assert proc.returncode != 0 and "is taken" in proc.stderr


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from research import ledger as L
    monkeypatch.setattr(L, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(L, "RENDERED", tmp_path / "LEDGER.md")
    return L


def _eval(L, idea, verdict, net=0.1, period="build"):
    L.append({"kind": "eval", "idea": idea, "strategy": "s", "period": period, "verdict": verdict,
              "metrics": {"net_pips": net, "edge_pips": net + 1.3, "edge_t": 1.0}})


def test_ledger_status_flow_and_eval_counting(ledger):
    L = ledger
    L.main(["idea", "--title", "t", "--mechanism", "m", "--hypothesis", "h", "--rules", "r"])
    assert L.next_id() == "R002"
    _eval(L, "R001", "fail", -0.5)
    assert L.ideas()["R001"]["status"] == "build_fail"
    _eval(L, "R001", "pass", 0.4)
    assert L.ideas()["R001"]["status"] == "candidate"
    assert L.build_evals("R001") == 2
    L.main(["validate", "--id", "R001", "--strategy", "s", "--verdict", "validated", "--reason", "ok"])
    assert L.ideas()["R001"]["status"] == "validated"
    assert "R001" in (L.RENDERED).read_text()


def test_build_gate_tightens_with_each_prior_evaluation():
    from research.evaluate import gates
    m = {"trades": 500, "edge_t": 3.5, "net_pips": 0.5, "pos_quarters": 0.7, "worst_dd": 0.03,
         "by_year": {str(y): {"edge_pips": 1.0} for y in (2018, 2019, 2020, 2021)}}
    assert all(gates("build", m, 0).values())
    assert not all(gates("build", m, 2).values())   # needs t >= 3.6 on the third try


def test_confirm_and_stress_gates():
    from research.evaluate import gates
    m = {"trades": 100, "edge_t": 2.2, "net_pips": 0.2, "pos_quarters": 0.58, "worst_dd": 0.02,
         "by_year": {"2022": {"edge_pips": 1}, "2023": {"edge_pips": -1}, "2024": {"edge_pips": 1}}}
    assert all(gates("confirm", m, 0).values())
    assert gates("stress", {**m, "net_pips": -0.01}, 0) == {"net>0 under stress": False}


def test_best_build_tracks_the_best_failing_variant(ledger):
    L = ledger
    L.main(["idea", "--title", "t", "--mechanism", "m", "--hypothesis", "h", "--rules", "r"])
    _eval(L, "R001", "fail", -1.6)
    _eval(L, "R001", "fail", -1.1)
    assert L.ideas()["R001"]["best"]["metrics"]["net_pips"] == -1.1
