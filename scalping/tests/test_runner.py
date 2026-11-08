"""The runner scores every block with the same frozen parameters."""

from __future__ import annotations

import json

import pytest

from scalping.config import load_config
from scalping.costs import CostModel
from scalping.instruments import get_instrument
from scalping.run import Job, format_table, main, run_oos
from scalping.strategies import BollingerReversion
from scalping.synthetic import synthetic_eurusd


class Spy(BollingerReversion):
    constructed: list[dict] = []

    def __init__(self, period: int = 20, **kwargs):
        Spy.constructed.append({"period": period, **kwargs})
        super().__init__(period=period, **kwargs)


def test_oos_runner_does_not_retune_between_blocks():
    Spy.constructed = []
    market = synthetic_eurusd(n_bars=80, seed=1)
    instrument = get_instrument("EURUSD")
    jobs = [
        Job("2018-19", "EURUSD", market, instrument),
        Job("2019-20", "EURUSD", market, instrument),
        Job("2020-21", "EURUSD", market, instrument),
    ]
    report = run_oos(
        Spy,
        {"period": 12, "lots": 0.05},
        jobs,
        CostModel(commission_per_lot_side=3.5, slippage_points=1.0),
        initial_equity=50_000.0,
    )
    assert report["oos"] is True
    assert len(Spy.constructed) >= 3
    assert all(row["period"] == 12 and row["lots"] == pytest.approx(0.05) for row in Spy.constructed)
    assert [cell["block"] for cell in report["blocks"]] == ["2018-19", "2019-20", "2020-21"]
    assert "consistency_score" in report["aggregate"]
    assert 0.0 <= report["aggregate"]["consistency_score"] <= 1.0
    # The shared dict the runner closed over is not a place to stash results.
    assert "net_return" not in report["params"]
    text = format_table(report)
    assert "2018-19" in text and "consistency" in text


def test_config_blocks_and_pairs_match_the_brief():
    cfg = load_config()
    assert cfg.pairs == ("EURUSD", "GBPUSD", "USDJPY")
    assert [block.id for block in cfg.blocks] == [
        "2018-19",
        "2019-20",
        "2020-21",
        "2021-22",
        "2022-23",
        "2023-24",
    ]
    assert cfg.blocks[0].start.year == 2018
    assert cfg.blocks[0].end.year == 2019
    assert cfg.costs.account_currency == "EUR"
    assert cfg.costs.commission_per_lot_side == pytest.approx(3.5)


def test_demo_cli_writes_json(tmp_path):
    out = tmp_path / "demo.json"
    code = main(["--demo", "--strategy", "bollinger_reversion", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["oos"] is True
    assert payload["strategy"] == "bollinger_reversion"
    assert payload["account_currency"] == "EUR"
    assert payload["blocks"]
    cell = payload["blocks"][0]
    assert cell["trade_count"] > 0
    for key in ("net_return", "profit_factor", "win_rate", "expectancy", "max_drawdown", "sharpe"):
        assert key in cell
    assert "consistency_score" in payload["aggregate"]


def test_package_does_not_import_the_apex_engine():
    import sys

    import scalping
    import scalping.engine
    import scalping.run

    banned_roots = {
        "apex_trader",
        "apex_trader_v76",
        "continuous_backtester",
        "shadow_strategies",
        "intraday_backtester",
    }
    loaded = [name for name in sys.modules if name.split(".")[0] in banned_roots]
    assert loaded == []
    assert scalping.__version__
    assert scalping.engine.run_backtest
    assert scalping.run.main
