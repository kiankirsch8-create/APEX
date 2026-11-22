"""IBKR side: server-side market scanners find the movers, live quotes for the candidates, and IB news headlines.

IB's scanners watch the whole US market on their servers, so we only stream the ~50 names they return.
Read-only: this module never places orders.
"""
import logging, time
from datetime import datetime, timezone, timedelta
from ib_async import IB, ScannerSubscription, TagValue
from . import config as C

log = logging.getLogger("scanner.ib")


class IBFeed:
    def __init__(self):
        self.ib = IB()
        self.scans = {}              # scanCode -> ScanDataList
        self.tickers = {}            # sym -> (contract, Ticker)
        self.providers = ""
        self.news_checked = {}       # sym -> last time we asked IB for headlines
        self.ib_news = {}            # sym -> [{"time","source","title"}]

    def connect(self):
        self.ib.connect(C.IB_HOST, C.IB_PORT, clientId=C.IB_CLIENT_ID, readonly=True)
        # 2 = "frozen": live while the market is open, last recorded values when it's closed (evenings, weekends),
        # so the dashboard still shows the last session's movers outside market hours
        self.ib.reqMarketDataType(2)
        try:
            self.providers = "+".join(p.code for p in self.ib.reqNewsProviders())
            log.info("IB news providers: %s", self.providers or "(none)")
        except Exception as e:
            log.warning("no IB news providers: %s", e)
        filt = [TagValue("priceAbove", str(C.MIN_PRICE)), TagValue("priceBelow", str(C.MAX_PRICE))]
        for code in C.SCAN_CODES:
            sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode=code,
                                      numberOfRows=C.SCAN_ROWS)
            self.scans[code] = self.ib.reqScannerSubscription(sub, [], filt)
            log.info("scanner %s subscribed", code)

    def scan_symbols(self):
        """Merged scanner results, best rank first."""
        best = {}
        for code, rows in self.scans.items():
            for r in rows:
                c = r.contractDetails.contract
                best[c.symbol] = min(best.get(c.symbol, (999, None))[0], r.rank), c
        return [(s, c) for s, (rk, c) in sorted(best.items(), key=lambda kv: kv[1][0])]

    def sync_quotes(self, wanted):
        """Stream quotes for `wanted` [(sym, contract)], cancel the rest; keeps within MAX_LIVE_LINES."""
        wanted = wanted[:C.MAX_LIVE_LINES]
        keep = {s for s, _ in wanted}
        for s in list(self.tickers):
            if s not in keep:
                self.ib.cancelMktData(self.tickers[s][0]); del self.tickers[s]
        for s, c in wanted:
            if s not in self.tickers:
                # 165 = misc stats (avVolume = 90-day average daily volume)
                self.tickers[s] = (c, self.ib.reqMktData(c, "165", snapshot=False))

    def quote(self, sym):
        c, t = self.tickers[sym]
        num = lambda x: x if (x is not None and x == x and x > 0) else None   # drop NaN / -1
        return dict(last=num(t.last) or num(t.marketPrice()), prev_close=num(t.close), volume=num(t.volume),
                    avg_volume=num(t.avVolume), high=num(t.high))

    def headlines(self, sym, every_sec=120):
        """IB news for a candidate, refreshed every 2 minutes."""
        if not self.providers or sym not in self.tickers: return self.ib_news.get(sym, [])
        if time.time() - self.news_checked.get(sym, 0) < every_sec: return self.ib_news.get(sym, [])
        self.news_checked[sym] = time.time()
        c = self.tickers[sym][0]
        end = datetime.now(timezone.utc); start = end - timedelta(hours=C.NEWS_LOOKBACK_H)
        try:
            hs = self.ib.reqHistoricalNews(c.conId, self.providers, start.strftime("%Y-%m-%d %H:%M:%S.0"),
                                           end.strftime("%Y-%m-%d %H:%M:%S.0"), 5)
            self.ib_news[sym] = [dict(time=h.time.replace(tzinfo=timezone.utc).isoformat() if h.time.tzinfo is None
                                      else h.time.astimezone(timezone.utc).isoformat(),
                                      source=f"IB {h.providerCode}", title=h.headline) for h in hs]
        except Exception as e:
            log.debug("IB news %s failed: %s", sym, e)
        return self.ib_news.get(sym, [])

    def sleep(self, s):
        self.ib.sleep(s)
