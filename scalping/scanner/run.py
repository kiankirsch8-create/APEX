"""Small-cap momentum scanner (Ross Cameron's 5 pillars) on IBKR scanners + free float/news.

    python -m scalping.scanner.run            # live: needs IB Gateway/TWS running with US stock data
    python -m scalping.scanner.run --demo     # simulated market, to see the dashboard without IBKR
Then open http://127.0.0.1:8765 in a browser.
"""
import argparse, logging, random, time
from datetime import datetime, timezone
from . import config as C, server
from .pillars import Candidate, score

log = logging.getLogger("scanner")


def rank(rows):
    return sorted(rows, key=lambda r: (r["all5"], r["pillars"], r["change_pct"] or -999), reverse=True)


def live():
    from .ib_feed import IBFeed
    from .floatdata import FloatCache
    from .news import News
    feed, floats, news = IBFeed(), FloatCache(), News()
    news.start()
    server.STATE["status"] = "connecting to IBKR"
    feed.connect()
    seen = {}
    while True:
        feed.sleep(C.REFRESH_SEC)
        syms = feed.scan_symbols()
        feed.sync_quotes(syms)
        floats.refresh_async([s for s, _ in syms])
        now = datetime.now(timezone.utc); rows = []
        for s in list(feed.tickers):
            seen.setdefault(s, now.isoformat())
            q = feed.quote(s)
            heads = sorted(news.recent(s, now) + feed.headlines(s), key=lambda n: n["time"], reverse=True)
            rows.append(score(Candidate(symbol=s, float_shares=floats.get(s), news=heads, first_seen=seen[s], **q), now))
        server.STATE.update(rows=rank(rows), updated=now.isoformat(),
                            status="live" if rows else "live · no movers from IB yet (market closed?)")


def demo():
    """Random-walk market so the dashboard can be checked without IBKR. Not real data."""
    rnd = random.Random(7)
    names = ["ABCD", "ZAPX", "QWER", "MOMO", "FLTR", "NEWZ", "GAPP", "RUNR", "TICK", "LOWF", "HALT", "VOLX"]
    st = {}
    for s in names:
        st[s] = dict(prev=rnd.uniform(1.5, 15), vol=rnd.uniform(2e5, 4e6), avg=rnd.uniform(3e5, 3e6),
                     float=rnd.choice([2.1e6, 4.5e6, 8e6, 12e6, 35e6, None]), drift=rnd.uniform(-0.002, 0.012))
        st[s]["last"] = st[s]["prev"] * rnd.uniform(0.95, 1.6)
    heads = {"ABCD": "ABCD announces FDA clearance for lead device", "MOMO": "MOMO signs $40M supply agreement",
             "NEWZ": "NEWZ reports record Q3 revenue", "GAPP": "GAPP enters partnership with major retailer",
             "LOWF": "LOWF receives Nasdaq compliance notice"}
    first = {s: datetime.now(timezone.utc).isoformat() for s in names}
    while True:
        now = datetime.now(timezone.utc); rows = []
        for s, x in st.items():
            x["last"] *= 1 + x["drift"] * rnd.uniform(-1, 1.5)
            x["vol"] *= rnd.uniform(1.0, 1.02)
            n = [dict(time=now.isoformat(), source="DEMO", title=heads[s])] if s in heads else []
            rows.append(score(Candidate(symbol=s, last=x["last"], prev_close=x["prev"], volume=x["vol"],
                                        avg_volume=x["avg"], float_shares=x["float"], news=n, first_seen=first[s]), now))
        server.STATE.update(rows=rank(rows), updated=now.isoformat(), status="demo")
        time.sleep(C.REFRESH_SEC)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--demo", action="store_true"); a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server.start(C.HTTP_PORT)
    log.info("dashboard on http://127.0.0.1:%d", C.HTTP_PORT)
    demo() if a.demo else live()


if __name__ == "__main__":
    main()
