"""Brokers for the NQ demo: SimBroker (replay tests) and IBBroker (Interactive Brokers paper via IB Gateway, ib_async).

All quantities are MNQ contracts, signed (+ buy / - sell). The engine keeps per-strategy virtual positions; the broker
holds the net position. IBBroker also keeps one broker-side STOP order on the net position (disaster stop) so a crash of
the engine never leaves an unprotected position.
"""
import logging, time
import numpy as np

log = logging.getLogger("apex.live.broker")


class SimBroker:
    """Fills market orders at the last trade price +/- 1 tick (replay / dry run). $0.62/side MNQ commission (IB-like)."""
    def __init__(self, tape, slip_ticks=1, fee=0.62):
        self.tape, self.slip, self.fee = tape, slip_ticks * 0.25, fee
        self.net = 0; self.cash = 0.0; self.fills = []

    def market(self, qty, tag=""):
        px = self.tape.last_price() + (self.slip if qty > 0 else -self.slip)
        self.net += qty; self.cash -= qty * px * 2.0 + abs(qty) * self.fee
        self.fills.append((qty, px, tag)); return px

    def realized(self):
        return self.cash + self.net * self.tape.last_price() * 2.0

    def equity(self): return None
    def set_disaster_stop(self, net, ref_px, pts): pass


class IBBroker:
    """IB Gateway / TWS paper account. Contract = MNQ outright matching the Databento front month (e.g. 'NQZ6' -> MNQ Dec 2026)."""
    def __init__(self, host, port, client_id, symbol="MNQ"):
        from ib_async import IB
        self.ib = IB(); self.host, self.port, self.cid, self.symbol = host, port, client_id, symbol
        self.contract = None; self.dstop = None; self._real = 0.0

    def connect(self):
        self.ib.connect(self.host, self.port, clientId=self.cid, timeout=20)
        log.info("IB connected: accounts %s", self.ib.managedAccounts())
        if not any(a.startswith("D") for a in self.ib.managedAccounts()):
            raise RuntimeError("Refusing to run: the connected IB account is not a paper (D...) account")

    def set_contract(self, databento_symbol):
        """'NQZ6' -> MNQ Dec 2026. Called on start and whenever the Databento front month changes."""
        from ib_async import Future
        code, y = databento_symbol[2], databento_symbol[3:]
        month = "FGHJKMNQUVXZ".index(code) + 1
        year = 2020 + int(y) if len(y) == 1 else 2000 + int(y)
        c = Future(self.symbol, f"{year}{month:02d}", "CME")
        self.contract = self.ib.qualifyContracts(c)[0]
        log.info("IB contract %s", self.contract)

    def market(self, qty, tag=""):
        from ib_async import MarketOrder
        if qty == 0: return np.nan
        o = MarketOrder("BUY" if qty > 0 else "SELL", abs(qty)); o.orderRef = tag[:40]
        tr = self.ib.placeOrder(self.contract, o)
        t0 = time.time()
        while not tr.isDone() and time.time() - t0 < 15: self.ib.sleep(0.05)
        if tr.orderStatus.status != "Filled":
            log.error("order not filled: %s %s", tag, tr.orderStatus); return np.nan
        return tr.orderStatus.avgFillPrice

    def equity(self):
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                try: return float(v.value)
                except ValueError: pass
        return None

    def realized(self): return 0.0

    def position(self):
        return int(sum(p.position for p in self.ib.positions() if p.contract.conId == getattr(self.contract, "conId", None)))

    def set_disaster_stop(self, net, ref_px, pts):
        """One broker-side STOP on the net position at ref_px -/+ pts (cancelled when flat)."""
        from ib_async import StopOrder
        if self.dstop is not None:
            self.ib.cancelOrder(self.dstop.order); self.dstop = None
        if net == 0 or ref_px != ref_px: return
        side = "SELL" if net > 0 else "BUY"
        lvl = round((ref_px - pts if net > 0 else ref_px + pts) * 4) / 4
        o = StopOrder(side, abs(net), lvl); o.orderRef = "APEX disaster stop"; o.tif = "GTC"
        self.dstop = self.ib.placeOrder(self.contract, o)

    def sleep(self, s): self.ib.sleep(s)
