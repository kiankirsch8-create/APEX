"""Replay a past morning on the scanner dashboard, using IBKR historical data (works on weekends).

    python3 -m scalping.scanner.replay 2024-08-21 VRAX GOVX BPTH GDC MNPR APDN
    python3 -m scalping.scanner.replay 2024-06-13 ZAPP --speed 120 --start 07:00

IBKR has no historical *scanner*, so you name the tickers (e.g. from Cameron's recaps). For each one we load the day's
1-minute bars incl. pre-market, the previous 30 daily bars (previous close, average volume) and IB news headlines,
then step a simulated clock through the day and score the 5 pillars minute by minute.
Float is today's value (Finviz/Yahoo) — after reverse splits it can differ from the float on the replayed day.
Delisted tickers can't be loaded from IBKR; they are skipped with a message.
"""
import argparse, logging, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from ib_async import IB, Stock
from . import config as C, server
from .pillars import Candidate, score
from .floatdata import FloatCache
from .run import rank

log = logging.getLogger("scanner.replay")
NY = ZoneInfo("America/New_York")


def load(ib, sym, day):
    c = Stock(sym, "SMART", "USD")
    if not ib.qualifyContracts(c):
        log.warning("%s: not found at IBKR (delisted or renamed?) - skipped", sym); return None
    end = f"{day.strftime('%Y%m%d')} 20:00:00 US/Eastern"
    m1 = ib.reqHistoricalData(c, end, "1 D", "1 min", "TRADES", useRTH=False, formatDate=2)
    d1 = ib.reqHistoricalData(c, f"{day.strftime('%Y%m%d')} 00:00:01 US/Eastern", "35 D", "1 day", "TRADES",
                              useRTH=True, formatDate=1)
    if not m1 or not d1:
        log.warning("%s: no historical data for %s - skipped", sym, day.date()); return None
    prev = [b for b in d1 if b.date < day.date()]
    news = []
    try:
        prov = "+".join(p.code for p in ib.reqNewsProviders())
        hs = ib.reqHistoricalNews(c.conId, prov, (day - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00.0"),
                                  (day + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00.0"), 20)
        for h in hs:
            t = h.time if h.time.tzinfo else h.time.replace(tzinfo=timezone.utc)
            news.append(dict(time=t.astimezone(timezone.utc).isoformat(), source=f"IB {h.providerCode}", title=h.headline))
    except Exception as e:
        log.info("%s: no IB news history (%s)", sym, e)
    bars = [(b.date.astimezone(timezone.utc), b.close, b.high, b.volume) for b in m1]
    log.info("%s: %d minute bars, prev close %.2f, %d headlines", sym, len(bars), prev[-1].close if prev else float("nan"), len(news))
    return dict(bars=bars, prev_close=prev[-1].close if prev else None,
                avg_volume=(sum(b.volume for b in prev[-30:]) / len(prev[-30:])) if prev else None, news=news)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date"); ap.add_argument("tickers", nargs="+")
    ap.add_argument("--speed", type=float, default=60, help="simulated seconds per real second (60 = 1 minute per second)")
    ap.add_argument("--start", default="04:00", help="New York time to start (pre-market opens 04:00)")
    ap.add_argument("--end", default="12:00")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    day = datetime.strptime(a.date, "%Y-%m-%d").replace(tzinfo=NY)
    server.start(C.HTTP_PORT)
    server.STATE.update(status=f"replay {a.date}: loading data from IBKR…", rows=[])
    log.info("dashboard on http://127.0.0.1:%d", C.HTTP_PORT)

    ib = IB(); ib.connect(C.IB_HOST, C.IB_PORT, clientId=C.IB_CLIENT_ID + 1, readonly=True)
    data = {s.upper(): d for s in a.tickers if (d := load(ib, s.upper(), day))}
    floats = FloatCache()
    for s in data: floats.fetch(s)
    ib.disconnect()
    if not data:
        server.STATE.update(status="replay: no ticker could be loaded"); log.error("nothing to replay"); return

    hh, mm = map(int, a.start.split(":")); eh, em = map(int, a.end.split(":"))
    t = day.replace(hour=hh, minute=mm); t_end = day.replace(hour=eh, minute=em)
    seen = {}
    while t <= t_end:
        tu = t.astimezone(timezone.utc); rows = []
        for s, d in data.items():
            past = [b for b in d["bars"] if b[0] + timedelta(minutes=1) <= tu]      # bars completed by now
            if not past: continue
            seen.setdefault(s, tu.isoformat())
            news = [n for n in d["news"] if n["time"] <= tu.isoformat() and n["time"] >= (tu - timedelta(hours=C.NEWS_LOOKBACK_H)).isoformat()]
            news.sort(key=lambda n: n["time"], reverse=True)
            rows.append(score(Candidate(symbol=s, last=past[-1][1], prev_close=d["prev_close"], volume=sum(b[3] for b in past),
                                        avg_volume=d["avg_volume"], high=max(b[2] for b in past), float_shares=floats.get(s),
                                        news=news, first_seen=seen[s]), t))
        server.STATE.update(rows=rank(rows), updated=datetime.now(timezone.utc).isoformat(), sim_time=t.isoformat(),
                            status=f"replay {a.date} · {t.strftime('%H:%M')} New York · {a.speed:g}× speed")
        time.sleep(60 / a.speed)
        t += timedelta(minutes=1)
    server.STATE["status"] = f"replay {a.date} finished"
    log.info("replay finished - dashboard stays up, Ctrl+C to quit")
    while True: time.sleep(3600)


if __name__ == "__main__":
    main()
