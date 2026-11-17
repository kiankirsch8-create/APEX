"""TRULLAS-1/2, CREAMER-1 (reversal + continuation, no gamma), WILLIAMS-1 (4 day-trading studies) on Databento NQ.

Rule sheets: research/existing/rulesheets/{TRULLAS-1_DIVERGENCE_FIB, CREAMER-1_AUCTION_RESOLUTION, WILLIAMS-1_DAYTRADE_BIAS}.md.
Defaults for the unstated parts were approved by Kian on 2026-11-16 (chat); every one is marked INTERPRETED below.

Execution model (shared, conservative, bar-level on 1-minute bars built from trades):
- Stop entry fills at max(level, bar open) + 1 tick; market entry at the next bar open + 1 tick.
- Stop fills at min(stop, bar open) - 1 tick; a target fills only if traded THROUGH by 1 tick.
- If stop and target are both touched in one bar -> stop. In the entry bar only the stop can be hit.
- Flat at the strategy's flat time (bar open - 1 tick). Costs $4.50 per NQ round turn (account view: MNQ $1.50).
Grade of every result: Partial/Proxy (see rule sheets).
"""
import os, sys
import numpy as np, pandas as pd

TICK, PV = 0.25, 20.0
NB = "scalping_data/newbars"
MONTHS = sorted(f[3:10] for f in os.listdir(NB) if f.startswith("1m_"))


def load(kind):
    b = pd.concat([pd.read_parquet(f"{NB}/{kind}_{m}.parquet") for m in MONTHS], ignore_index=True)
    b["mod"] = b.t.dt.hour * 60 + b.t.dt.minute
    return b


# ------------------------------------------------------------------------------------------------ execution
class Exec:
    def __init__(self, b1, cost=4.50, live=False):
        self.live = live
        self.O, self.H, self.L, self.C = b1.o.values, b1.h.values, b1.l.values, b1.c.values
        self.MOD, self.SES, self.T = b1["mod"].values, b1.session.values, b1.t.values
        self.cost = cost

    def trade(self, i0, d, entry, stop, tgt, flat_mod, kind="stop", valid=30, cancel=None, cut=None):
        """Walk 1m bars from i0. entry: level (stop order) or None (market at bar i0 open).
        cancel(i) -> True cancels an unfilled order; cut(i, k_entry) -> True exits at the bar close (management rule).
        Returns dict or None (never filled)."""
        O, H, L, C, MOD, SES = self.O, self.H, self.L, self.C, self.MOD, self.SES
        n = len(O)
        if i0 >= n:                                                # order created on the last closed bar
            if not self.live or kind != "stop" or MOD[n - 1] + 1 >= flat_mod: return None
            if (C[n - 1] >= entry) if d == 1 else (C[n - 1] <= entry): return None   # already passed = missed
            return dict(session=SES[n - 1], entry_t=None, exit_t=None, d=d, fill=np.nan, stop=stop, exit=np.nan, tgt=tgt,
                        entry=entry, dist=abs(entry - stop), pnl=np.nan, R=np.nan, why="pending", kx=n - 1,
                        valid_left=valid, flat_mod=flat_mod)
        ses = SES[i0]; fill = None; k = i0
        if kind == "stop" and i0 > 0 and ((C[i0 - 1] >= entry) if d == 1 else (C[i0 - 1] <= entry)):
            return None                                            # level already passed when the order could be placed = missed
        while k < n and SES[k] == ses and MOD[k] < flat_mod:
            if fill is None:
                if k - i0 >= valid or (cancel is not None and cancel(k)): return None
                if kind == "market":
                    fill = O[k] + d * TICK
                elif (H[k] >= entry) if d == 1 else (L[k] <= entry):
                    fill = (max(entry, O[k]) if d == 1 else min(entry, O[k])) + d * TICK
                else:
                    k += 1; continue
                ke = k
                if (L[k] <= stop) if d == 1 else (H[k] >= stop):
                    return self._out(i0, ke, k, d, fill, stop, (min(stop, O[k]) if d == 1 else max(stop, O[k])) - d * TICK, "stop")
                k += 1; continue
            if (L[k] <= stop) if d == 1 else (H[k] >= stop):
                return self._out(i0, ke, k, d, fill, stop, (min(stop, O[k]) if d == 1 else max(stop, O[k])) - d * TICK, "stop")
            if tgt is not None and ((H[k] >= tgt + TICK) if d == 1 else (L[k] <= tgt - TICK)):
                return self._out(i0, ke, k, d, fill, stop, (max(tgt, O[k]) if d == 1 else min(tgt, O[k])), "target")
            if cut is not None and cut(k, ke):
                return self._out(i0, ke, k, d, fill, stop, C[k] - d * TICK, "cut")
            k += 1
        if fill is None:
            if self.live and k >= n and kind == "stop" and (k - i0) < valid:   # live: order still resting at tape end
                return dict(session=SES[n - 1], entry_t=None, exit_t=None, d=d, fill=np.nan, stop=stop, exit=np.nan, tgt=tgt,
                            entry=entry, dist=abs(entry - stop), pnl=np.nan, R=np.nan, why="pending", kx=n - 1,
                            valid_left=valid - (k - i0), flat_mod=flat_mod)
            return None
        if k >= n and self.live:                                   # live engine: tape ended with the position open
            return dict(session=SES[ke], entry_t=self.T[ke], exit_t=None, d=d, fill=fill, stop=stop, exit=np.nan, tgt=tgt,
                        dist=abs(fill - stop), pnl=np.nan, R=np.nan, why="open", kx=n - 1, flat_mod=flat_mod)
        k = min(k, n - 1)
        px = (O[k] if (SES[k] == ses and k < n) else C[k - 1]) - d * TICK
        return self._out(i0, ke, k, d, fill, stop, px, "flat")

    def _out(self, i0, ke, kx, d, fill, stop, px, why):
        dist = abs(fill - stop)
        pnl = (px - fill) * d * PV - self.cost
        return dict(session=self.SES[ke], entry_t=self.T[ke], exit_t=self.T[kx], d=d, fill=fill, stop=stop, exit=px,
                    dist=dist, pnl=pnl, R=pnl / (dist * PV) if dist > 0 else np.nan, why=why, kx=kx)


# ------------------------------------------------------------------------------------------------ helpers
def macd(c, f=12, s=26, sig=9):
    ef = pd.Series(c).ewm(span=f, adjust=False).mean(); es = pd.Series(c).ewm(span=s, adjust=False).mean()
    m = ef - es
    return m.values, (m - m.ewm(span=sig, adjust=False).mean()).values


def rsi(c, n=14):
    dlt = np.diff(c, prepend=c[0]); up = pd.Series(np.clip(dlt, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
    dn = pd.Series(np.clip(-dlt, 0, None)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).values


def pivots(H, L, k=2):
    """5-bar pivots (k bars each side). Returns bool arrays ph, pl located AT the pivot bar (confirmed k bars later)."""
    n = len(H); ph = np.zeros(n, bool); pl = np.zeros(n, bool)
    for j in range(k, n - k):
        if H[j] == H[j - k:j + k + 1].max() and H[j] > H[j - k:j].max(): ph[j] = True
        if L[j] == L[j - k:j + k + 1].min() and L[j] < L[j - k:j].min(): pl[j] = True
    return ph, pl


def hourly_ema200(b1):
    """1-hour 200 EMA (on hourly closes, all sessions) mapped back to 1m bars, using only completed hours."""
    hr = b1.set_index("t").c.resample("1h").last().dropna()
    e = hr.ewm(span=200, adjust=False).mean().shift(1)            # completed hours only
    return e.reindex(b1.t.dt.floor("1h")).values


# ------------------------------------------------------------------------------------------------ TRULLAS-1
def trullas1(b1, X, tgt_fib=0.382, tf="1m", trend=True, kinds=("regular", "hidden"), w0=9 * 60, w1=10 * 60 + 30,
             flat=12 * 60, maxloss=3, daywinR=2.0, piv_k=2, lookback=60, entry_fib=0.236):
    """T1 MACD divergence mandatory; T2 +RSI, +RSI from OB/OS, +volume -> confluence 1-4 (T8); T5 fib from the newest extreme
    to the opposite extreme between the two swings; T6 entry stop 1 tick through ~23.6%, stop 1 tick beyond the extreme;
    T7 target 1 tick before 38.2% (variants 50 / 61.8); T10 with the 1h 200 EMA (INTERPRETED); window 09:00-10:30 NY
    (US open, he's done by ~16:00 CET); 3 losses or +2R ends the day (INTERPRETED from 2-3 bullets / ~$1,000 then stop)."""
    if tf == "5m":
        b = b1.set_index("t").groupby("session").resample("5min").agg(dict(o="first", h="max", l="min", c="last", v="sum")).dropna().reset_index()
        b["mod"] = b.t.dt.hour * 60 + b.t.dt.minute
        idx1 = np.searchsorted(b1.t.values, (b.t + pd.Timedelta("5min")).values)   # first 1m bar after the 5m close
    else:
        b = b1; idx1 = np.arange(1, len(b1) + 1)
    H, L, C, V, MOD, SES = b.h.values, b.l.values, b.c.values, b.v.values, b["mod"].values, b.session.values
    M, _ = macd(C); R = rsi(C)
    ema = hourly_ema200(b1)[np.minimum(idx1 - 1, len(b1) - 1)]
    ph, pl = pivots(H, L, piv_k)
    out = []; day = None; dayR = 0; losses = 0; busy_until = -1
    lastL = []; lastH = []          # recent confirmed pivots (index)
    for j in range(len(b)):
        if SES[j] != day: day, dayR, losses, lastL, lastH = SES[j], 0.0, 0, [], []
        c = j - piv_k                                             # pivot at c is confirmed at bar j
        if c < 0: continue
        for isLow, arr, piv in ((True, lastL, pl), (False, lastH, ph)):
            if not piv[c]: continue
            prev = [p for p in arr if c - p <= lookback]
            arr.append(c)
            if not prev: continue
            p = prev[-1]; d = 1 if isLow else -1
            if not (w0 <= MOD[j] < w1) or losses >= maxloss or dayR >= daywinR or idx1[j] <= busy_until: continue
            if trend and (np.isnan(ema[j]) or (C[j] - ema[j]) * d <= 0): continue
            x2, x1 = (L[c], L[p]) if isLow else (H[c], H[p])
            regular = (x2 < x1) if isLow else (x2 > x1)
            mdiv = (M[c] > M[p]) if regular == isLow else (M[c] < M[p])   # regular: price new extreme, MACD not; hidden: inverse
            kind = "regular" if regular else "hidden"
            if kind not in kinds: continue
            if regular:
                mdiv = (M[c] > M[p]) if isLow else (M[c] < M[p])
                rdiv = (R[c] > R[p]) if isLow else (R[c] < R[p])
            else:
                mdiv = (M[c] < M[p]) if isLow else (M[c] > M[p])
                rdiv = (R[c] < R[p]) if isLow else (R[c] > R[p])
            if not mdiv: continue                                  # T1: no MACD divergence, no trade
            conf = 1 + int(rdiv) + int(rdiv and ((R[p] < 30) if isLow else (R[p] > 70))) + int(V[c] < V[p])
            opp = H[p:c + 1].max() if isLow else L[p:c + 1].min()  # extreme between the two swings
            rng = abs(opp - x2)
            if rng < 8 * TICK: continue
            ent = x2 + d * entry_fib * rng; ent = (np.ceil(ent / TICK) * TICK + TICK) if d == 1 else (np.floor(ent / TICK) * TICK - TICK)
            stp = x2 - d * TICK
            tgt = x2 + d * tgt_fib * rng - d * TICK
            if (tgt - ent) * d <= 0: continue
            k0 = idx1[j]
            if k0 >= len(b1) and not X.live: continue
            r = X.trade(k0, d, ent, stp, tgt, flat, valid=30,
                        cancel=lambda k, d=d, x2=x2, tgt=tgt: (X.L[k] <= x2 - TICK) if d == 1 else (X.H[k] >= x2 + TICK))
            if r is None: continue
            r.update(model="TRULLAS-1", kind=kind, conf=conf); out.append(r); busy_until = r["kx"]
            dayR += r["R"]; losses += int(r["pnl"] < 0)
    return pd.DataFrame(out)


# ------------------------------------------------------------------------------------------------ TRULLAS-2
def trullas2(b1, X, tgt_fib=0.5, w0=9 * 60 + 30, w1=10 * 60 + 30, flat=12 * 60):
    """Opening sweep: after 09:30, a new low (5-bar pivot) below the previous opening pivot low on LOWER bar volume
    -> long on a stop 1 tick above the signal pivot bar's high (INTERPRETED trigger); stop 1 tick under the new low;
    target 50% (variant 66%) of the drop from the session high since 09:30 (INTERPRETED reference high). One trade/day."""
    H, L, V, MOD, SES = X.H, X.L, b1.v.values, X.MOD, X.SES
    ph, pl = pivots(H, L, 2)
    out = []; day = None; lows = []; done = False; hi = np.nan
    for j in range(len(b1)):
        if SES[j] != day: day, lows, done, hi = SES[j], [], False, np.nan
        if MOD[j] >= w0: hi = H[j] if np.isnan(hi) else max(hi, H[j])
        c = j - 2
        if done or c < 0 or not (w0 <= MOD[c]) or not (MOD[j] < w1) or not pl[c]: continue
        if lows and L[c] < L[lows[-1]] and V[c] < V[lows[-1]]:
            ref = H[np.flatnonzero((SES == day) & (MOD >= w0))[0]:c + 1].max()
            tgt = L[c] + tgt_fib * (ref - L[c]) - TICK
            ent = H[c] + TICK; stp = L[c] - TICK
            if (tgt - ent) > 2 * TICK:
                r = X.trade(j + 1, 1, ent, stp, tgt, flat, valid=15, cancel=lambda k, x=L[c]: X.L[k] < x)
                if r is not None:
                    r.update(model="TRULLAS-2", kind="sweep", conf=1); out.append(r); done = True
        lows.append(c)
    return pd.DataFrame(out)


# ------------------------------------------------------------------------------------------------ CREAMER-1
def creamer(b1, b5, prof5, X, mode="reversal", w0=9 * 60 + 30, w1=11 * 60, flat=15 * 60 + 55, big=30, effK=1.5,
            volPct=25, maxloss=2, cut_bars=6, piv_k=2, needVA=True, needPOC=True):
    """Environment (A1, INTERPRETED): 1h structure from 5-bar pivots on 1h bars - last two highs and lows both higher = bull,
    both lower = bear, else balance. Reversal (location B1-B4 + D1-D3): fixed-range profile of the last 5m impulse swing;
    fib zone 0.705-0.886 of that swing must lie outside the value area (discount for longs); a 5m candle in the zone with
    strong opposite delta (>= effK x avg |delta|), candle POC in the wick and the candle closing the other way (D1); entry
    on the next candle trading back through that candle's high/low after a pullback (D2, INTERPRETED); stop 2 ticks beyond
    the failed candle; target = the swing extreme (E2); cut at the close if price has not re-entered value within
    cut_bars x 5m (E1). Continuation (D5): with the environment, a 5m candle with a big single order >= `big` NQ in the body
    (300 MNQ = 30 NQ notional) and a close in the outer 25%; the next candle pulls back to the big order's price, then
    entry 1 tick through the pullback candle's extreme, stop 2 ticks beyond the pullback low/high, target 2R (his 1.5-2R).
    Participation (D7, INTERPRETED): skip 5m candles with volume below the volPct percentile of RTH 5m volume (prior 20 days).
    Hard stop 1.5h after the NY open (11:00), 2 losses end the day (E3). One position at a time."""
    H, L, O, C, V, D, POC = (b5[k].values for k in ("h", "l", "o", "c", "v", "delta", "poc"))
    BB, BBP, BS, BSP = b5.bigbuy.values, b5.bigbuy_px.values, b5.bigsell.values, b5.bigsell_px.values
    MOD, SES, T = b5["mod"].values, b5.session.values, b5.t.values
    # environment on 1h
    h1 = b1.set_index("t").resample("1h").agg(dict(h="max", l="min", c="last")).dropna()
    hph, hpl = pivots(h1.h.values, h1.l.values, 2)
    env = np.zeros(len(h1)); HH = []; LL = []
    for j in range(len(h1)):
        c = j - 2
        if c >= 0 and hph[c]: HH.append(h1.h.values[c])
        if c >= 0 and hpl[c]: LL.append(h1.l.values[c])
        if len(HH) >= 2 and len(LL) >= 2:
            env[j] = 1 if (HH[-1] > HH[-2] and LL[-1] > LL[-2]) else (-1 if (HH[-1] < HH[-2] and LL[-1] < LL[-2]) else 0)
    envS = pd.Series(env, index=h1.index).shift(1)                 # completed hours only
    ENV = envS.reindex(b5.t.dt.floor("1h")).fillna(0).values
    # participation threshold
    rth = (MOD >= 9 * 60 + 30) & (MOD < 16 * 60)
    dv = pd.DataFrame({"s": SES, "v": V})[rth].groupby("s").v.apply(lambda s: np.percentile(s, volPct))
    thr = dv.rolling(20, min_periods=5).median().shift(1)
    THR = pd.Series(SES).map(thr).values
    avgAD = pd.Series(np.abs(D)).rolling(30, min_periods=5).mean().shift(1).values
    ph, pl = pivots(H, L, piv_k)
    # profile rows per 5m bar
    pb = prof5.bar.values; pstart = np.searchsorted(pb, np.arange(len(b5) + 1))
    prow, pvol = prof5.row.values, prof5.v.values
    idx1 = np.searchsorted(X.T, T + np.timedelta64(5, "m"))        # first 1m bar after a 5m bar closes

    def va(a, z):
        rr = prow[pstart[a]:pstart[z + 1]]; vv = pvol[pstart[a]:pstart[z + 1]]
        if len(rr) == 0: return np.nan, np.nan
        u, inv = np.unique(rr, return_inverse=True); vol = np.bincount(inv, weights=vv)
        pi = int(np.argmax(vol)); lo = hi = pi; acc = vol[pi]; tot = vol.sum()
        while acc < 0.7 * tot and (lo > 0 or hi < len(u) - 1):
            up = vol[hi + 1] if hi < len(u) - 1 else -1; dn = vol[lo - 1] if lo > 0 else -1
            if up >= dn: hi += 1; acc += up
            else: lo -= 1; acc += dn
        return u[hi] + 1, u[lo]

    out = []; day = None; losses = 0; busy = -1; lastH = None; lastL = None
    for j in range(2, len(b5) - 1):
        if SES[j] != day: day, losses = SES[j], 0
        c = j - piv_k
        if ph[c]: lastH = c
        if pl[c]: lastL = c
        if not (w0 <= MOD[j] < w1) or losses >= maxloss or idx1[j] <= busy or idx1[j] >= len(X.O): continue
        if np.isnan(THR[j]) or V[j] < THR[j]: continue
        e = ENV[j]
        if mode == "reversal":
            for d in (1, -1):
                if lastH is None or lastL is None: continue
                # long: impulse up = swing low -> swing high (low earlier); price now back in the discount zone
                a, z = (lastL, lastH) if d == 1 else (lastH, lastL)
                if not a < z or z >= j: continue
                if e == -d: continue                               # not against the 1h structure
                lo, hi = L[lastL], H[lastH]; rg = hi - lo
                if rg < 20: continue
                if d == 1: zt, zb = hi - 0.705 * rg, hi - 0.886 * rg
                else: zb, zt = lo + 0.705 * rg, lo + 0.886 * rg
                vah, val = va(a, z)
                if np.isnan(vah): continue
                if needVA and ((zt >= val) if d == 1 else (zb <= vah)): continue          # B4: zone outside value
                inzone = (L[j] <= zt and L[j] >= zb - 0) if d == 1 else (H[j] >= zb and H[j] <= zt)
                if not inzone: continue
                strong = not np.isnan(avgAD[j]) and -D[j] * d > 0 and -D[j] * d >= effK * avgAD[j]
                pocwick = (not needPOC) or ((POC[j] < min(O[j], C[j])) if d == 1 else (POC[j] > max(O[j], C[j])))
                flips = (C[j] - O[j]) * d > 0
                if not (strong and pocwick and flips): continue
                # D2: next 5m candle pulls back then trades through this candle's extreme -> entry on 1m stop order
                ent = H[j] + TICK if d == 1 else L[j] - TICK
                stp = L[j] - 2 * TICK if d == 1 else H[j] + 2 * TICK
                tgt = hi if d == 1 else lo
                if (tgt - ent) * d <= (ent - stp) * d: continue      # at least 1:1 to the swing extreme
                vline = val if d == 1 else vah
                k0 = idx1[j]
                r = X.trade(k0, d, ent, stp, tgt, flat, valid=12,
                            cut=lambda k, ke, d=d, vl=vline: (k - ke) >= cut_bars * 5 and ((X.C[k] < vl) if d == 1 else (X.C[k] > vl)))
                if r is None: continue
                r.update(model="CREAMER-1", kind="reversal", conf=1); out.append(r); busy = r["kx"]; losses += int(r["pnl"] < 0)
                break
        else:
            d = int(e)
            if d == 0: continue
            bsz, bpx = (BB[j], BBP[j]) if d == 1 else (BS[j], BSP[j])
            if bsz < big or not (min(O[j], C[j]) <= bpx <= max(O[j], C[j])): continue
            rg = H[j] - L[j]
            if rg <= 0 or ((C[j] - L[j]) / rg < 0.75 if d == 1 else (H[j] - C[j]) / rg < 0.75): continue
            n = j + 1                                              # pullback candle
            if SES[n] != SES[j]: continue
            if not ((L[n] <= bpx) if d == 1 else (H[n] >= bpx)): continue
            if (C[n] < bpx - 4) if d == 1 else (C[n] > bpx + 4): continue      # held the big trade (room 4 pts, INTERPRETED)
            ent = H[n] + TICK if d == 1 else L[n] - TICK
            stp = min(L[n], bpx) - 2 * TICK if d == 1 else max(H[n], bpx) + 2 * TICK
            dist = abs(ent - stp)
            if dist > 40 or dist < 2: continue
            tgt = ent + d * 2 * dist
            k0 = idx1[n]
            if k0 >= len(X.O) and not X.live: continue
            r = X.trade(k0, d, ent, stp, tgt, flat, valid=10)
            if r is None: continue
            r.update(model="CREAMER-1", kind="continuation", conf=1); out.append(r); busy = r["kx"]; losses += int(r["pnl"] < 0)
    return pd.DataFrame(out)


# ------------------------------------------------------------------------------------------------ WILLIAMS-1
def williams(b1, X, study="closeopen", stopK=0.5, blastPct=20, bracketK=0.25):
    """NQ RTH (09:30-16:00). ATR10 = average RTH range of the prior 10 days. Stops are INTERPRETED (he uses money stops):
    stopK x ATR10 from the entry. All trades exit at the close (D3 'hold to the close').
    closeopen: D4 - after a DOWN close (RTH close < prior RTH close) buy the open; variant kinds logged by open vs prior close.
    dow:       D5 - buy the open Mon/Tue/Thu (S&P bias), exit close.
    blastoff:  D2 - prior day |O-C|/range in the lowest blastPct% of the last 20 days -> bracket the open by bracketK x ATR10,
               first break enters, stop at the opposite bracket, exit close (D9 distance INTERPRETED).
    expansion: D3 - at 12:00, if the day's range already >= 1.0 x ATR10 and price sits in the outer 25% of the range, enter in
               that direction, stop at mid-range, exit close (trigger INTERPRETED)."""
    rth = b1[(b1["mod"] >= 9 * 60 + 30) & (b1["mod"] < 16 * 60)]
    g = rth.groupby("session")
    dd = pd.DataFrame({"o": g.o.first(), "h": g.h.max(), "l": g.l.min(), "c": g.c.last(), "i0": g["o"].apply(lambda x: x.index[0])})
    dd["rng"] = dd.h - dd.l; dd["atr"] = dd.rng.rolling(10).mean().shift(1)
    dd["pc"] = dd.c.shift(1); dd["pc2"] = dd.c.shift(2); dd["po"] = dd.o.shift(1); dd["prng"] = dd.rng.shift(1)
    dd["oc"] = (dd.o - dd.c).abs() / dd.rng
    dd["ocpct"] = dd.oc.rolling(20).apply(lambda s: (s.iloc[-1] <= s).mean() * 100, raw=False).shift(1)
    dd["dow"] = pd.to_datetime(dd.index).dayofweek
    out = []
    for s, r in dd.iterrows():
        if np.isnan(r.atr) or np.isnan(r.pc2): continue
        i0 = int(r.i0); flat = 16 * 60 - 1
        if study == "closeopen":
            if not r.pc < r.pc2: continue                          # down close yesterday
            kind = "down close + higher open" if r.o > r.pc else "down close + lower open"
            t = X.trade(i0, 1, None, r.o - stopK * r.atr, None, flat, kind="market")
        elif study == "closeopen_dh":                              # D4 best case only: down close + HIGHER open
            if not (r.pc < r.pc2 and r.o > r.pc): continue
            kind = "down close + higher open"
            t = X.trade(i0, 1, None, r.o - stopK * r.atr, None, flat, kind="market")
        elif study == "closeopen_up":
            if not r.pc > r.pc2: continue
            kind = "up close + higher open" if r.o > r.pc else "up close + lower open"
            t = X.trade(i0, 1, None, r.o - stopK * r.atr, None, flat, kind="market")
        elif study in ("dow", "everyday"):
            if study == "dow" and r.dow not in (0, 1, 3): continue
            kind = ["Mon", "Tue", "Wed", "Thu", "Fri"][int(r.dow)]
            t = X.trade(i0, 1, None, r.o - stopK * r.atr, None, flat, kind="market")
        elif study == "blastoff":
            if not r.ocpct <= blastPct: continue
            up, dn = r.o + bracketK * r.atr, r.o - bracketK * r.atr
            up, dn = np.ceil(up / TICK) * TICK, np.floor(dn / TICK) * TICK   # resting stop orders sit on the tick grid
            k = i0; t = None
            while k < len(X.O) and X.SES[k] == s and X.MOD[k] < flat:
                if X.H[k] >= up: t = X.trade(k, 1, up, dn, None, flat); kind = "blastoff long"; break
                if X.L[k] <= dn: t = X.trade(k, -1, dn, up, None, flat); kind = "blastoff short"; break
                k += 1
            if t is None and X.live and k >= len(X.O):
                for dd_, lv, st in ((1, up, dn), (-1, dn, up)):
                    out.append(dict(session=s, entry_t=None, exit_t=None, d=dd_, fill=np.nan, stop=st, exit=np.nan, tgt=None,
                                    entry=lv, dist=abs(lv - st), pnl=np.nan, R=np.nan, why="pending", kx=len(X.O) - 1,
                                    valid_left=10**6, flat_mod=flat, oco="blastoff", model="WILLIAMS-1", kind="blastoff bracket", conf=1))
                continue
        elif study == "expansion":
            k = np.flatnonzero((X.SES == s) & (X.MOD == 12 * 60))
            if len(k) == 0: continue
            k = int(k[0]); hi, lo = X.H[i0:k].max(), X.L[i0:k].min(); rg = hi - lo
            if rg < r.atr: continue
            px = X.O[k]
            if px >= hi - 0.25 * rg: dd_, kind = 1, "expansion long"
            elif px <= lo + 0.25 * rg: dd_, kind = -1, "expansion short"
            else: continue
            t = X.trade(k, dd_, None, (lo + hi) / 2, None, flat, kind="market")
        else:
            raise ValueError(study)
        if t is None: continue
        t.update(model="WILLIAMS-1", kind=kind, conf=1); out.append(t)
    return pd.DataFrame(out)


# ------------------------------------------------------------------------------------------------ account + stats
def account(t, risk, eq0=100_000.0, mnq_cost=1.50, conf=False):
    """Sized to `risk` in MNQ units ($2/pt), traded as NQ ($4.50 RT) + MNQ remainder ($1.50 RT); of equity (confluence variant: risk x conf/4); daily loss cap max(2%, 3 x risk)."""
    eq = eq0; peak = eq0; mdd = 0.0; day = None; dstart = eq0; cap = max(0.02, 3 * risk); months = {}
    for r in t.sort_values("entry_t").itertuples():
        if r.session != day: day, dstart = r.session, eq
        if eq - dstart <= -cap * dstart: continue
        rk = risk * (r.conf / 4 if conf else 1)
        q = int(rk * eq // (r.dist * 2.0))                         # in MNQ units; 10 MNQ are traded as 1 NQ
        if q < 1: continue
        pnl = q * (r.exit - r.fill) * r.d * 2.0 - (q // 10) * 4.50 - (q % 10) * mnq_cost
        eq += pnl; months[r.session[:7]] = months.get(r.session[:7], 0) + pnl
        peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    up = sum(1 for v in months.values() if v > 0)
    return (eq / eq0 - 1) * 100, mdd * 100, f"{up}/{len(months)}"


PER = {"Jun-Dec 25": ("2025-06", "2025-12"), "Jan-Mar 26": ("2026-01", "2026-03"), "Apr-Jun 26": ("2026-04", "2026-06")}
RISKS = (0.0025, 0.005, 0.01, 0.015, 0.02)


def summarize(name, t, conf=False):
    r = dict(strategy=name, trades=len(t))
    if t.empty: return r
    w = t[t.pnl > 0]; gl = -t[t.pnl <= 0].pnl.sum()
    r.update(win=round(len(w) / len(t) * 100), avgR=round(t.R.mean(), 3),
             t=round(t.R.mean() / (t.R.std(ddof=1) / np.sqrt(len(t))), 2) if len(t) > 2 else np.nan,
             PF=round(w.pnl.sum() / gl, 2) if gl else np.inf, net_NQ=round(t.pnl.sum()))
    for pn, (a, b) in PER.items():
        tp = t[(t.session.str[:7] >= a) & (t.session.str[:7] <= b)]
        r[f"{pn} n/R"] = f"{len(tp)}/{tp.R.mean():+.2f}" if len(tp) else "0"
    for rk in RISKS:
        ret, mdd, mo = account(t, rk, conf=conf)
        r[f"{rk*100:g}%"] = f"{ret:+.1f}% (DD {mdd:.1f}%)"
    r["months up (1%)"] = account(t, 0.01, conf=conf)[2]
    return r
