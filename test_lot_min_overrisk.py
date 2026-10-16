"""Broker volume_min floor must not silently over-risk tiny positions."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import apex_mt5_live as live


class _FakeMt5:
    """Minimal MT5 stand-in for volume / tick math."""

    def __init__(
        self,
        *,
        volume_min: float = 0.01,
        volume_step: float = 0.01,
        volume_max: float = 100.0,
        trade_tick_size: float = 0.00001,
        trade_tick_value: float = 1.0,
    ) -> None:
        self._info = SimpleNamespace(
            volume_min=volume_min,
            volume_step=volume_step,
            volume_max=volume_max,
            trade_tick_size=trade_tick_size,
            trade_tick_value=trade_tick_value,
            point=trade_tick_size,
            filling_mode=0,
        )

    def symbol_info(self, _sym: str) -> Any:
        return self._info


def test_tiny_risk_tight_stop_skips_lot_min_overrisk() -> None:
    """
    Risk-sized lots below 0.01 would floor UP; actual risk at vmin is 2× intended
    (>50% over) → skip with ``lot_min_overrisk``.
    """
    # 100 ticks × $1/tick = $100/lot to SL. risk=$5 → raw=0.05? Wait need raw < 0.01.
    # Use wider SL: 200 ticks → $200/lot. risk=$1 → raw=0.005 → floor to 0.01 → $200 actual.
    mt5 = _FakeMt5(trade_tick_size=0.00001, trade_tick_value=1.0)
    entry, sl = 1.10000, 1.09800  # 0.002 = 200 ticks
    mpl = live._money_per_lot_to_sl(mt5, "EURUSD", entry, sl)
    assert mpl is not None and abs(mpl - 200.0) < 1e-6
    intend = 1.0  # raw lots = 0.005
    vol, reason = live._volume_for_risk(mt5, "EURUSD", entry, sl, intend)
    assert vol is None
    assert reason == "lot_min_overrisk"
    # Actual at vmin: 0.01 * 200 = $2 > 1.5 * $1
    assert 0.01 * mpl > intend * live.LOT_MIN_OVERRISK_RATIO


def test_normal_risk_still_sizes() -> None:
    """Normal risk_usd produces a positive volume (no skip)."""
    mt5 = _FakeMt5(trade_tick_size=0.00001, trade_tick_value=1.0)
    entry, sl = 1.10000, 1.09900  # 100 ticks → $100/lot
    intend = 50.0  # raw = 0.5 lots
    vol, reason = live._volume_for_risk(mt5, "EURUSD", entry, sl, intend)
    assert reason is None
    assert vol == 0.5


def test_floor_up_within_50pct_still_places() -> None:
    """
    Flooring to vmin that oversizes by ≤50% is allowed (e.g. intend $7 → actual $10).
    """
    mt5 = _FakeMt5(trade_tick_size=0.00001, trade_tick_value=1.0)
    entry, sl = 1.10000, 1.09900  # $100/lot
    intend = 7.0  # raw=0.07 → wait that's above min. Need raw < 0.01.
    # $100/lot, intend=$7 → raw=0.07 >= 0.01. Use intend just under vmin risk.
    # vmin risk = $1.0 at $100/lot. intend=$0.70 → actual=$1.0 is ~43% over (<50%).
    intend = 0.70
    vol, reason = live._volume_for_risk(mt5, "EURUSD", entry, sl, intend)
    assert reason is None
    assert vol == 0.01


def test_execute_trade_propagates_lot_min_overrisk(monkeypatch) -> None:
    """Caller surfaces skip_reason ``lot_min_overrisk`` instead of placing."""
    mt5 = _FakeMt5(trade_tick_size=0.00001, trade_tick_value=1.0)

    class _Tick:
        ask = 1.10000
        bid = 1.09980

    mt5.symbol_info_tick = lambda _s: _Tick()  # type: ignore[method-assign]
    mt5.symbol_select = lambda _s, _e=True: True  # type: ignore[method-assign]
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    mt5.ORDER_FILLING_IOC = 1
    mt5.ORDER_FILLING_FOK = 2
    mt5.ORDER_FILLING_RETURN = 0
    mt5.ORDER_TIME_GTC = 0
    mt5.TRADE_ACTION_DEAL = 1
    mt5.TRADE_RETCODE_DONE = 10009

    sent: list[Any] = []

    def _order_send(req: dict) -> Any:
        sent.append(req)
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, comment="ok")

    mt5.order_send = _order_send  # type: ignore[method-assign]
    mt5.positions_get = lambda symbol=None: []  # type: ignore[method-assign]

    monkeypatch.setattr(live, "_lazy_mt5", lambda: mt5)
    monkeypatch.setattr(live, "trading_allowed", lambda: (True, ""))

    # Tiny risk + 200-tick stop → lot_min_overrisk
    out = live.execute_trade(
        "EURUSD",
        "LONG",
        stop_loss=1.09800,
        tp1=1.10200,
        risk_dollars=1.0,
    )
    assert out["ok"] is False
    assert out["error"] == "lot_min_overrisk"
    assert out.get("skip_reason") == "lot_min_overrisk"
    assert sent == []

    # Normal risk still places
    out_ok = live.execute_trade(
        "EURUSD",
        "LONG",
        stop_loss=1.09900,
        tp1=1.10200,
        risk_dollars=50.0,
    )
    assert out_ok["ok"] is True
    assert out_ok["volume"] == 0.5
    assert len(sent) == 1
