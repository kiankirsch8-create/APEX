"""Parity test: stream historical sessions through the LIVE engine (SimBroker) and compare its trades with the backtest
code run afterwards on exactly the same data. Engine mechanics are correct when both lists match (same entries,
directions, exits within slippage).

usage: python -m scalping.live.replay_test 2026-05-12 [2026-05-13 ...]
"""
import os, sys, time, logging
import numpy as np, pandas as pd
from . import config as CF
from .tape import Tape
from .engine import Engine
from .broker import SimBroker
from .feed import ReplayFeed
from . import strategies as ST

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def run(days, store=None):
    store = store or f"scalping_data/live_replay_{os.environ.get('REPLAY_ONLY', 'all')}"
    os.makedirs(store, exist_ok=True)
    for f in os.listdir(store) if os.path.exists(store) else []:
        pass
    months = sorted({d[:7] for d in days})
    allm = sorted(f[:7] for f in os.listdir("scalping_data/nq_trades") if f.endswith(".parquet"))
    prev = allm[allm.index(months[0]) - 1]
    hist = pd.concat([pd.read_parquet(f"scalping_data/nq_trades/{m}.parquet",
                                      columns=["ts", "seq", "price", "size", "side", "session", "degraded"]) for m in [prev] + months])
    hist = hist[~hist.degraded]; hist["session"] = hist.session.astype(str)
    sess = sorted(hist.session.unique())
    i0 = sess.index(days[0])
    warm = sess[max(0, i0 - CF.WARMUP_SESSIONS):i0]
    tape = Tape(store, keep=CF.WARMUP_SESSIONS + len(days) + 2)
    tape.dir = os.path.join(store, "tape_tmp"); os.makedirs(tape.dir, exist_ok=True)
    tape.add_history(hist[hist.session.isin(warm)][["ts", "seq", "price", "size", "side"]])
    clock = {"t": int(tape.last_ts)}
    br = SimBroker(tape)
    for f in os.listdir(os.path.join(store, "journal")) if os.path.exists(os.path.join(store, "journal")) else []:
        os.remove(os.path.join(store, "journal", f))
    only = os.environ.get("REPLAY_ONLY")
    strats = {k: v for k, v in CF.STRATEGIES.items() if not only or (v["family"] == "valentini") == (only == "valentini")}
    names = os.environ.get("REPLAY_NAMES")                      # optional substring filter, e.g. "defaults"
    if names: strats = {k: v for k, v in strats.items() if any(x in k for x in names.split(","))}
    if os.environ.get("REPLAY_RECOMPUTE"): CF.RECOMPUTE_SEC = int(os.environ["REPLAY_RECOMPUTE"])
    until = os.environ.get("REPLAY_UNTIL")                       # NY time HH:MM on the last day, to stop early
    eng = Engine(tape, br, store=store, strategies=strats, clock=lambda: clock["t"])
    t0 = time.time(); n = 0; last_m = None
    test = hist[hist.session.isin(days)].sort_values(["ts", "seq"], kind="stable")
    if until:
        test = test[test.ts < pd.Timestamp(f"{days[-1]} {until}").tz_localize("America/New_York").value]
    debug = os.environ.get("REPLAY_DEBUG")                       # print each strategy view between two NY times "HH:MM-HH:MM"
    for r in test[["ts", "seq", "price", "size", "side"]].itertuples(index=False):
        clock["t"] = int(r.ts)
        tape.add(int(r.ts), int(r.seq), float(r.price), int(r.size), int(r.side))
        eng.on_tick(int(r.ts), float(r.price))
        n += 1
        m = int(r.ts) // 60_000_000_000
        if n % 50 == 0 or m != last_m or eng.vbar_closed:
            before = dict(eng.last_cycle)
            eng.cycle()
            if debug:
                a, b = debug.split("-"); tny = pd.Timestamp(int(r.ts), tz="UTC").tz_convert("America/New_York").strftime("%H:%M:%S")
                if a <= tny[:5] < b:
                    for k, s_ in eng.S.items():
                        if eng.last_cycle.get(k) != before.get(k):
                            v = s_.last_view or {}
                            print(tny, k, "open", v.get("open"), "pend", v.get("pending"), "pos", s_.pos, "spend", s_.pending, flush=True)
        last_m = m
    tape.roll("END")
    if until: print("(stopped early at", until, ")")
    print(f"replayed {n} trades in {time.time()-t0:.0f}s", flush=True)
    J = pd.concat([pd.read_csv(os.path.join(store, "journal", f)) for f in sorted(os.listdir(os.path.join(store, "journal")))],
                  ignore_index=True) if os.listdir(os.path.join(store, "journal")) else pd.DataFrame()
    # backtest on the same data (warm-up + test days), keep the test days
    d = tape.frame(CF.WARMUP_SESSIONS + len(days))
    expected = []
    b1, b5, prof = ST.BarCache().bars(tape, CF.WARMUP_SESSIONS + len(days))
    for name, cfg in strats.items():
        if cfg["family"] == "valentini":
            dv = d[d.session.astype(str).isin(sorted(d.session.astype(str).unique())[-(CF.VALENTINI_SESSIONS + len(days) - 1):])].reset_index(drop=True)
            dv["session"] = dv.session.astype(str).astype("category")
            import valentini2 as V
            t = V.run(dv, *V.build(dv, bars_kind=cfg["kw"].get("barKind", "1m")), **cfg["kw"])
            t = t[(t.leg == 0) & t.session.isin(days)] if not t.empty else t
            for r in t.itertuples():
                expected.append(dict(strategy=name, entry=pd.Timestamp(int(r.entry_ts), tz="UTC").tz_convert("America/New_York").strftime("%H:%M:%S"),
                                     d=r.d, fill=r.fill, exit=r.exit, why=r.why))
        else:
            import pros as P
            X = P.Exec(b1)
            fn = {"trullas1": P.trullas1, "creamer": P.creamer, "williams": P.williams}[cfg["family"]]
            t = fn(b1, b5, prof, X, **cfg["kw"]) if cfg["family"] == "creamer" else fn(b1, X, **cfg["kw"])
            t = t[t.session.isin(days)] if not t.empty else t
            for r in t.itertuples():
                expected.append(dict(strategy=name, entry=pd.Timestamp(r.entry_t).strftime("%H:%M:%S"), d=r.d, fill=r.fill, exit=r.exit, why=r.why))
    E = pd.DataFrame(expected)
    pd.set_option("display.width", 220); pd.set_option("display.max_columns", 20)
    print("\n=== BACKTEST on the same data ===")
    print(E.to_string(index=False) if not E.empty else "(no trades)")
    print("\n=== LIVE ENGINE journal ===")
    if not J.empty:
        J["t_ny"] = pd.to_datetime(J.time_utc).dt.tz_convert("America/New_York").dt.strftime("%m-%d %H:%M:%S")
        print(J[["t_ny", "strategy", "action", "d", "qty", "model_px", "broker_px", "slip_pts", "why"]].to_string(index=False))
    else:
        print("(no actions)")
    return E, J


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orderflow"))
    run(sys.argv[1:])
