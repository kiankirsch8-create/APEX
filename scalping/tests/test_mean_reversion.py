"""Mean-reversion strategies emit fixed-parameter intents and do not look ahead."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from scalping.costs import OrderType, Side
from scalping.instruments import get_instrument
from scalping.strategies import all_strategies, get_strategy
from scalping.strategies.base import BarContext, BarWindow, OrderView, PositionView
from scalping.strategies.ema_zscore import EmaZscoreReversion
from scalping.strategies.grid_reversion import GridReversion
from scalping.strategies.mean_anchor import MeanAnchorReversion
from scalping.tests.support import market_from_minutes

POINT = get_instrument("EURUSD").point


def _minutes(mids: list[float], spread: float = 0.00008) -> list[list[tuple[float, float]]]:
    half = spread / 2.0
    return [[(mid - half, mid + half)] for mid in mids]


def _context(market, end: int | None = None, **overrides) -> BarContext:
    end = len(market) if end is None else end
    i = end - 1
    ctx = BarContext(
        symbol="EURUSD",
        point=POINT,
        bar_index=i,
        time="2020-01-06T10:00:00.000Z",
        open=float(market.open[i]),
        high=float(market.high[i]),
        low=float(market.low[i]),
        close=float(market.close[i]),
        spread_close=float(market.spread_close[i]),
        spread_mean=float(market.spread_mean[i]),
        window=BarWindow(market, end),
        positions=(),
        pending=(),
        equity=100_000.0,
    )
    if overrides:
        return replace(ctx, **overrides)
    return ctx


def _position(side: Side, entry: float) -> PositionView:
    return PositionView(
        id=1,
        side=side,
        lots=0.10,
        entry_price=entry,
        sl=None,
        tp=None,
        entry_time="2020-01-06T10:00:00.000Z",
        tag="open",
    )


def _pending(order_id: int = 7) -> OrderView:
    return OrderView(
        id=order_id,
        type=OrderType.LIMIT,
        side=Side.BUY,
        lots=0.10,
        price=1.09980,
        sl=None,
        tp=1.10000,
        tag="grid-buy-1",
    )


def test_registry_includes_mean_reversion_for_strategy_all():
    names = [cls.name for cls in all_strategies()]
    assert names == sorted(names)
    for name in ("grid_reversion", "mean_anchor_reversion", "ema_zscore_reversion"):
        assert name in names
        assert get_strategy(name).name == name


def test_defaults_are_fixed_and_json_safe():
    strategies = (GridReversion(), MeanAnchorReversion(), EmaZscoreReversion())
    dumped = [json.dumps(strategy.parameters()) for strategy in strategies]
    assert json.loads(dumped[0])["levels"] == 3
    assert json.loads(dumped[1])["anchor_period"] == 20
    assert json.loads(dumped[2])["z_enter"] == pytest.approx(2.0)
    market = market_from_minutes(_minutes([1.10] * 5))
    for strategy in strategies:
        before = dict(strategy.parameters())
        strategy.on_bar(_context(market))
        assert strategy.parameters() == before


def test_window_exposes_bar_volume():
    market = market_from_minutes(_minutes([1.10, 1.10010, 1.10020]))
    ctx = _context(market)
    assert len(ctx.window.volume) == 3
    assert float(ctx.window.volume[-1]) == pytest.approx(2.0)
    assert ctx.volume == pytest.approx(2.0)
    assert len(ctx.window.close) == 3


def test_grid_places_a_ladder_inside_a_quiet_band():
    market = market_from_minutes(_minutes([1.10] * 20))
    strategy = GridReversion(step_points=20, levels=3, tp_points=20, band_period=20)
    intents = strategy.on_bar(_context(market))
    assert len(intents) == 6
    close = 1.10
    buys = [item for item in intents if item.side == Side.BUY]
    sells = [item for item in intents if item.side == Side.SELL]
    assert [item.tag for item in buys] == ["grid-buy-1", "grid-buy-2", "grid-buy-3"]
    for level, item in enumerate(buys, start=1):
        price = close - level * 20 * POINT
        assert item.type == OrderType.LIMIT
        assert item.lots == pytest.approx(0.10)
        assert item.price == pytest.approx(price)
        assert item.tp == pytest.approx(price + 20 * POINT)
        assert item.sl is None
        assert item.price < close
    for level, item in enumerate(sells, start=1):
        price = close + level * 20 * POINT
        assert item.type == OrderType.LIMIT
        assert item.price == pytest.approx(price)
        assert item.tp == pytest.approx(price - 20 * POINT)
        assert item.price > close


def test_grid_skips_wide_spread_high_vol_and_an_existing_ladder():
    quiet = market_from_minutes(_minutes([1.10] * 20))
    strategy = GridReversion(step_points=20, levels=3, band_period=20, max_spread_points=30)
    wide = _context(quiet, spread_close=31 * POINT)
    assert strategy.on_bar(wide) == []

    mids = [1.10 if i % 2 == 0 else 1.101 for i in range(20)]
    wild = market_from_minutes(_minutes(mids))
    # Wide bands keep the close inside the envelope; the step is smaller than the chop.
    choppy = GridReversion(step_points=1, levels=2, band_period=20, band_std=10)
    assert choppy.on_bar(_context(wild)) == []

    assert strategy.on_bar(_context(quiet, pending=(_pending(),))) == []
    assert strategy.on_bar(_context(quiet, positions=(_position(Side.BUY, 1.09980),))) == []


def test_grid_cancels_when_price_leaves_the_band_and_flattens_a_losing_basket():
    mids = [1.10] * 19 + [1.105]
    market = market_from_minutes(_minutes(mids))
    strategy = GridReversion(band_period=20, basket_sl_points=50)
    pending = _pending()
    intents = strategy.on_bar(_context(market, pending=(pending,)))
    assert [type(item).__name__ for item in intents] == ["CancelIntent"]
    assert intents[0].order_id == pending.id

    # 80 points underwater on one long. The basket stop does not wait for the band.
    underwater = _context(market, positions=(_position(Side.BUY, 1.10580),), pending=(pending,))
    stopped = strategy.on_bar(underwater)
    assert any(type(item).__name__ == "CancelIntent" and item.order_id == pending.id for item in stopped)
    closes = [item for item in stopped if type(item).__name__ == "CloseIntent"]
    assert len(closes) == 1 and closes[0].position_id is None

    # A position in profit is not flattened, and it does not add another ladder.
    held = strategy.on_bar(_context(market_from_minutes(_minutes([1.10] * 20)), positions=(_position(Side.BUY, 1.09900),)))
    assert held == []


def test_grid_does_not_see_a_future_bar():
    mids = [1.10] * 20 + [1.105]
    market = market_from_minutes(_minutes(mids))
    strategy = GridReversion(step_points=20, levels=1, tp_points=20, band_period=20)
    early = strategy.on_bar(_context(market, end=20))
    assert len(early) == 2
    assert early[0].price == pytest.approx(1.10 - 20 * POINT)
    later = strategy.on_bar(_context(market, end=21))
    assert later == []
    assert len(strategy.parameters()) == 8


def test_mean_anchor_buys_a_stretch_and_sells_the_other_side():
    drop = 1.10 - 22 * POINT
    mids = [1.10] * 20 + [drop]
    minutes = _minutes(mids)
    minutes[-1] = [(1.09970 - 0.00004, 1.09970 + 0.00004), (drop - 0.00004, drop + 0.00004)]
    market = market_from_minutes(minutes)
    strategy = MeanAnchorReversion(anchor_period=20, entry_points=15, sl_points=20, trend_slope_max=2)
    intents = strategy.on_bar(_context(market))
    assert len(intents) == 1
    intent = intents[0]
    anchor = (19 * 1.10 + drop) / 20
    assert intent.type == OrderType.MARKET
    assert intent.side == Side.BUY
    assert intent.tag == "anchor-long"
    assert intent.lots == pytest.approx(0.10)
    assert intent.tp == pytest.approx(anchor)
    assert intent.sl == pytest.approx(1.09970 - 20 * POINT)
    assert intent.sl < float(market.low[-1])

    spike = 1.10 + 22 * POINT
    up = _minutes([1.10] * 20 + [spike])
    up[-1] = [(1.10030 - 0.00004, 1.10030 + 0.00004), (spike - 0.00004, spike + 0.00004)]
    short = MeanAnchorReversion().on_bar(_context(market_from_minutes(up)))
    assert len(short) == 1
    assert short[0].side == Side.SELL
    assert short[0].tag == "anchor-short"
    assert short[0].tp == pytest.approx((19 * 1.10 + spike) / 20)
    assert short[0].sl == pytest.approx(1.10030 + 20 * POINT)


def test_mean_anchor_skips_flat_trend_spread_and_an_open_position():
    strategy = MeanAnchorReversion()
    flat = market_from_minutes(_minutes([1.10] * 25))
    assert strategy.on_bar(_context(flat)) == []

    trend = [1.10 + i * 5 * POINT for i in range(30)]
    assert strategy.on_bar(_context(market_from_minutes(_minutes(trend)))) == []

    drop = 1.10 - 22 * POINT
    stretched = market_from_minutes(_minutes([1.10] * 20 + [drop]))
    assert strategy.on_bar(_context(stretched, spread_close=40 * POINT)) == []
    assert strategy.on_bar(_context(stretched, positions=(_position(Side.SELL, 1.10),))) == []
    assert strategy.on_bar(_context(stretched, pending=(_pending(),))) == []

    # The stretch is the last bar. The prior close is not a setup.
    assert strategy.on_bar(_context(stretched, end=len(stretched) - 1)) == []


def test_zscore_fades_a_stretch_and_ignores_a_quiet_or_wide_market():
    quiet = [1.10] * 40
    strategy = EmaZscoreReversion(ema_period=20, z_period=20, z_enter=2, sl_points=25)
    assert strategy.on_bar(_context(market_from_minutes(_minutes(quiet)))) == []

    spike = quiet + [1.102]
    market = market_from_minutes(_minutes(spike))
    intents = strategy.on_bar(_context(market))
    assert len(intents) == 1
    intent = intents[0]
    assert intent.type == OrderType.MARKET
    assert intent.side == Side.SELL
    assert intent.tag == "z-short"
    assert intent.lots == pytest.approx(0.10)
    assert intent.sl == pytest.approx(1.102 + 25 * POINT)
    assert 1.10 < intent.tp < 1.102

    dumped = quiet + [1.098]
    down = strategy.on_bar(_context(market_from_minutes(_minutes(dumped))))
    assert len(down) == 1
    assert down[0].side == Side.BUY
    assert down[0].tag == "z-long"
    assert down[0].sl == pytest.approx(1.098 - 25 * POINT)
    assert 1.098 < down[0].tp < 1.10

    assert strategy.on_bar(_context(market, spread_close=50 * POINT)) == []
    assert strategy.on_bar(_context(market, positions=(_position(Side.BUY, 1.10),))) == []
    # Without the spike, the same strategy has nothing to fade.
    assert strategy.on_bar(_context(market, end=len(market) - 1)) == []
