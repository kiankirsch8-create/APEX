"""NQ demo engine: mirrors the backtest code's decisions on live data at a broker (IBKR paper) in MNQ.

Per strategy, at most one base position (as in the backtests). Each cycle:
1. The strategy's backtest code runs on the live tape (strategies.py) -> closed trades, open position, resting orders.
2. reconcile(): open/close/adjust the broker position so it matches the backtest's open position.
3. Between cycles the tick guard works resting orders and protective stops/targets on every trade print, with the same
   rules as the backtests (stop on touch, target only when traded through by 1 tick, flat times).
Positions are virtual per strategy; the broker holds the NET MNQ position (IBKR nets one account). A broker-side
disaster stop protects the net position if this process dies.
"""
import os, time, json, csv, logging, threading
from bisect import bisect_left
import numpy as np, pandas as pd
from . import config as CF
from . import strategies as ST

TICK = 0.25
NS = 1_000_000_000
log = logging.getLogger("apex.live")


def ny_mod(ts_ns):
    t = pd.Timestamp(ts_ns, tz="UTC").tz_convert("America/New_York")
    return t.hour * 60 + t.minute


class Strat:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.pos = None            # dict(d, qty, model_fill, broker_fill, stop, tgt, entry_id, opened_ns, legs_qty, flat_mod)
        self.pending = []          # resting orders from the last view
        self.handled = set()       # model entry ids already traded (never re-enter the same model trade)
        self.last_view = None
        self.rng = None            # range-bar tracker for "bar" expiry: [hi, lo]
        self.recent_close = None   # (opened_ns, d) of the last position closed, to match the model's late view of it


class Engine:
    def __init__(self, tape, broker, store=CF.STORE, strategies=None, equity0=100_000.0, clock=None):
        self.tape, self.broker = tape, broker
        self.S = {n: Strat(n, c) for n, c in (strategies or CF.STRATEGIES).items()}
        self.cache = ST.BarCache()
        self.clock = clock or (lambda: time.time_ns())
        self.jdir = os.path.join(store, "journal"); os.makedirs(self.jdir, exist_ok=True)
        self.equity0 = equity0
        self.day = None; self.day_pnl = 0.0; self.halted = False
        self.last_cycle = {}; self.last_min = None
        self.rbar = None; self.vbar_closed = False

    # ------------------------------------------------------------------ journal
    def _j(self, row):
        row = dict(row); row["time_utc"] = pd.Timestamp(self.clock(), tz="UTC").isoformat()
        f = os.path.join(self.jdir, f"{pd.Timestamp(self.clock(), tz='UTC').tz_convert('America/New_York'):%Y-%m-%d}.csv")
        new = not os.path.exists(f)
        with open(f, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["time_utc", "strategy", "action", "d", "qty", "model_px", "broker_px",
                                               "slip_pts", "stop", "tgt", "why", "pnl_usd", "model_R", "note"])
            if new: w.writeheader()
            w.writerow({k: row.get(k, "") for k in w.fieldnames})
        log.info("%s %s %s", row.get("strategy"), row.get("action"), {k: row.get(k) for k in ("d", "qty", "model_px", "broker_px", "why")})

    # ------------------------------------------------------------------ sizing / day
    def equity(self):
        e = self.broker.equity()
        return e if e and e > 0 else self.equity0 + self.broker.realized()

    def qty_for(self, s, dist):
        q = int(s.cfg["risk"] * self.equity() // (dist * 2.0))          # MNQ: $2 per point
        return max(q, 0)

    def _new_day(self, ts):
        d = pd.Timestamp(ts, tz="UTC").tz_convert("America/Chicago") + pd.Timedelta(hours=7)
        d = d.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.day_pnl, self.halted = d, 0.0, False

    # ------------------------------------------------------------------ position actions
    def _open(self, s, d, model_px, stop, tgt, dist, entry_id, flat_mod, why, legs=()):
        if self.halted or s.pos is not None: return
        q = self.qty_for(s, dist)
        if q < 1:
            self._j(dict(strategy=s.name, action="skip", d=d, model_px=model_px, why=why, note=f"size<1 MNQ (dist {dist:.2f})")); return
        px = self.broker.market(d * q, f"{s.name} open")
        s.pos = dict(d=d, qty=q, base_qty=q, model_fill=model_px, broker_fill=px, stop=stop, tgt=tgt, entry_id=entry_id,
                     opened_ns=self.clock(), flat_mod=flat_mod, legs_qty=0, dist=dist)
        if entry_id is not None: s.handled.add(entry_id)
        s.pending = []
        self._j(dict(strategy=s.name, action="open", d=d, qty=q, model_px=model_px, broker_px=px,
                     slip_pts=(px - model_px) * d if model_px == model_px else "", stop=stop, tgt=tgt, why=why))
        self._safety()

    def _close(self, s, model_px, why):
        p = s.pos
        if p is None: return
        px = self.broker.market(-p["d"] * p["qty"], f"{s.name} close")
        pnl = (px - p["broker_fill"]) * p["d"] * 2.0 * p["qty"]                     # broker_fill = average incl. G4 legs
        self.day_pnl += pnl
        R = (model_px - p["model_fill"]) * p["d"] / p["dist"] if model_px == model_px and p["dist"] else ""
        self._j(dict(strategy=s.name, action="close", d=p["d"], qty=p["qty"], model_px=model_px, broker_px=px,
                     slip_pts=(model_px - px) * p["d"] if model_px == model_px else "", why=why, pnl_usd=round(pnl, 2), model_R=R))
        s.recent_close = (p["entry_id"] if p["entry_id"] is not None else p["opened_ns"], p["d"])
        s.pos = None
        if self.day_pnl <= -CF.DAILY_LOSS_CAP * self.equity():
            self.halted = True; self._j(dict(strategy="ALL", action="halt", why="daily loss cap"))
        self._safety()

    def _safety(self):
        net = sum(s.pos["d"] * s.pos["qty"] for s in self.S.values() if s.pos)
        widest = max([s.pos["dist"] for s in self.S.values() if s.pos] + [0])
        self.broker.set_disaster_stop(net, self.tape.last_price(), max(CF.DISASTER_STOP_PTS, widest + 60))

    # ------------------------------------------------------------------ tick guard
    def on_tick(self, ts, price):
        self._new_day(ts)
        rb = self.rbar                                                # 40-tick range-bar tracker (same rule as V._range_ids)
        if rb is None or rb[2] != self.day:
            self.rbar = [price, price, self.day]
        else:
            hi, lo = max(rb[0], price), min(rb[1], price)
            if hi - lo > 40 * TICK + 1e-9:
                self.rbar = [price, price, self.day]; self.vbar_closed = True
            else:
                rb[0], rb[1] = hi, lo
        mod = None
        for s in self.S.values():
            p = s.pos
            if p is not None:
                d = p["d"]
                if (price <= p["stop"]) if d == 1 else (price >= p["stop"]):
                    self._close(s, p["stop"] - d * TICK, "stop (tick guard)"); continue
                if p["tgt"] == p["tgt"] and p["tgt"] is not None and ((price >= p["tgt"] + TICK) if d == 1 else (price <= p["tgt"] - TICK)):
                    self._close(s, p["tgt"], "target (tick guard)"); continue
                mod = ny_mod(ts) if mod is None else mod
                if p["flat_mod"] and p["flat_mod"] <= mod < 17 * 60:
                    self._close(s, price - d * TICK, "flat time"); continue
            elif s.pending and not self.halted:
                self._work_pending(s, ts, price)

    def _work_pending(self, s, ts, price):
        keep = []
        for o in s.pending:
            if ts < o["from_ns"]: keep.append(o); continue
            if isinstance(o.get("expire_ns"), (int, np.integer)) and ts >= o["expire_ns"]: continue
            if o.get("expire_ns") == "bar" and self._bar_over(s, o, ts, price): continue
            d = o["d"]
            if o["kind"] == "market":
                self._open(s, d, price + d * TICK, price - d * o["dist"], np.nan, o["dist"], None, o["flat_mod"], "market (next bar)")
                return
            cb = o.get("cancel_beyond")
            if o["kind"] == "limit":
                if (price <= cb) if d == 1 else (price >= cb): continue
                if (price <= o["lvl"] - TICK) if d == 1 else (price >= o["lvl"] + TICK):
                    self._open(s, d, o["lvl"], o["stop"], o["tgt"], o["dist"], None, o["flat_mod"], "limit"); return
            else:
                if (price >= o["lvl"]) if d == 1 else (price <= o["lvl"]):
                    fill = price + d * TICK                                   # same slip rule as the backtests
                    stop = o["stop"] if s.cfg["family"] != "valentini" else fill - d * o["dist"]
                    self._open(s, d, fill, stop, o["tgt"], abs(fill - stop), None, o["flat_mod"], "stop entry")
                    return
                if cb is not None and ((price < cb) if d == 1 else (price > cb)): continue
            keep.append(o)
        s.pending = keep

    def _bar_over(self, s, o, ts, price):
        """True once the unfinished bar the order belongs to has ended (range bar: range > 40 ticks; 1m: next minute)."""
        if s.cfg.get("bars") == "range":
            if s.rng is None or s.rng[2] != o["from_ns"]: s.rng = [price, price, o["from_ns"]]
            hi, lo = max(s.rng[0], price), min(s.rng[1], price)
            if hi - lo > 40 * TICK + 1e-9: return True
            s.rng[0], s.rng[1] = hi, lo; return False
        return ts // (60 * NS) > o["from_ns"] // (60 * NS)

    def _catch_up(self, s, until_ns):
        """Replay ticks of the unfinished bar that printed before the view arrived (orders are valid from bar start)."""
        if not s.pending or not self.tape.buf: return
        f = min(o["from_ns"] for o in s.pending)
        tss = [b[0] for b in self.tape.buf]
        i = bisect_left(tss, f)
        s.rng = None
        for b in self.tape.buf[i:]:
            if b[0] > until_ns or not s.pending or s.pos is not None: break
            self._work_pending(s, b[0], b[2])

    # ------------------------------------------------------------------ cycle
    def due(self, s, now):
        last = self.last_cycle.get(s.name, 0)
        a0, a1 = s.cfg.get("active", (0, 24 * 60))
        m = ny_mod(now)
        if s.pos is None and not s.pending and not (a0 <= m < a1): return False
        if s.cfg["family"] == "valentini":
            return (self.vbar_closed and now - last >= NS) or now - last >= CF.RECOMPUTE_SEC * NS
        return now // (60 * NS) != last // (60 * NS) or now - last >= CF.RECOMPUTE_SEC * NS

    def cycle(self, force=False):
        now = self.clock()
        names = [n for n, s in self.S.items() if force or self.due(s, now)]
        if not names: return
        vframe = None; bars = None
        if any(self.S[n].cfg["family"] == "valentini" for n in names): self.vbar_closed = False
        for n in names:
            s = self.S[n]; fam = s.cfg["family"]
            try:
                if fam == "valentini":
                    if vframe is None: vframe = self.tape.frame(CF.VALENTINI_SESSIONS)
                    view = ST.valentini_view(vframe, s.cfg["kw"])
                else:
                    if bars is None: bars = self.cache.bars(self.tape, CF.WARMUP_SESSIONS)
                    view = ST.pros_view(fam, s.cfg["kw"], *bars, partial_ok=(fam == "williams"))
            except Exception as e:                                       # never let one strategy kill the engine
                log.exception("view failed for %s", n); continue
            self.last_cycle[n] = now
            self.reconcile(s, view, now)

    def reconcile(self, s, v, now):
        s.last_view = v
        mo = v.get("open")
        if s.pos is None and mo and s.recent_close and abs(mo["entry_ns"] - s.recent_close[0]) <= 120 * NS and mo["d"] == s.recent_close[1]:
            s.handled.add(mo["entry_ns"])                            # the model's view of a trade the tick guard already closed
        if s.pos is None:
            if mo and mo["entry_ns"] not in s.handled and now - mo["entry_ns"] <= CF.ACT_WINDOW_SEC * NS:
                self._open(s, mo["d"], mo["fill"], mo["stop"], mo["tgt"], abs(mo["fill"] - mo["stop"]), mo["entry_ns"],
                           mo.get("flat_mod"), "model open (catch-up)")
            elif mo and mo["entry_ns"] not in s.handled:
                s.handled.add(mo["entry_ns"])                         # too old to chase: skip it, log once
                self._j(dict(strategy=s.name, action="skip", d=mo["d"], model_px=mo["fill"], why="stale model entry"))
            if s.pos is None:
                s.pending = [o for o in v.get("pending", [])]
                self._catch_up(s, now)
            return
        p = s.pos
        if mo is None:
            cut = v.get("cut_ns")
            if p["entry_id"] is None and (cut is None or p["opened_ns"] >= cut - NS):
                if cut is not None or p["opened_ns"] >= now - CF.RECOMPUTE_SEC * NS * 2:
                    return                                           # opened on live ticks the model hasn't processed yet
            last = None
            for c in reversed(v.get("closed", [])):                  # the model's trade that matches this position
                if (p["entry_id"] is not None and c["entry_ns"] == p["entry_id"]) or \
                   (p["entry_id"] is None and c["d"] == p["d"] and abs(c["entry_ns"] - p["opened_ns"]) <= 120 * NS):
                    last = c; break
            if last is not None: s.handled.add(last["entry_ns"])
            self._close(s, last["exit"] if last else self.tape.last_price(), f"model exit ({last['why'] if last else 'no matching model trade'})")
            return
        if p["entry_id"] is None:
            p["entry_id"] = mo["entry_ns"]; s.handled.add(mo["entry_ns"]); p["model_fill"] = mo["fill"]
        elif p["entry_id"] != mo["entry_ns"]:
            self._close(s, self.tape.last_price(), "model switched position")
            return self.reconcile(s, v, now)
        if mo.get("exit_now"):
            self._close(s, self.tape.last_price(), "model exit (management)"); return
        p["stop"], p["tgt"] = mo["stop"], mo["tgt"]
        want_legs = int(round(p["base_qty"] * sum(q for _, q in mo.get("legs", []))))
        if want_legs > p["legs_qty"]:                                 # G4 add-on legs
            add = want_legs - p["legs_qty"]
            px = self.broker.market(p["d"] * add, f"{s.name} add")
            p["broker_fill"] = (p["broker_fill"] * p["qty"] + px * add) / (p["qty"] + add)
            p["legs_qty"] = want_legs; p["qty"] = p["base_qty"] + want_legs
            self._j(dict(strategy=s.name, action="add", d=p["d"], qty=add, model_px=mo["legs"][-1][0], broker_px=px, why="G4 add"))
            self._safety()
