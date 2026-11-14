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
              "metrics": {"net_pips": net, "edge_pips": net + 1.0, "edge_t": 1.0}})


def _idea(L, section="B"):
    L.main(["idea", "--section", section, "--title", "t", "--mechanism", "m", "--hypothesis", "h", "--rules", "r"])


def _check(L, idea, sha="abc", verdict="pass"):
    L.append({"kind": "check", "idea": idea, "strategy": "s", "source_sha": sha, "verdict": verdict})


def _pm(L, idea, mode="edge_below_costs"):
    L.main(["postmortem", "--id", idea, "--strategy", "s", "--failure-mode", mode,
            "--diagnosis", "d", "--constraint", "c"])


def test_section_ids_and_status_flow(ledger):
    L = ledger
    _idea(L, "B")
    _idea(L, "C")
    assert set(L.ideas()) == {"B001", "C001"} and L.next_id("B") == "B002"
    _eval(L, "B001", "fail", -0.5)
    assert L.ideas()["B001"]["status"] == "build_fail"
    _eval(L, "B001", "pass", 0.4)
    assert L.ideas()["B001"]["status"] == "candidate"
    _eval(L, "B001", "pass", 0.4, period="robust")
    assert L.ideas()["B001"]["status"] == "robust"
    L.main(["validate", "--id", "B001", "--strategy", "s", "--verdict", "validated", "--reason", "ok"])
    assert L.ideas()["B001"]["status"] == "validated"
    assert "B001" in L.RENDERED.read_text()


def test_build_requires_check_and_postmortem_and_graveyards_after_three_fails(ledger):
    L = ledger
    _idea(L, "A")
    assert not L.can_build("A001", "abc")[0]               # no check yet
    _check(L, "A001")
    assert L.can_build("A001", "abc")[0]
    _eval(L, "A001", "fail", -1.0)
    ok, why = L.can_build("A001", "abc")
    assert not ok and "post-mortem" in why                  # revision needs a post-mortem
    _pm(L, "A001")
    assert L.can_build("A001", "abc")[0]
    _eval(L, "A001", "fail", -0.8)
    _pm(L, "A001")
    _eval(L, "A001", "fail", -0.6)
    assert L.ideas()["A001"]["status"] == "graveyard"
    assert not L.can_build("A001", "abc")[0]


def test_implementation_bug_refunds_one_strike(ledger):
    L = ledger
    _idea(L, "C")
    _check(L, "C001")
    for _ in range(3):
        _eval(L, "C001", "fail", -1.0)
        _pm(L, "C001", "implementation" if _ == 0 else "no_edge")
    assert L.ideas()["C001"]["build_fails"] == 2 and L.ideas()["C001"]["status"] == "build_fail"


def test_best_build_tracks_the_best_failing_variant(ledger):
    L = ledger
    _idea(L, "A")
    _eval(L, "A001", "fail", -1.6)
    _eval(L, "A001", "fail", -1.1)
    assert L.ideas()["A001"]["best"]["metrics"]["net_pips"] == -1.1


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


def _toy_frame(n=400):
    import numpy as np
    import pandas as pd
    t = pd.date_range("2019-06-03", periods=n, freq="5min", tz="UTC")
    close = 1.1 + np.cumsum(np.where(np.arange(n) % 2, 1e-4, -1e-4))
    df = pd.DataFrame({"time": t, "open": close, "high": close + 5e-5, "low": close - 5e-5, "close": close,
                       "london_minute": (t.hour * 60 + t.minute).astype(float)})
    df["year"] = df["time"].dt.year
    return df


def test_scanner_catches_lookahead_and_accepts_causal_signals():
    import numpy as np
    from research.scan import _lookahead_check
    df = _toy_frame()
    cheat = lambda d: np.sign(d["close"].shift(-1) - d["close"]).fillna(0)
    causal = lambda d: np.sign(d["close"] - d["close"].rolling(3).mean()).fillna(0)
    import pandas as pd
    assert _lookahead_check(cheat, df, pd.Series(cheat(df), index=df.index)) is not None
    assert _lookahead_check(causal, df, pd.Series(causal(df), index=df.index)) is None


def test_scanner_measures_signed_forward_moves_in_pips():
    import numpy as np
    import pandas as pd
    from research.scan import scan_signal
    df = _toy_frame()
    sig = pd.Series(0, index=df.index)
    sig.iloc[10] = 1          # expect up after bar 10; entry at open of bar 11
    res = scan_signal(df, sig, [2], cooldown=5, pip=1e-4)
    expected = (df["close"].iloc[12] - df["open"].iloc[11]) / 1e-4
    assert res["events"] == 1
    assert res["horizons"]["2"]["mean"] == pytest.approx(round(expected, 3))
