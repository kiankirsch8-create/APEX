"""Bookmap-style order-flow window: liquidity heatmap, volume bubbles, best bid/offer, current order book (COB) and
cumulative volume profile (CVP) for one symbol, from IBKR.

    python3 -m scalping.scanner.book GDC          # a stock
    python3 -m scalping.scanner.book NQ           # NQ front-month future
    python3 -m scalping.scanner.book --demo       # simulated market (to see it any time)
Open http://127.0.0.1:8766  — or click a ticker in the scanner, it switches this window to that symbol.

Depth (heatmap + order book) shows the Level 2 feeds you subscribe to at IBKR, merged ("smart depth"). With only the free
IEX depth it shows a thin slice of the real book; with Nasdaq TotalView it is complete for Nasdaq stocks. Volume bubbles
and best bid/offer come from Level 1 data and are complete already.
Read-only: never places orders.
"""
import argparse, json, logging, os, random, threading, time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from . import config as C

log = logging.getLogger("scanner.book")
HERE = os.path.dirname(__file__)
PORT = int(os.environ.get("BOOK_PORT", "8766"))
LOCK = threading.Lock()
STATE = dict(sym=None, status="starting", bids=[], asks=[], bb=None, ba=None, last=None, depth_note="")
TRADES = deque(maxlen=20000)       # dicts with id, t (epoch s), p, s, side (+1 buy aggressor, -1 sell, 0 unknown)
SEQ = [0]
WANT = {"sym": None}


def add_trade(t, p, s, side):
    with LOCK:
        SEQ[0] += 1
        TRADES.append(dict(id=SEQ[0], t=t, p=p, s=s, side=side))


def side_of(p, bb, ba, last):
    if ba is not None and p >= ba: return 1
    if bb is not None and p <= bb: return -1
    if last is not None: return 1 if p > last else (-1 if p < last else 0)
    return 0


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            body = open(os.path.join(HERE, "book.html"), "rb").read(); ctype = "text/html; charset=utf-8"
        elif u.path == "/state":
            since = int(q.get("since", ["0"])[0])
            with LOCK:
                d = dict(STATE); d["trades"] = [t for t in TRADES if t["id"] > since][-3000:]
            body = json.dumps(d).encode(); ctype = "application/json"
        elif u.path == "/subscribe":
            WANT["sym"] = q.get("sym", [""])[0].strip().upper() or WANT["sym"]
            body = b'{"ok":true}'; ctype = "application/json"
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:%d" % C.HTTP_PORT); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def contract_for(sym):
    from ib_async import Stock, ContFuture
    if sym in ("NQ", "MNQ", "ES", "MES"):
        return ContFuture(sym, "CME")
    return Stock(sym, "SMART", "USD")


def live(initial):
    from ib_async import IB
    ib = IB()
    ib.connect(C.IB_HOST, C.IB_PORT, clientId=C.IB_CLIENT_ID + 2, readonly=True)
    ib.reqMarketDataType(2)

    def on_error(reqId, code, msg, contract):
        if code in (2104, 2106, 2158, 2119): return
        log.warning("IB %s: %s", code, msg)
        if code in (309, 310, 316, 317, 354, 10089, 10090, 10092, 10167, 10168, 10186, 10187, 10197):
            with LOCK: STATE["depth_note"] = f"IB {code}: {msg}"
    ib.errorEvent += on_error

    WANT["sym"] = initial; cur = None; subs = None
    while True:
        if WANT["sym"] and WANT["sym"] != cur:
            if subs:
                c0, d0, l0, t0 = subs
                for f in (lambda: ib.cancelMktDepth(c0, True), lambda: ib.cancelMktData(c0), lambda: ib.cancelTickByTickData(c0, "AllLast")):
                    try: f()
                    except Exception: pass
            cur = WANT["sym"]
            with LOCK:
                TRADES.clear(); STATE.update(sym=cur, status=f"subscribing {cur}…", bids=[], asks=[], bb=None, ba=None, last=None, depth_note="")
            c = contract_for(cur)
            q = ib.qualifyContracts(c)
            if not q:
                with LOCK: STATE["status"] = f"{cur}: not found at IBKR"
                subs = None; continue
            c = q[0]
            depth = ib.reqMktDepth(c, numRows=50, isSmartDepth=True)
            l1 = ib.reqMktData(c, "", False)
            tbt = ib.reqTickByTickData(c, "AllLast", 0, False)

            def on_tbt(tk, l1=l1):
                for x in tk.tickByTicks:
                    p, s = float(x.price), float(x.size)
                    bb, ba = (l1.bid if l1.bid and l1.bid > 0 else None), (l1.ask if l1.ask and l1.ask > 0 else None)
                    with LOCK: last = STATE["last"]
                    add_trade(x.time.timestamp(), p, s, side_of(p, bb, ba, last))
                    with LOCK: STATE["last"] = p
            tbt.updateEvent += on_tbt
            subs = (c, depth, l1, tbt)
            with LOCK: STATE["status"] = "live"
        if subs:
            c, depth, l1, tbt = subs
            num = lambda v: float(v) if v is not None and v == v and v > 0 else None
            with LOCK:
                STATE["bids"] = [[float(x.price), float(x.size)] for x in depth.domBids if x.size > 0]
                STATE["asks"] = [[float(x.price), float(x.size)] for x in depth.domAsks if x.size > 0]
                STATE["bb"], STATE["ba"] = num(l1.bid), num(l1.ask)
                if STATE["last"] is None: STATE["last"] = num(l1.last) or num(l1.close)
                if not STATE["bids"] and not STATE["asks"] and not STATE["depth_note"]:
                    STATE["depth_note"] = "no depth yet (market closed, or no Level 2 subscription for this symbol)"
                elif STATE["bids"] or STATE["asks"]:
                    STATE["depth_note"] = ""
        ib.sleep(0.1)


def demo():
    """Simulated market with resting walls, pulled orders and aggressive bursts. Not real data."""
    rnd = random.Random(3); tick = 0.01; mid = 4.20
    walls = {round(mid + k * tick * 7, 2): rnd.uniform(5e3, 3e4) for k in range(-6, 7) if k}
    with LOCK: STATE.update(sym="DEMO", status="demo", depth_note="simulated data")
    last = mid
    while True:
        mid = max(1.0, mid + rnd.choice([-1, 0, 0, 1]) * tick * (1 if rnd.random() < 0.85 else 3))
        if rnd.random() < 0.02:  # a wall moves
            k = rnd.choice(list(walls)); walls.pop(k); walls[round(mid + rnd.choice([-1, 1]) * rnd.randint(3, 25) * tick, 2)] = rnd.uniform(8e3, 4e4)
        bb, ba = round(mid - tick / 2, 2), round(mid + tick / 2, 2)
        bids, asks = [], []
        for i in range(40):
            pb, pa = round(bb - i * tick, 2), round(ba + i * tick, 2)
            base = lambda: rnd.expovariate(1 / 1500)
            bids.append([pb, base() + walls.get(pb, 0)]); asks.append([pa, base() + walls.get(pa, 0)])
        n = rnd.choice([0, 0, 1, 2, 3, 6]) if rnd.random() < 0.9 else 25
        now = time.time(); side = 1 if rnd.random() < 0.55 else -1
        for _ in range(n):
            p = ba if side > 0 else bb
            add_trade(now, p, float(int(rnd.expovariate(1 / 300)) + 1), side); last = p
        with LOCK: STATE.update(bids=bids, asks=asks, bb=bb, ba=ba, last=last)
        time.sleep(0.15)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("symbol", nargs="?", default=None); ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("order-flow window on http://127.0.0.1:%d", PORT)
    demo() if a.demo else live((a.symbol or "SPY").upper())


if __name__ == "__main__":
    main()
