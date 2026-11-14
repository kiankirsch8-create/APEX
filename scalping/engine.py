"""Tick-accurate execution.

Strategy code runs on closed M1 bars. Orders emitted on bar ``i`` become
active on bar ``i + 1`` (no lookahead). While a bar is open, ticks decide
fills: a stop or target can execute before the bar closes.

Fills are pessimistic. A market buy pays the ask plus slippage. A long
stop-loss sells at ``min(stop, bid) - slippage``. Limit orders and
take-profits fill at their price, without improvement. Commission is charged
on the entry and on the exit and converted into the account currency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from scalping.costs import (
    CostModel,
    FillKind,
    OrderType,
    Side,
    convert_to_eur,
    execution_price,
    quote_pnl,
)
from scalping.fills import EVENT_WIDTH, get_simulate
from scalping.instruments import Instrument
from scalping.market import MarketData, with_min_spread
from scalping.strategies.base import (
    BarContext,
    BarWindow,
    CancelIntent,
    CloseIntent,
    ModifyIntent,
    OrderIntent,
    OrderView,
    PositionView,
)

MAX_POS = 64
MAX_ORD = 128

_ORDER_CODE = {OrderType.MARKET: 0, OrderType.LIMIT: 1, OrderType.STOP: 2}


@dataclass
class Trade:
    symbol: str
    side: str
    lots: float
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    exit_reason: str
    spread_entry: float
    spread_exit: float
    slippage_entry: float
    slippage_exit: float
    gross_pnl: float
    commission: float
    net_pnl: float
    tag: str
    financing: float = 0.0  # overnight funding paid (account currency, positive = cost)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "lots": self.lots,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "spread_entry": self.spread_entry,
            "spread_exit": self.spread_exit,
            "slippage_entry": self.slippage_entry,
            "slippage_exit": self.slippage_exit,
            "gross_pnl": self.gross_pnl,
            "commission": self.commission,
            "net_pnl": self.net_pnl,
            "tag": self.tag,
            "financing": self.financing,
        }


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_bar: np.ndarray
    equity_trade: np.ndarray
    bar_time: np.ndarray
    initial_equity: float
    final_equity: float
    warnings: list[str] = field(default_factory=list)
    ticks_processed: int = 0
    bars_processed: int = 0

    @property
    def cash_pnl(self) -> float:
        return self.final_equity - self.initial_equity


@dataclass
class _Pos:
    id: int
    side: int
    lots: float
    entry_price: float
    entry_time_ns: int
    entry_commission: float
    spread_entry: float
    slippage_entry: float
    tag: str


@dataclass
class _Ord:
    id: int
    type_name: str
    tag: str


class FxBook:
    """EUR conversion using the traded quote and, when needed, EURUSD ticks."""

    def __init__(self, instrument: Instrument, eurusd: MarketData | None = None):
        self.instrument = instrument
        needs_cross = instrument.symbol != "EURUSD" and instrument.quote != "EUR"
        if needs_cross and (eurusd is None or len(eurusd.tick_time) == 0):
            raise ValueError(
                f"{instrument.symbol} settles in {instrument.quote}. "
                "Load EURUSD ticks to convert P&L into EUR."
            )
        if eurusd is None:
            self._ns = None
            self._bid = None
            self._ask = None
        else:
            self._ns = np.ascontiguousarray(eurusd.tick_time.view(np.int64))
            self._bid = eurusd.bid
            self._ask = eurusd.ask

    def to_eur(
        self,
        amount: float,
        currency: str,
        when_ns: int,
        symbol_bid: float,
        symbol_ask: float,
    ) -> float:
        if self.instrument.symbol == "EURUSD":
            eu_bid, eu_ask = float(symbol_bid), float(symbol_ask)
        else:
            eu_bid, eu_ask = self._eurusd_at(when_ns)
        jpy_bid = jpy_ask = None
        if currency.upper() == "JPY":
            jpy_bid, jpy_ask = float(symbol_bid), float(symbol_ask)
        return convert_to_eur(
            amount,
            currency,
            eurusd_bid=eu_bid,
            eurusd_ask=eu_ask,
            usdjpy_bid=jpy_bid,
            usdjpy_ask=jpy_ask,
        )

    def _eurusd_at(self, when_ns: int) -> tuple[float, float]:
        if self._ns is None:
            raise ValueError("EURUSD ticks are not loaded")
        idx = int(np.searchsorted(self._ns, int(when_ns), side="right") - 1)
        if idx < 0:
            idx = 0
        if idx >= len(self._bid):
            idx = len(self._bid) - 1
        return float(self._bid[idx]), float(self._ask[idx])


def run_backtest(
    market: MarketData,
    strategy,
    instrument: Instrument,
    costs: CostModel,
    *,
    fx: FxBook | None = None,
    initial_equity: float = 100_000.0,
    flatten_end: bool = True,
) -> BacktestResult:
    """Run ``strategy`` once over ``market``. The strategy object is not reused."""
    if len(market) == 0:
        raise ValueError("market has no bars")
    book = fx if fx is not None else FxBook(instrument)
    if costs.min_spread_points > 0:
        market = with_min_spread(market, costs.min_spread_points * float(instrument.point))
    engine = _Engine(market, strategy, instrument, costs, book, float(initial_equity), flatten_end)
    return engine.run()


class _Engine:
    def __init__(self, market, strategy, instrument, costs, fx, initial_equity, flatten_end):
        self.market = market
        self.strategy = strategy
        self.instrument = instrument
        self.costs = costs
        self.fx = fx
        self.initial_equity = float(initial_equity)
        self.flatten_end = flatten_end
        self.cash = float(initial_equity)
        self.slip = costs.slippage_price(instrument)
        self.trades: list[Trade] = []
        self.warnings: list[str] = []
        self.next_id = 1
        self.pending: list = []
        self.equity_bar = np.empty(len(market), dtype=np.float64)
        self.equity_trade: list[float] = []
        self.ticks_processed = 0
        self.rejected_invalid_tp = 0

        self.pos_side = np.zeros(MAX_POS, dtype=np.int64)
        self.pos_lots = np.zeros(MAX_POS, dtype=np.float64)
        self.pos_entry = np.zeros(MAX_POS, dtype=np.float64)
        self.pos_sl = np.full(MAX_POS, -1.0, dtype=np.float64)
        self.pos_tp = np.full(MAX_POS, -1.0, dtype=np.float64)
        self.pos_close = np.zeros(MAX_POS, dtype=np.int64)
        self.pos_born = np.full(MAX_POS, -1, dtype=np.int64)
        self.pos_lock = np.zeros(MAX_POS, dtype=np.int64)
        self.pos_hi = np.array([-1], dtype=np.int64)
        self.pos_meta: list[_Pos | None] = [None] * MAX_POS

        self.ord_type = np.zeros(MAX_ORD, dtype=np.int64)
        self.ord_side = np.zeros(MAX_ORD, dtype=np.int64)
        self.ord_px = np.zeros(MAX_ORD, dtype=np.float64)
        self.ord_sl = np.full(MAX_ORD, -1.0, dtype=np.float64)
        self.ord_tp = np.full(MAX_ORD, -1.0, dtype=np.float64)
        self.ord_lots = np.zeros(MAX_ORD, dtype=np.float64)
        self.ord_alive = np.zeros(MAX_ORD, dtype=np.int64)
        self.ord_hi = np.array([-1], dtype=np.int64)
        self.ord_meta: list[_Ord | None] = [None] * MAX_ORD

        cap = MAX_POS + 2 * MAX_ORD + 8
        self.events = np.zeros((cap, EVENT_WIDTH), dtype=np.float64)
        self.simulate = get_simulate()
        self.tick_ns = np.ascontiguousarray(market.tick_time.view(np.int64))

    def run(self) -> BacktestResult:
        n_bars = len(self.market)
        for bar_i in range(n_bars):
            self._arm_bar()
            self._apply_intents(self.pending)
            self.pending = []
            self._replay_bar(bar_i)
            self.equity_bar[bar_i] = self._mark(bar_i)
            ctx = self._context(bar_i)
            emitted = self.strategy.on_bar(ctx)
            self.pending = _as_list(emitted)
        if self.flatten_end:
            self._flatten()
        if self.rejected_invalid_tp:
            self.warnings.append(
                f"rejected {self.rejected_invalid_tp} order(s): take-profit already through the fill price"
            )
        final = float(self.equity_bar[-1]) if not self._open_positions() else self._mark(n_bars - 1)
        if self.flatten_end and not self._open_positions():
            final = self.cash
            self.equity_bar[-1] = final
        return BacktestResult(
            trades=self.trades,
            equity_bar=self.equity_bar,
            equity_trade=np.asarray(self.equity_trade, dtype=np.float64),
            bar_time=self.market.bar_time,
            initial_equity=self.initial_equity,
            final_equity=final,
            warnings=self.warnings,
            ticks_processed=self.ticks_processed,
            bars_processed=n_bars,
        )

    def _arm_bar(self) -> None:
        self.pos_lock[:] = 0
        self.pos_born[:] = -1
        live = np.flatnonzero(self.pos_side != 0)
        self.pos_hi[0] = int(live[-1]) if len(live) else -1
        alive = np.flatnonzero(self.ord_alive != 0)
        self.ord_hi[0] = int(alive[-1]) if len(alive) else -1

    def _replay_bar(self, bar_i: int) -> None:
        start = int(self.market.tick_start[bar_i])
        end = int(self.market.tick_end[bar_i])
        if end <= start:
            return
        bid = np.ascontiguousarray(self.market.bid[start:end])
        ask = np.ascontiguousarray(self.market.ask[start:end])
        n_ev = int(
            self.simulate(
                bid,
                ask,
                self.pos_side,
                self.pos_lots,
                self.pos_entry,
                self.pos_sl,
                self.pos_tp,
                self.pos_close,
                self.pos_born,
                self.pos_lock,
                self.pos_hi,
                self.ord_type,
                self.ord_side,
                self.ord_px,
                self.ord_sl,
                self.ord_tp,
                self.ord_lots,
                self.ord_alive,
                self.ord_hi,
                float(self.slip),
                self.events,
            )
        )
        if n_ev >= self.events.shape[0]:
            raise RuntimeError("fill event buffer exhausted")
        self.ticks_processed += end - start
        for k in range(n_ev):
            self._apply_event(self.events[k], start)

    def _apply_event(self, event: np.ndarray, tick_start: int) -> None:
        tick_i = tick_start + int(event[0])
        kind = float(event[1])
        slot = int(event[2])
        ord_slot = int(event[3])
        price = float(event[4])
        lots = float(event[5])
        side = int(event[6])
        bid = float(event[7])
        ask = float(event[8])
        when = int(self.tick_ns[tick_i])
        if kind == 1.0:
            self._open_position(slot, ord_slot, side, lots, price, bid, ask, when)
        elif kind == 5.0:
            self.ord_meta[ord_slot] = None
            self.rejected_invalid_tp += 1
        else:
            reason = {2.0: "sl", 3.0: "tp", 4.0: "market"}.get(kind, "exit")
            slip = 0.0 if reason == "tp" else self.slip
            self._close_position(slot, price, bid, ask, when, reason, slip)

    def _open_position(self, slot, ord_slot, side, lots, price, bid, ask, when) -> None:
        meta_ord = self.ord_meta[ord_slot] if ord_slot >= 0 else None
        type_name = meta_ord.type_name if meta_ord is not None else "market"
        tag = meta_ord.tag if meta_ord is not None else ""
        if meta_ord is not None:
            self.ord_meta[ord_slot] = None
        slip = self.slip if type_name in ("market", "stop") else 0.0
        commission = self._cash_cost(lots, when, bid, ask)
        self.cash -= commission
        self.pos_meta[slot] = _Pos(
            id=self._take_id(),
            side=side,
            lots=lots,
            entry_price=price,
            entry_time_ns=when,
            entry_commission=commission,
            spread_entry=ask - bid,
            slippage_entry=slip,
            tag=tag,
        )

    def _financing(self, meta, when, bid, ask) -> float:
        """Overnight funding for every 22:00 UTC rollover between entry and exit."""
        pct = self.costs.financing_long_pct if meta.side == 1 else self.costs.financing_short_pct
        if pct == 0.0:
            return 0.0
        day, roll = 86_400_000_000_000, 22 * 3_600_000_000_000
        nights = (int(when) - roll) // day - (int(meta.entry_time_ns) - roll) // day
        if nights <= 0:
            return 0.0
        notional = abs(meta.lots) * float(self.instrument.contract_size)
        return self.fx.to_eur(notional * pct / 100.0 * nights, self.instrument.base, when, bid, ask)

    def _close_position(self, slot, price, bid, ask, when, reason, slip) -> None:
        meta = self.pos_meta[slot]
        if meta is None:
            self.warnings.append(f"exit on empty slot {slot}")
            return
        exit_comm = self._cash_cost(meta.lots, when, bid, ask)
        gross = self.fx.to_eur(
            quote_pnl(Side(meta.side), meta.entry_price, price, meta.lots, self.instrument.contract_size),
            self.instrument.quote,
            when,
            bid,
            ask,
        )
        financing = self._financing(meta, when, bid, ask)
        self.cash += gross - exit_comm - financing
        net = gross - meta.entry_commission - exit_comm - financing
        self.trades.append(
            Trade(
                symbol=self.instrument.symbol,
                side="buy" if meta.side == 1 else "sell",
                lots=meta.lots,
                entry_time=_iso(meta.entry_time_ns),
                exit_time=_iso(when),
                entry_price=meta.entry_price,
                exit_price=price,
                exit_reason=reason,
                spread_entry=meta.spread_entry,
                spread_exit=ask - bid,
                slippage_entry=meta.slippage_entry,
                slippage_exit=slip,
                gross_pnl=gross,
                commission=meta.entry_commission + exit_comm,
                net_pnl=net,
                tag=meta.tag,
                financing=financing,
            )
        )
        self.pos_meta[slot] = None
        self.pos_side[slot] = 0
        self.equity_trade.append(self._mark_quote(bid, ask, when))

    def _apply_intents(self, intents: list) -> None:
        for intent in intents:
            if isinstance(intent, CancelIntent):
                self._cancel(intent.order_id)
            elif isinstance(intent, ModifyIntent):
                self._modify(intent)
            elif isinstance(intent, CloseIntent):
                self._request_close(intent.position_id)
            elif isinstance(intent, OrderIntent):
                self._place(intent)
            else:
                self.warnings.append(f"ignored intent {type(intent).__name__}")

    def _place(self, intent: OrderIntent) -> None:
        if intent.lots <= 0:
            self.warnings.append("rejected order with non-positive lots")
            return
        if intent.type in (OrderType.LIMIT, OrderType.STOP) and (intent.price is None or intent.price <= 0):
            self.warnings.append("rejected limit/stop without a price")
            return
        slot = self._alloc_order()
        if slot < 0:
            self.warnings.append("rejected order: pending-order capacity is full")
            return
        self.ord_type[slot] = _ORDER_CODE[intent.type]
        self.ord_side[slot] = int(intent.side)
        self.ord_px[slot] = float(intent.price or 0.0)
        self.ord_sl[slot] = float(intent.sl) if intent.sl else -1.0
        self.ord_tp[slot] = float(intent.tp) if intent.tp else -1.0
        self.ord_lots[slot] = float(intent.lots)
        self.ord_alive[slot] = 1
        self.ord_meta[slot] = _Ord(id=self._take_id(), type_name=intent.type.value, tag=intent.tag)
        if slot > int(self.ord_hi[0]):
            self.ord_hi[0] = slot

    def _cancel(self, order_id: int) -> None:
        slot = self._find_order(order_id)
        if slot < 0:
            self.warnings.append(f"cancel missed order {order_id}")
            return
        self.ord_alive[slot] = 0
        self.ord_meta[slot] = None

    def _modify(self, intent: ModifyIntent) -> None:
        slot = self._find_position(intent.position_id)
        if slot < 0:
            self.warnings.append(f"modify missed position {intent.position_id}")
            return
        if intent.sl is not None:
            self.pos_sl[slot] = float(intent.sl)
        if intent.tp is not None:
            self.pos_tp[slot] = float(intent.tp)

    def _request_close(self, position_id: int | None) -> None:
        if position_id is None:
            for slot, meta in enumerate(self.pos_meta):
                if meta is not None and self.pos_side[slot] != 0:
                    self.pos_close[slot] = 1
            return
        slot = self._find_position(position_id)
        if slot < 0:
            self.warnings.append(f"close missed position {position_id}")
            return
        self.pos_close[slot] = 1

    def _flatten(self) -> None:
        if len(self.market.bid) == 0 or not self._open_positions():
            return
        last = len(self.market.bid) - 1
        bid = float(self.market.bid[last])
        ask = float(self.market.ask[last])
        when = int(self.tick_ns[last])
        for slot, meta in enumerate(list(self.pos_meta)):
            if meta is None or self.pos_side[slot] == 0:
                continue
            price = execution_price(
                side=_closing_side(meta.side),
                kind=FillKind.MARKET,
                bid=bid,
                ask=ask,
                level=None,
                slippage_price=self.slip,
            )
            if price is None:
                self.warnings.append(f"could not flatten position {meta.id}")
                continue
            self.pos_side[slot] = 0
            self._close_position(slot, float(price), bid, ask, when, "eod", self.slip)

    def _context(self, bar_i: int) -> BarContext:
        end = bar_i + 1
        return BarContext(
            symbol=self.instrument.symbol,
            point=self.instrument.point,
            bar_index=bar_i,
            time=_iso(int(self.market.bar_time.view(np.int64)[bar_i])),
            open=float(self.market.open[bar_i]),
            high=float(self.market.high[bar_i]),
            low=float(self.market.low[bar_i]),
            close=float(self.market.close[bar_i]),
            spread_close=float(self.market.spread_close[bar_i]),
            spread_mean=float(self.market.spread_mean[bar_i]),
            window=BarWindow(self.market, end),
            positions=tuple(self._position_views()),
            pending=tuple(self._order_views()),
            equity=float(self.equity_bar[bar_i]),
        )

    def _position_views(self) -> list[PositionView]:
        views = []
        for slot, meta in enumerate(self.pos_meta):
            if meta is None or self.pos_side[slot] == 0:
                continue
            sl = float(self.pos_sl[slot])
            tp = float(self.pos_tp[slot])
            views.append(
                PositionView(
                    id=meta.id,
                    side=Side(meta.side),
                    lots=meta.lots,
                    entry_price=meta.entry_price,
                    sl=None if sl <= 0 else sl,
                    tp=None if tp <= 0 else tp,
                    entry_time=_iso(meta.entry_time_ns),
                    tag=meta.tag,
                )
            )
        return views

    def _order_views(self) -> list[OrderView]:
        views = []
        names = {0: OrderType.MARKET, 1: OrderType.LIMIT, 2: OrderType.STOP}
        for slot, meta in enumerate(self.ord_meta):
            if meta is None or self.ord_alive[slot] == 0:
                continue
            px = float(self.ord_px[slot])
            sl = float(self.ord_sl[slot])
            tp = float(self.ord_tp[slot])
            views.append(
                OrderView(
                    id=meta.id,
                    type=names[int(self.ord_type[slot])],
                    side=Side(int(self.ord_side[slot])),
                    lots=float(self.ord_lots[slot]),
                    price=None if px <= 0 else px,
                    sl=None if sl <= 0 else sl,
                    tp=None if tp <= 0 else tp,
                    tag=meta.tag,
                )
            )
        return views

    def _mark(self, bar_i: int) -> float:
        end = int(self.market.tick_end[bar_i])
        if end <= 0:
            return self.cash
        last = end - 1
        return self._mark_quote(
            float(self.market.bid[last]),
            float(self.market.ask[last]),
            int(self.tick_ns[last]),
        )

    def _mark_quote(self, bid: float, ask: float, when_ns: int) -> float:
        unreal = 0.0
        for slot, meta in enumerate(self.pos_meta):
            if meta is None or self.pos_side[slot] == 0:
                continue
            position_side = Side(meta.side)
            price = execution_price(
                side=_closing_side(meta.side),
                kind=FillKind.MARKET,
                bid=bid,
                ask=ask,
                level=None,
                slippage_price=self.slip,
            )
            if price is None:
                continue
            unreal += self.fx.to_eur(
                quote_pnl(position_side, meta.entry_price, float(price), meta.lots, self.instrument.contract_size),
                self.instrument.quote,
                when_ns,
                bid,
                ask,
            )
            unreal -= self._cash_cost(meta.lots, when_ns, bid, ask)
        return self.cash + unreal

    def _cash_cost(self, lots: float, when_ns: int, bid: float, ask: float) -> float:
        amount, currency = self.costs.commission_cash(lots)
        if amount == 0.0:
            return 0.0
        return -self.fx.to_eur(-amount, currency, when_ns, bid, ask)

    def _open_positions(self) -> bool:
        return any(meta is not None and self.pos_side[i] != 0 for i, meta in enumerate(self.pos_meta))

    def _alloc_order(self) -> int:
        for i, meta in enumerate(self.ord_meta):
            if meta is None and self.ord_alive[i] == 0:
                return i
        return -1

    def _find_order(self, order_id: int) -> int:
        for i, meta in enumerate(self.ord_meta):
            if meta is not None and meta.id == order_id and self.ord_alive[i] != 0:
                return i
        return -1

    def _find_position(self, position_id: int) -> int:
        for i, meta in enumerate(self.pos_meta):
            if meta is not None and meta.id == position_id and self.pos_side[i] != 0:
                return i
        return -1

    def _take_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value


def _closing_side(position_side: int) -> Side:
    """A long is closed by selling at the bid; a short is closed by buying at the ask."""
    return Side.SELL if int(position_side) == int(Side.BUY) else Side.BUY


def _as_list(emitted) -> list:
    if emitted is None:
        return []
    if isinstance(emitted, (list, tuple)):
        return list(emitted)
    return [emitted]


def _iso(ns: int) -> str:
    """UTC timestamp from numpy datetime64[ns] nanoseconds."""
    seconds, rem = divmod(int(ns), 1_000_000_000)
    micros = rem // 1_000
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micros)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
