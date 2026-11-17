"""Live trade tape in the exact format of scalping_data/nq_trades (ts ns UTC, seq, price, size, side +1/-1/0, session).

Session = CME trading day: Chicago time + 7h (same rule as build_nq_trades.py). Completed sessions are written to
{STORE}/tape/{session}.parquet so restarts and later research reuse them. Memory keeps the newest `keep` sessions of trades.
"""
import os
import numpy as np, pandas as pd


def session_of(ts_ns):
    t = pd.to_datetime(np.asarray(ts_ns), utc=True).tz_convert("America/Chicago") + pd.Timedelta(hours=7)
    return np.asarray(t.strftime("%Y-%m-%d"))


class Tape:
    COLS = ["ts", "seq", "price", "size", "side", "session"]

    def __init__(self, store, keep=12):
        self.dir = os.path.join(store, "tape"); os.makedirs(self.dir, exist_ok=True)
        self.keep = keep
        self.done = {}                     # session -> DataFrame (completed, in memory)
        self.buf = []                      # current-session rows (tuples)
        self.cur = None
        self.last_ts = 0

    # -------------------------------------------------------------- history
    def load_stored(self, sessions=None):
        files = sorted(f for f in os.listdir(self.dir) if f.endswith(".parquet"))
        for f in files[-(sessions or self.keep):]:
            self.done[f[:-8]] = pd.read_parquet(os.path.join(self.dir, f))
        if self.done: self.last_ts = int(max(df.ts.iloc[-1] for df in self.done.values()))

    def add_history(self, df):
        """df with columns ts, seq, price, size, side (session computed here); used for the Databento backfill."""
        df = df[df.ts > self.last_ts].copy()
        if df.empty: return
        df["session"] = session_of(df.ts.values)
        for s, g in df.groupby("session"):
            g = g[self.COLS].sort_values(["ts", "seq"], kind="stable").reset_index(drop=True)
            self.done[s] = pd.concat([self.done[s], g], ignore_index=True) if s in self.done else g
        self.last_ts = int(df.ts.max())
        self._prune()

    # -------------------------------------------------------------- live
    def add(self, ts, seq, price, size, side):
        if ts < self.last_ts: return               # out-of-order duplicate after a reconnect
        s = session_of([ts])[0]
        if self.cur is None: self.cur = s
        if s != self.cur: self.roll(s)
        self.buf.append((ts, seq, price, size, side)); self.last_ts = ts

    def roll(self, new_session):
        if self.buf:
            g = pd.DataFrame(self.buf, columns=self.COLS[:-1]); g["session"] = self.cur
            prev = self.done.get(self.cur)
            g = pd.concat([prev, g], ignore_index=True) if prev is not None else g
            self.done[self.cur] = g
            g.to_parquet(os.path.join(self.dir, f"{self.cur}.parquet"), index=False)
        self.buf = []; self.cur = new_session; self._prune()

    def _prune(self):
        for s in sorted(self.done)[:-self.keep]:
            del self.done[s]

    # -------------------------------------------------------------- views
    def frame(self, n_sessions):
        """Newest n sessions (including the live one) as a V.load-style frame (categorical session, sorted).
        Completed sessions are prepared once and cached; only the live buffer is rebuilt each call."""
        live = None
        if self.buf:
            live = pd.DataFrame(self.buf, columns=self.COLS[:-1])
            if self.cur in self.done:
                live = pd.concat([self.done[self.cur][self.COLS[:-1]], live], ignore_index=True)
        done = [s for s in sorted(self.done) if not (live is not None and s == self.cur)]
        want = done[-(n_sessions - (1 if live is not None else 0)):] if n_sessions > (1 if live is not None else 0) else []
        names = want + ([self.cur] if live is not None else [])
        if not names: return pd.DataFrame(columns=self.COLS)
        key = tuple(want)
        if getattr(self, "_fkey", None) != key:
            if want:
                h = pd.concat([self.done[s] for s in want], ignore_index=True)
                h["session"] = h.session.astype(str)
                h["size"] = h["size"].astype(np.int32); h["side"] = h.side.astype(np.int8)
                h = h.sort_values(["ts", "seq"], kind="stable").reset_index(drop=True)
            else:
                h = pd.DataFrame(columns=self.COLS)
            self._fkey, self._fhist = key, h
        h = self._fhist
        cats = pd.CategoricalDtype(names)
        if live is not None:
            live = live.sort_values(["ts", "seq"], kind="stable")
            live["session"] = self.cur
            live["size"] = live["size"].astype(np.int32); live["side"] = live.side.astype(np.int8)
            d = pd.concat([h, live], ignore_index=True)
        else:
            d = h.copy()
        d["session"] = d.session.astype(cats)
        return d

    def last_price(self):
        if self.buf: return self.buf[-1][2]
        if self.done: return float(self.done[sorted(self.done)[-1]].price.iloc[-1])
        return np.nan
