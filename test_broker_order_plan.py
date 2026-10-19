"""Tests for :func:`apex_trader.build_broker_order_plan` and the live-order path.

Live FTMO Demo orders were all failing with ``[TRADE] MT5 order result ...
ok=False error=Invalid stops`` because the engine was sending the decision
layer's STRATEGY-space (yfinance) absolute SL/TP prices straight to MT5. The
broker streams in a different price space (e.g. USDCHF ≈ 0.80 broker vs
strategy values around 24.33), so SL/TP were rejected and volume floored to 0.

These tests exercise the full broker-space translation with a mocked MT5
object that emulates FTMO Demo symbol attributes (bid/ask, point, digits,
trade_stops_level, volume_min/step/max, trade_tick_value, trade_contract_size).

The strategy's decision (direction / strategy_id / intended risk %) is
preserved — only the broker-space execution values change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest

import apex_trader as at
from apex_trader import BrokerOrderPlanError, build_broker_order_plan


# ---------------------------------------------------------------------------
# Mock MT5 scaffolding — mirrors MetaTrader5 Python API surface we touch
# ---------------------------------------------------------------------------

@dataclass
class _Tick:
    bid: float
    ask: float


@dataclass
class _SymInfo:
    point: float
    digits: int
    trade_stops_level: int
    trade_freeze_level: int
    volume_min: float
    volume_step: float
    volume_max: float
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float = 100_000.0
    filling_mode: int = 1


class _MockMT5:
    """Minimal MT5 stand-in. Attach ``info`` and ``tick`` per symbol."""

    # Constants used by apex_trader.order_send_live / fill_mode.
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self._symbols: dict[str, tuple[_SymInfo, _Tick]] = {}
        self.sent: list[dict[str, Any]] = []
        self.next_retcode: int = self.TRADE_RETCODE_DONE
        self.next_comment: str = "Done"
        self._last_error: tuple[int, str] = (0, "no error")

    def register(self, symbol: str, info: _SymInfo, tick: _Tick) -> None:
        self._symbols[symbol] = (info, tick)

    def symbol_info(self, s: str) -> _SymInfo | None:
        return self._symbols.get(s, (None, None))[0]

    def symbol_info_tick(self, s: str) -> _Tick | None:
        return self._symbols.get(s, (None, None))[1]

    def symbol_select(self, s: str, _enable: bool) -> bool:
        return s in self._symbols

    def positions_get(self, **_kw: Any) -> list[Any]:
        return []

    def order_send(self, req: dict[str, Any]) -> Any:
        self.sent.append(req)

        class _Res:
            retcode = self.next_retcode
            comment = self.next_comment
            order = 123456

        return _Res()

    def last_error(self) -> tuple[int, str]:
        return self._last_error


# FTMO Demo-like symbol table used across tests.
def _ftmo_mt5() -> _MockMT5:
    mt5 = _MockMT5()
    # USDCHF: 5-digit FX pair, broker price around 0.80, standard contract 100k.
    mt5.register(
        "USDCHF",
        _SymInfo(point=1e-5, digits=5, trade_stops_level=10, trade_freeze_level=0,
                 volume_min=0.01, volume_step=0.01, volume_max=100.0,
                 trade_tick_size=1e-5, trade_tick_value=1.25,
                 trade_contract_size=100_000.0),
        _Tick(bid=0.80000, ask=0.80020),
    )
    # EURCHF: broker price around 0.93.
    mt5.register(
        "EURCHF",
        _SymInfo(point=1e-5, digits=5, trade_stops_level=15, trade_freeze_level=0,
                 volume_min=0.01, volume_step=0.01, volume_max=100.0,
                 trade_tick_size=1e-5, trade_tick_value=1.17,
                 trade_contract_size=100_000.0),
        _Tick(bid=0.93000, ask=0.93025),
    )
    # GBPUSD: broker price around 1.27, tick value in USD is 10 per 1.00 lot.
    mt5.register(
        "GBPUSD",
        _SymInfo(point=1e-5, digits=5, trade_stops_level=5, trade_freeze_level=0,
                 volume_min=0.01, volume_step=0.01, volume_max=100.0,
                 trade_tick_size=1e-5, trade_tick_value=1.0,
                 trade_contract_size=100_000.0),
        _Tick(bid=1.27000, ask=1.27020),
    )
    # GBPJPY: 3-digit JPY pair, broker price around 199.
    mt5.register(
        "GBPJPY",
        _SymInfo(point=1e-3, digits=3, trade_stops_level=20, trade_freeze_level=0,
                 volume_min=0.01, volume_step=0.01, volume_max=100.0,
                 trade_tick_size=1e-3, trade_tick_value=0.73,
                 trade_contract_size=100_000.0),
        _Tick(bid=199.000, ask=199.020),
    )
    return mt5


# ---------------------------------------------------------------------------
# Bug-report reproductions: the exact symbols/numbers the user cited
# ---------------------------------------------------------------------------

class TestBugReportReproductions:
    """Each case feeds the user-reported strategy-space SL and checks that the
    broker-space SL now lands on the correct side of the market at a sane
    distance (and NOT the raw strategy value like 24.3295)."""

    def test_usdchf_long_strategy_sl_translates_to_valid_broker_sl(self):
        mt5 = _ftmo_mt5()
        # User-reported: USDCHF stop=24.3295 in strategy space. Suppose
        # strategy entry was 25.00 (approx), so sl_pct ≈ -2.68%.
        bp = build_broker_order_plan(
            mt5, "USDCHF", "LONG",
            strategy_entry=25.00,
            strategy_sl=24.3295,
            risk_usd=100.0,
        )
        assert bp["broker_entry"] == pytest.approx(0.80020, abs=1e-5)
        assert bp["broker_sl"] < bp["broker_entry"]
        # ~2.68% below 0.80020 ≈ 0.7788
        assert 0.775 < bp["broker_sl"] < 0.781
        assert bp["volume"] > 0

    def test_eurchf_long_strategy_sl_lands_on_broker_side(self):
        mt5 = _ftmo_mt5()
        # User-reported EURCHF stop=15.2675. Suppose entry ~15.70; sl_pct ≈ -2.76%.
        bp = build_broker_order_plan(
            mt5, "EURCHF", "LONG",
            strategy_entry=15.70,
            strategy_sl=15.2675,
            risk_usd=100.0,
        )
        assert bp["broker_entry"] == pytest.approx(0.93025, abs=1e-5)
        assert bp["broker_sl"] < bp["broker_entry"]
        assert 0.898 < bp["broker_sl"] < 0.910
        assert bp["volume"] > 0

    def test_gbpusd_long_strategy_sl_lands_on_broker_side(self):
        mt5 = _ftmo_mt5()
        # User-reported GBPUSD stop=83.922. Suppose entry ~86.00; sl_pct ≈ -2.42%.
        bp = build_broker_order_plan(
            mt5, "GBPUSD", "LONG",
            strategy_entry=86.00,
            strategy_sl=83.922,
            risk_usd=100.0,
        )
        assert bp["broker_entry"] == pytest.approx(1.27020, abs=1e-5)
        assert bp["broker_sl"] < bp["broker_entry"]
        assert 1.230 < bp["broker_sl"] < 1.245
        assert bp["volume"] > 0

    def test_gbpjpy_short_strategy_sl_above_broker_entry(self):
        mt5 = _ftmo_mt5()
        # SHORT: SL should be ABOVE entry. User-reported GBPJPY stop=89.117 with
        # entry hypothetically ~87.00 — short with sl_pct ≈ +2.43%.
        bp = build_broker_order_plan(
            mt5, "GBPJPY", "SHORT",
            strategy_entry=87.00,
            strategy_sl=89.117,
            risk_usd=100.0,
        )
        assert bp["broker_entry"] == pytest.approx(199.000, abs=1e-3)
        assert bp["broker_sl"] > bp["broker_entry"]
        assert 202.5 < bp["broker_sl"] < 205.5
        assert bp["volume"] > 0


# ---------------------------------------------------------------------------
# Core builder invariants
# ---------------------------------------------------------------------------

class TestBuilderInvariants:

    def test_long_sl_below_entry_tp_above_entry(self):
        mt5 = _ftmo_mt5()
        bp = build_broker_order_plan(
            mt5, "GBPUSD", "LONG",
            strategy_entry=100.0, strategy_sl=99.0,
            strategy_tp1=101.0, strategy_tp2=102.0, strategy_tp3=103.0,
            risk_usd=100.0,
        )
        assert bp["broker_sl"] < bp["broker_entry"]
        assert bp["broker_tp1"] > bp["broker_entry"]
        assert bp["broker_tp2"] > bp["broker_tp1"]
        assert bp["broker_tp3"] > bp["broker_tp2"]

    def test_short_sl_above_entry_tp_below_entry(self):
        mt5 = _ftmo_mt5()
        bp = build_broker_order_plan(
            mt5, "GBPUSD", "SHORT",
            strategy_entry=100.0, strategy_sl=101.0,
            strategy_tp1=99.0, strategy_tp2=98.0, strategy_tp3=97.0,
            risk_usd=100.0,
        )
        assert bp["broker_sl"] > bp["broker_entry"]
        assert bp["broker_tp1"] < bp["broker_entry"]
        assert bp["broker_tp2"] < bp["broker_tp1"]
        assert bp["broker_tp3"] < bp["broker_tp2"]

    def test_entry_matches_tick_side_for_direction(self):
        mt5 = _ftmo_mt5()
        tick = mt5.symbol_info_tick("GBPUSD")
        long_bp = build_broker_order_plan(mt5, "GBPUSD", "LONG",
            strategy_entry=100.0, strategy_sl=99.0, risk_usd=100.0)
        short_bp = build_broker_order_plan(mt5, "GBPUSD", "SHORT",
            strategy_entry=100.0, strategy_sl=101.0, risk_usd=100.0)
        assert long_bp["broker_entry"] == pytest.approx(tick.ask, abs=1e-6)
        assert short_bp["broker_entry"] == pytest.approx(tick.bid, abs=1e-6)

    def test_rounds_to_symbol_digits(self):
        mt5 = _ftmo_mt5()
        bp5 = build_broker_order_plan(mt5, "GBPUSD", "LONG",
            strategy_entry=100.0, strategy_sl=99.1234567, risk_usd=100.0)
        # GBPUSD has digits=5 → broker_sl must be a 5-decimal number.
        assert round(bp5["broker_sl"], 5) == bp5["broker_sl"]
        bp3 = build_broker_order_plan(mt5, "GBPJPY", "LONG",
            strategy_entry=100.0, strategy_sl=99.1234567, risk_usd=100.0)
        assert round(bp3["broker_sl"], 3) == bp3["broker_sl"]

    def test_stops_level_clamp_pulls_sl_in_when_too_close(self):
        mt5 = _ftmo_mt5()
        # Strategy wants SL only 0.001% below entry — broker's stops_level
        # (10 points on USDCHF at 1e-5 point) = 1e-4 price. The clamp must
        # push the SL at least that far from entry.
        bp = build_broker_order_plan(
            mt5, "USDCHF", "LONG",
            strategy_entry=1000.0,
            strategy_sl=999.99,   # 0.001% below
            risk_usd=100.0,
        )
        min_dist = max(mt5.symbol_info("USDCHF").trade_stops_level, 1) * mt5.symbol_info("USDCHF").point
        assert bp["broker_entry"] - bp["broker_sl"] >= min_dist - 1e-9
        assert bp["stops_level_applied"] is True

    def test_sizing_derives_from_tick_value(self):
        mt5 = _ftmo_mt5()
        bp = build_broker_order_plan(
            mt5, "GBPUSD", "LONG",
            strategy_entry=100.0, strategy_sl=98.0,  # 2% below
            risk_usd=100.0,
        )
        # vol * money_per_lot must be close to intended risk_usd.
        assert bp["money_per_lot"] > 0
        assert bp["actual_risk_usd"] <= 100.01
        # Volume must be a positive multiple of volume_step.
        step = mt5.symbol_info("GBPUSD").volume_step
        assert bp["volume"] > 0
        assert math.isclose(bp["volume"] / step, round(bp["volume"] / step), abs_tol=1e-6)

    def test_volume_floored_to_volume_step(self):
        mt5 = _ftmo_mt5()
        bp = build_broker_order_plan(
            mt5, "GBPUSD", "LONG",
            strategy_entry=100.0, strategy_sl=99.5,  # tight 0.5% stop
            risk_usd=50.0,
        )
        step = mt5.symbol_info("GBPUSD").volume_step
        # Must be a clean multiple of step (no fractional residue).
        ratio = bp["volume"] / step
        assert abs(ratio - round(ratio)) < 1e-6

    def test_lot_min_overrisk_raises_specific_error(self):
        mt5 = _ftmo_mt5()
        # Tiny intended risk ($1) + wide stop → vol_min would risk way more
        # than 1.5× intended → should raise lot_min_overrisk, NOT silently
        # size to 0 or send an oversized order.
        with pytest.raises(BrokerOrderPlanError) as exc:
            build_broker_order_plan(
                mt5, "USDCHF", "LONG",
                strategy_entry=100.0, strategy_sl=98.0,  # ~2% stop
                risk_usd=0.50,
            )
        assert exc.value.reason == "lot_min_overrisk"
        assert "would risk" in exc.value.detail

    def test_sl_wrong_side_raises(self):
        mt5 = _ftmo_mt5()
        # LONG with SL ABOVE entry — must reject, not silently flip.
        with pytest.raises(BrokerOrderPlanError) as exc:
            build_broker_order_plan(
                mt5, "GBPUSD", "LONG",
                strategy_entry=100.0, strategy_sl=101.0,
                risk_usd=100.0,
            )
        assert exc.value.reason == "sl_wrong_side"

    def test_no_tick_raises(self):
        mt5 = _MockMT5()  # empty, no symbols registered
        with pytest.raises(BrokerOrderPlanError) as exc:
            build_broker_order_plan(
                mt5, "ZZZZZZ", "LONG",
                strategy_entry=100.0, strategy_sl=99.0,
                risk_usd=100.0,
            )
        assert exc.value.reason == "no_symbol_info"


# ---------------------------------------------------------------------------
# End-to-end: order_send_live receives the strategy-space SL and must send a
# broker-space request that the mock broker accepts.
# ---------------------------------------------------------------------------

class TestOrderSendLiveEndToEnd:

    def test_end_to_end_live_order_uses_broker_space_request(self, monkeypatch):
        """The exact failure mode from the bug report: 17 live attempts, every
        one ``ok=False error=Invalid stops`` with ``lot_size=0.0``. With the
        fix the mock broker ACCEPTS the request, the SL/TP are in broker space,
        and the volume is >0.
        """
        mt5 = _ftmo_mt5()

        # Short-circuit the currency/account conflict gates to focus on the
        # order-construction fix.
        monkeypatch.setattr(at, "position_forex_base6", lambda s: "")
        monkeypatch.setattr(at, "get_currencies", lambda s: [])
        monkeypatch.setattr(at, "account_symbol_direction_conflict",
                            lambda m, s, d: (False, ""))
        # Avoid touching ticket-meta disk in tests.
        monkeypatch.setattr(at, "ticket_meta_load", lambda: {})
        monkeypatch.setattr(at, "ticket_meta_save", lambda tm: None)
        # positions_get after send → simulate the broker returning our ticket.
        class _Pos:
            ticket = 42
            magic = at.APEX_MAGIC
            comment = at.ORDER_COMMENT
            time = 1

        monkeypatch.setattr(mt5, "positions_get", lambda **kw: [_Pos()])

        meta = {
            "ticker": "USDCHF",
            "entry": 25.00,         # strategy-space entry
            "sl": 24.3295,          # strategy-space SL (the bug-report value)
            "tp1": 25.50,
            "tp2": 26.00,
            "tp3": 26.50,
        }
        res = at.order_send_live(
            mt5, "USDCHF", "LONG",
            sl=24.3295, risk_usd=100.0, meta=meta,
        )

        assert res["ok"] is True, f"order was rejected: {res}"
        assert res["ticket"] == 42
        assert res["volume"] > 0
        # One request was sent — inspect it.
        assert len(mt5.sent) == 1
        req = mt5.sent[0]
        # Entry must match broker ask (LONG), not the strategy-space 25.00.
        assert req["price"] == pytest.approx(0.80020, abs=1e-5)
        # SL must be in broker space, below entry, 5-digit rounded.
        assert req["sl"] < req["price"]
        assert 0.775 < req["sl"] < 0.781
        assert round(req["sl"], 5) == req["sl"]
        # TP must be in broker space, above entry.
        assert req["tp"] > req["price"]
        # Volume must be a positive multiple of volume_step (0.01) — NOT 0.0.
        assert req["volume"] >= 0.01
        step_ratio = req["volume"] / 0.01
        assert abs(step_ratio - round(step_ratio)) < 1e-6

    def test_lot_min_overrisk_short_circuits_before_order_send(self, monkeypatch):
        """When vol_min would over-risk by >50%, we must NOT call mt5.order_send."""
        mt5 = _ftmo_mt5()
        monkeypatch.setattr(at, "position_forex_base6", lambda s: "")
        monkeypatch.setattr(at, "get_currencies", lambda s: [])
        monkeypatch.setattr(at, "account_symbol_direction_conflict",
                            lambda m, s, d: (False, ""))

        meta = {"entry": 100.0, "sl": 98.0}
        res = at.order_send_live(
            mt5, "USDCHF", "LONG",
            sl=98.0, risk_usd=0.50, meta=meta,
        )
        assert res["ok"] is False
        assert res["error"] == "lot_min_overrisk"
        assert mt5.sent == []   # no order placed

    def test_broker_rejects_still_returns_full_diagnostics(self, monkeypatch):
        """If the broker still rejects for any reason, the result must carry
        retcode, last_error, and the full request we sent — the user's
        diagnostic logging requirement."""
        mt5 = _ftmo_mt5()
        mt5.next_retcode = 10016  # TRADE_RETCODE_INVALID_STOPS
        mt5.next_comment = "Invalid stops"
        mt5._last_error = (-2, "Invalid stops level")

        monkeypatch.setattr(at, "position_forex_base6", lambda s: "")
        monkeypatch.setattr(at, "get_currencies", lambda s: [])
        monkeypatch.setattr(at, "account_symbol_direction_conflict",
                            lambda m, s, d: (False, ""))

        meta = {"entry": 100.0, "sl": 98.0, "tp1": 102.0}
        res = at.order_send_live(
            mt5, "GBPUSD", "LONG",
            sl=98.0, risk_usd=100.0, meta=meta,
        )
        assert res["ok"] is False
        assert res["retcode"] == 10016
        assert res["last_error"] == (-2, "Invalid stops level")
        assert "request" in res and res["request"]["symbol"] == "GBPUSD"
        assert "broker_plan" in res


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
