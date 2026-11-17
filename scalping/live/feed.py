"""Databento feeds: live NQ trades (GLBX.MDP3, front month by volume = NQ.v.0) and historical backfill for warm-up.

Same field meaning as build_nq_trades.py: ts = ts_event (ns UTC), seq = sequence, side 'B' -> +1, 'A' -> -1, 'N' -> 0.
The live client runs in its own thread and pushes (ts, seq, price, size, side) into a queue; symbol changes (roll) are
reported as ('SYMBOL', 'NQZ6').
"""
import threading, queue, logging, time
import numpy as np, pandas as pd

log = logging.getLogger("apex.live.feed")
SIDE = {"B": 1, "A": -1, "N": 0}
SYMBOL = "NQ.v.0"


def backfill(key, start, end):
    """Historical trades for NQ.v.0 between start and end (UTC). Costs Databento credits: check the estimate first."""
    import databento as db
    c = db.Historical(key)
    est = c.metadata.get_cost(dataset="GLBX.MDP3", schema="trades", stype_in="continuous", symbols=[SYMBOL], start=start, end=end)
    log.info("backfill %s -> %s, Databento cost estimate $%.2f", start, end, est)
    data = c.timeseries.get_range(dataset="GLBX.MDP3", schema="trades", stype_in="continuous", symbols=[SYMBOL], start=start, end=end)
    df = data.to_df().reset_index()
    return pd.DataFrame({"ts": df.ts_event.astype("int64"), "seq": df.sequence.astype("int64"), "price": df.price.astype(float),
                         "size": df["size"].astype("int32"), "side": df.side.map(SIDE).fillna(0).astype("int8")}), est


class LiveFeed:
    def __init__(self, key):
        self.key = key; self.q = queue.Queue(maxsize=2_000_000); self.symbol = None
        self._stop = False; self.t = None; self.last_msg = time.time()

    def start(self):
        self.t = threading.Thread(target=self._run, daemon=True); self.t.start()

    def _run(self):
        import databento as db
        while not self._stop:
            try:
                c = db.Live(key=self.key)
                c.subscribe(dataset="GLBX.MDP3", schema="trades", stype_in="continuous", symbols=[SYMBOL])
                for rec in c:
                    self.last_msg = time.time()
                    if isinstance(rec, db.SymbolMappingMsg):
                        sym = rec.stype_out_symbol
                        if sym and sym != self.symbol and "-" not in sym:
                            self.symbol = sym; self.q.put(("SYMBOL", sym))
                    elif isinstance(rec, db.TradeMsg):
                        sd = rec.side if isinstance(rec.side, str) else chr(rec.side)
                        self.q.put((int(rec.ts_event), int(rec.sequence), rec.price / 1e9, int(rec.size), SIDE.get(sd, 0)))
                    if self._stop: break
            except Exception:
                log.exception("live feed dropped; reconnecting in 5 s")
                time.sleep(5)

    def stop(self): self._stop = True


class ReplayFeed:
    """Streams historical sessions from scalping_data/nq_trades for parity tests (no network)."""
    def __init__(self, month_file, sessions):
        x = pd.read_parquet(month_file, columns=["ts", "seq", "price", "size", "side", "session", "symbol", "degraded"])
        x = x[x.session.astype(str).isin(sessions) & ~x.degraded].sort_values(["ts", "seq"], kind="stable")
        self.x = x; self.symbol = str(x.symbol.iloc[0])

    def __iter__(self):
        for r in self.x[["ts", "seq", "price", "size", "side"]].itertuples(index=False):
            yield (int(r.ts), int(r.seq), float(r.price), int(r.size), int(r.side))
