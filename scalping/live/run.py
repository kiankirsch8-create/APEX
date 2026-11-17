"""Start the NQ demo: Databento live trades -> engine -> IBKR paper (MNQ).

    set DATABENTO_API_KEY=...   (environment variable; never in code or git)
    python -m scalping.live.run              # live
    python -m scalping.live.run --check      # connect, backfill, print state, place no orders

Refuses to run on a non-paper IB account. Logs to {STORE}/logs, trades to {STORE}/journal/YYYY-MM-DD.csv.
"""
import os, sys, time, logging, argparse
import pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orderflow"))
from . import config as CF
from .tape import Tape
from .engine import Engine
from .broker import IBBroker
from .feed import LiveFeed, backfill


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--check", action="store_true"); a = ap.parse_args()
    os.makedirs(os.path.join(CF.STORE, "logs"), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(CF.STORE, "logs", f"{pd.Timestamp.utcnow():%Y-%m-%d}.log")),
                                  logging.StreamHandler()])
    log = logging.getLogger("apex.live")
    if not CF.DATABENTO_KEY: sys.exit("DATABENTO_API_KEY is not set")
    for n, s in CF.STRATEGIES.items(): log.info("strategy %-38s risk %.2f%%", n, s["risk"] * 100)

    tape = Tape(CF.STORE, keep=CF.WARMUP_SESSIONS + 2)
    tape.load_stored(CF.WARMUP_SESSIONS + 1)
    now = pd.Timestamp.utcnow().floor("min")
    start = pd.Timestamp(tape.last_ts, tz="UTC") if tape.last_ts else now - pd.Timedelta(days=40)
    if now - start > pd.Timedelta(minutes=2):
        df, est = backfill(CF.DATABENTO_KEY, start.isoformat(), now.isoformat())
        log.info("backfilled %d trades (est. $%.2f)", len(df), est)
        tape.add_history(df)
        for s in sorted(tape.done)[:-1]:                         # persist completed backfilled sessions
            p = os.path.join(tape.dir, f"{s}.parquet")
            if not os.path.exists(p): tape.done[s].to_parquet(p, index=False)

    br = IBBroker(CF.IB_HOST, CF.IB_PORT, CF.IB_CLIENT_ID, CF.IB_SYMBOL); br.connect()
    feed = LiveFeed(CF.DATABENTO_KEY); feed.start()
    eng = Engine(tape, br)
    if br.position() != 0:
        log.warning("IB shows an open MNQ position (%d) at start: flatten it manually or it will be ignored by the engine", br.position())
    if a.check:
        t0 = time.time()
        while time.time() - t0 < 60:
            item = feed.q.get()
            if item[0] == "SYMBOL": br.set_contract(item[1]); log.info("front month %s", item[1]); break
        eng.cycle(force=True)
        for n, s in eng.S.items():
            v = s.last_view or {}
            log.info("%s: open=%s pending=%d closed today=%d", n, v.get("open"), len(v.get("pending", [])), len(v.get("closed", [])))
        return
    last_hb = time.time()
    while True:
        drained = 0
        while not feed.q.empty() and drained < 50_000:
            item = feed.q.get_nowait(); drained += 1
            if item[0] == "SYMBOL":
                br.set_contract(item[1]); log.info("front month %s", item[1]); continue
            ts, seq, px, sz, sd = item
            tape.add(ts, seq, px, sz, sd)
            if br.contract is not None: eng.on_tick(ts, px)
        if br.contract is not None: eng.cycle()
        if time.time() - last_hb > 300:
            log.info("heartbeat: last trade %s, feed age %.0fs, net IB position %d, day pnl %.0f, halted %s",
                     pd.Timestamp(tape.last_ts, tz="UTC"), time.time() - feed.last_msg, br.position(), eng.day_pnl, eng.halted)
            last_hb = time.time()
        br.sleep(0.05)


if __name__ == "__main__":
    main()
