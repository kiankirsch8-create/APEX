"""VALENTINI-1 (trend model, NY) on Databento NQ trades: a port of research/existing/pine/VALENTINI-suite.pine v5, module V1.

Same rules as Pine v5 (rule sheet VALENTINI-1_TREND.md + audit A1-A4, B1-B3, B5), with these documented changes:
- E1/E2 trigger: the footprint delta proxy is replaced by REAL single aggressor orders >= BIG lots (same exchange
  timestamp + same aggressor side summed = one order). Aggression = big order in trade direction inside the zone with
  follow-through (bar closes in the outer ftPct in direction, B2). Absorption (B1) = big order AGAINST the direction
  inside the zone, bar holds (close in outer absHold, no break of the zone).
- B4 strongest-delta location and the CVD early breakeven: OFF (B4 went 0/35, CVD-BE contradicts his words).
- Breakeven (X5, his words 2:54:21 / 3:12:42): only after a full-body 1m candle CLOSES beyond the leg extreme at entry.
- Entry timing (E4, INTERPRETED), two variants:
    A "close": signal at the close of the bar with the big order, fill at the first trade of the next bar + 1 tick.
    B "fast" : aggression only; fill at the first trade >= LAT ms after the big order + 1 tick (no bar confirmation).
- Exits run on every trade: stop fills at the triggering trade - 1 tick; target fills only if traded THROUGH by 1 tick.
- 1 NQ contract per trade (sizing to 0.25% gives < 1 contract on small accounts; minOne). Costs $4.50 round turn.
Fidelity grade: Proxy (noise filter, active management, day-type sizing not in yet).
"""
import sys, glob, json
import numpy as np, pandas as pd

TICK, PV, RS = 0.25, 20.0, 1.0           # NQ tick, $ per point, profile row = 4 ticks
P = dict(BIG=30, pvLen=5, legLookback=30, edgePct=0.2, maxLegBars=240, zoneTol=4 * TICK, ftPct=0.6, absHold=0.5,
         useAbs=True, useCvdBreak=True, useDevVA=True, stopOff=2 * TICK, minStop=40 * TICK, maxStop=40.0,
         minRR=2.5, fallbackRR=5.0, vaPct=70.0, maxLosses=3, houseMoney=True, useBE=True, fullBody=0.5,
         entry="close", LAT_ms=250, cost=4.50, slip=TICK)
NY0, NY1, FLAT = 9 * 60 + 45, 13 * 60 + 30, 15 * 60 + 55        # entry window 09:45-13:30, flatten 15:55 ET


def va(prof):
    """Pine f_va: POC, VAH, VAL from {rowkey: volume}."""
    if not prof:
        return np.nan, np.nan, np.nan
    keys = np.array(sorted(prof)); vols = np.array([prof[k] for k in keys]); tot = vols.sum()
    pi = int(np.argmax(vols)); lo = hi = pi; acc = vols[pi]; n = len(keys)
    while acc < tot * P["vaPct"] / 100 and (lo > 0 or hi < n - 1):
        vu = vols[hi + 1] if hi < n - 1 else -1.0
        vd = vols[lo - 1] if lo > 0 else -1.0
        if vu >= vd: hi += 1; acc += vu
        else: lo -= 1; acc += vd
    return (keys[pi] + 0.5) * RS, (keys[hi] + 1) * RS, keys[lo] * RS


def load(months):
    """Lean numpy load (pandas groupby on 20M rows runs out of memory)."""
    parts = []
    for m in months:
        x = pd.read_parquet(f"scalping_data/nq_trades/{m}.parquet", columns=["ts", "seq", "price", "size", "side", "session", "degraded"])
        x = x[~x.degraded]
        parts.append(pd.DataFrame({"ts": x.ts.values, "seq": x.seq.values, "price": x.price.values,
                                   "size": x["size"].values, "side": x.side.values, "session": x.session.astype(str).values}))
    d = pd.concat(parts, ignore_index=True); del parts
    d = d.sort_values(["ts", "seq"], kind="stable").reset_index(drop=True)
    return d


def build(d):
    """1m bars (continuous across days), row profile per bar, big orders per bar."""
    ts = d.ts.values; px = d.price.values; sz = d["size"].values.astype(np.int64); sd = d.side.values.astype(np.int64)
    minute = ts // 60_000_000_000
    starts = np.flatnonzero(np.r_[True, minute[1:] != minute[:-1]])
    tstart = np.r_[starts, len(ts)]
    bar = np.repeat(np.arange(len(starts)), np.diff(tstart))
    mins = pd.to_datetime(minute[starts] * 60_000_000_000, utc=True).tz_convert("America/New_York")
    bars = pd.DataFrame({"minute": mins.tz_localize(None), "o": px[starts], "c": px[tstart[1:] - 1],
                         "h": np.maximum.reduceat(px, starts), "l": np.minimum.reduceat(px, starts),
                         "v": np.add.reduceat(sz, starts), "delta": np.add.reduceat(sz * sd, starts),
                         "session": d.session.values[starts], "mod": mins.hour * 60 + mins.minute})
    key = np.floor(px / RS).astype(np.int64)
    comb = bar * 1_000_000 + (key - key.min())
    u, inv = np.unique(comb, return_inverse=True)
    rows = pd.DataFrame({"bar": u // 1_000_000, "key": u % 1_000_000 + key.min(),
                         "v": np.bincount(inv, weights=sz), "dl": np.bincount(inv, weights=sz * sd)})
    rstart = np.searchsorted(rows.bar.values, np.arange(len(bars) + 1))
    ok = np.r_[True, (ts[1:] != ts[:-1]) | (sd[1:] != sd[:-1])]          # same timestamp + same side = one order
    gs = np.flatnonzero(ok); ge = np.r_[gs[1:], len(ts)]
    osz = np.add.reduceat(sz, gs)
    m = osz >= P["BIG"]
    big = pd.DataFrame({"ts": ts[gs[m]], "side": sd[gs[m]], "size": osz[m], "px": px[ge[m] - 1], "bar": bar[gs[m]]})
    return bars, rows, rstart, big, tstart


def run(d, bars, rows, rstart, big, tstart, **over):
    p = dict(P, **over)
    tsA, pxA = d.ts.values, d.price.values
    H, L, C, O = bars.h.values, bars.l.values, bars.c.values, bars.o.values
    DEL, MOD, SES = bars.delta.values, bars["mod"].values, bars.session.astype(str).values
    rk, rv, rd = rows.key.values, rows.v.values, rows.dl.values
    bigbar = {}
    for r in big.itertuples():
        bigbar.setdefault(r.bar, []).append(r)
    n, pv = len(bars), p["pvLen"]
    trades = []
    # day state
    pPOC = pVAH = pVAL = pHigh = pLow = np.nan; olderPOC = []; dayProf = {}; onProf = {}
    dHi = dLo = onHigh = onLow = onPOC = np.nan; dayBias = 0; cvd = 0.0; cvdHist = np.zeros(n); dayStart = 0
    dayLosses = 0; lastWinDir = 0; dayPnl = 0.0; firstUsedL = firstUsedS = False; dayFull = False
    lastPL = lastPH = np.nan; lastPLbar = lastPHbar = -1
    v1dir = 0; v1Start = v1Ext = v1Lvn = np.nan; v1StartBar = -1; usedPL = usedPH = -1
    pos = None   # dict when in a trade
    pending = None
    canTrade = True
    lastExit = 0
    MIN = bars.minute.values

    def legscan(b0, b1, lo, hi, dd):
        k0, k1 = int(np.floor(lo / RS)), int(np.floor(hi / RS)); span = k1 - k0 + 1
        if span < 5: return np.nan
        s, e = rstart[max(b0, 0)], rstart[b1 + 1]
        kk, vv = rk[s:e], rv[s:e]; m = (kk >= k0) & (kk <= k1)
        vols = np.bincount(kk[m] - k0, weights=vv[m], minlength=span)
        a = int(np.floor(span * p["edgePct"])); z = span - 1 - a
        if z <= a: return np.nan
        return (k0 + a + int(np.argmin(vols[a:z + 1]))) * RS

    def target(dd, entry, dist):
        cands = [pHigh if dd == 1 else pLow, pPOC, onPOC] + olderPOC
        best = np.nan
        for c in cands:
            if np.isnan(c): continue
            if (c > entry + TICK if dd == 1 else c < entry - TICK) and (np.isnan(best) or abs(c - entry) < abs(best - entry)):
                best = c
        if np.isnan(best): return entry + dd * p["fallbackRR"] * dist
        return best if abs(best - entry) / dist >= p["minRR"] else np.nan

    def stopfor(dd, level, entry):
        s = level - p["stopOff"] if dd == 1 else level + p["stopOff"]
        s = min(s, entry - p["minStop"]) if dd == 1 else max(s, entry + p["minStop"])
        return np.nan if abs(entry - s) > p["maxStop"] else s

    def close_pos(i, px, why, tsx):
        nonlocal pos, dayLosses, lastWinDir, dayPnl, lastExit
        lastExit = tsx
        pnl = (px - pos["fill"]) * pos["d"] * PV - p["cost"]
        dayPnl += pnl
        if pnl < 0: dayLosses += 1
        else: lastWinDir = pos["d"]
        trades.append(dict(session=SES[i], entry_ts=pos["ts"], exit_ts=tsx, d=pos["d"], how=pos["how"], fill=pos["fill"],
                           stop0=pos["stop0"], tgt=pos["tgt"], exit=px, why=why, R=(px - pos["fill"]) * pos["d"] / pos["dist"],
                           pnl=pnl, be=pos["be"]))
        pos = None

    def open_pos(i, k, tsx, fill, dd, dist, how, ext, tsig=np.nan):
        nonlocal pos
        stp = fill - dd * dist                      # re-anchor to the fill (Pine posInit)
        t = target(dd, fill, dist)
        if np.isnan(t): t = tsig                    # Pine: tgtPx := na(t0) ? tgtPx : t0
        if np.isnan(t):
            return False
        pos = dict(d=dd, fill=fill, ts=tsx, dist=dist, stp=stp, stop0=stp, tgt=t, how=how, ext=ext, be=False, k=k)
        return True

    def walk(i, k0):
        """Exit checks on trades k0.. of bar i. Returns nothing; closes pos if hit."""
        if pos is None: return
        for k in range(k0, tstart[i + 1]):
            px = pxA[k]
            if pos["d"] == 1:
                if px <= pos["stp"]: close_pos(i, px - p["slip"], "stop" if not pos["be"] else "be", tsA[k]); return
                if px >= pos["tgt"] + TICK: close_pos(i, pos["tgt"], "target", tsA[k]); return
            else:
                if px >= pos["stp"]: close_pos(i, px + p["slip"], "stop" if not pos["be"] else "be", tsA[k]); return
                if px <= pos["tgt"] - TICK: close_pos(i, pos["tgt"], "target", tsA[k]); return

    for i in range(n):
        newDay = i == 0 or SES[i] != SES[i - 1]
        if newDay:
            if dayFull and dayProf:
                pp, ph_, pl_ = va(dayProf)
                if not np.isnan(pPOC): olderPOC = ([pPOC] + olderPOC)[:10]
                pPOC, pVAH, pVAL, pHigh, pLow = pp, ph_, pl_, dHi, dLo
            dayFull = True; dayProf = {}; onProf = {}; dHi = dLo = onHigh = onLow = onPOC = np.nan
            dayBias = 0; cvd = 0.0; dayStart = i; dayLosses = 0; lastWinDir = 0; dayPnl = 0.0
            firstUsedL = firstUsedS = False
        mod = MOD[i]
        inON = mod >= 18 * 60 or mod < 9 * 60 + 30
        inNY = NY0 <= mod < NY1
        afterOpen = 9 * 60 + 30 <= mod < 17 * 60

        # ---- intrabar
        k0 = tstart[i]
        if pos is not None and mod >= FLAT and mod < 17 * 60:          # T4 flatten 15:55
            close_pos(i, pxA[k0] - pos["d"] * p["slip"], "eod", tsA[k0])
        if pending is not None and pos is None and tstart[i + 1] > k0:   # variant A fill at next bar's first trade
            dd, dist, how, ext, tsig = pending
            if open_pos(i, k0, tsA[k0], pxA[k0] + dd * p["slip"], dd, dist, how, ext, tsig):
                walk(i, k0 + 1)
        elif pos is not None:
            walk(i, k0)
        pending = None
        # variant B: fast entry at a big order inside the zone (zone state from the previous bar close)
        if p["entry"] == "fast" and pos is None and v1dir != 0 and inNY and canTrade and not np.isnan(v1Lvn) and i in bigbar:
            zlo, zhi = v1Lvn - p["zoneTol"], v1Lvn + RS + p["zoneTol"]
            for b in bigbar[i]:
                if b.ts <= lastExit or b.side != v1dir or not (zlo <= b.px <= zhi):
                    continue
                k = int(np.searchsorted(tsA, b.ts + int(p["LAT_ms"] * 1e6), "left"))
                if k >= tstart[i + 1]: break
                fill = pxA[k] + v1dir * p["slip"]
                st = stopfor(v1dir, b.px, fill)
                if np.isnan(st): break
                dist = abs(fill - st)
                if p["houseMoney"] and lastWinDir == v1dir and dist * PV > dayPnl: break
                if open_pos(i, k, tsA[k], fill, v1dir, dist, "fast agg@LVN", v1Ext):
                    v1dir = 0; walk(i, k + 1)
                break

        # ---- bar close: day state, profile, bias
        dHi = H[i] if np.isnan(dHi) else max(dHi, H[i]); dLo = L[i] if np.isnan(dLo) else min(dLo, L[i])
        s, e = rstart[i], rstart[i + 1]
        for kk, vv in zip(rk[s:e], rv[s:e]):
            dayProf[kk] = dayProf.get(kk, 0) + vv
            if inON: onProf[kk] = onProf.get(kk, 0) + vv
        if inON:
            onHigh = H[i] if np.isnan(onHigh) else max(onHigh, H[i]); onLow = L[i] if np.isnan(onLow) else min(onLow, L[i])
        if not inON and i > 0 and (MOD[i - 1] >= 18 * 60 or MOD[i - 1] < 9 * 60 + 30) and onProf and not newDay:
            onPOC = va(onProf)[0]
        cvd += DEL[i]; cvdHist[i] = cvd
        if afterOpen and not inON and not np.isnan(onHigh):
            if C[i] > onHigh: dayBias = 1
            elif C[i] < onLow: dayBias = -1
        canTrade = dayLosses < p["maxLosses"]
        # pivots (confirmed pv bars later, like ta.pivotlow/high)
        c = i - pv
        if c - pv >= 0:
            if L[c] < L[c - pv:c].min() and L[c] <= L[c + 1:i + 1].min(): lastPL, lastPLbar = L[c], c
            if H[c] > H[c - pv:c].max() and H[c] >= H[c + 1:i + 1].max(): lastPH, lastPHbar = H[c], c
        lb0 = max(0, i - p["legLookback"] + 1)
        lowestL = L[lb0:i + 1].min(); lowestBar = lb0 + int(np.argmin(L[lb0:i + 1]))
        highestH = H[lb0:i + 1].max(); highestBar = lb0 + int(np.argmax(H[lb0:i + 1]))
        cvdPH = cvdHist[lastPHbar] if lastPHbar >= dayStart and 0 < i - lastPHbar < 450 else np.nan
        cvdPL = cvdHist[lastPLbar] if lastPLbar >= dayStart and 0 < i - lastPLbar < 450 else np.nan
        cvdOkL = not p["useCvdBreak"] or np.isnan(cvdPH) or cvd > cvdPH
        cvdOkS = not p["useCvdBreak"] or np.isnan(cvdPL) or cvd < cvdPL

        # ---- breakeven at bar close (his words: full-body candle accepted beyond the leg high)
        if pos is not None and p["useBE"] and not pos["be"]:
            rng = H[i] - L[i]; body = abs(C[i] - O[i]) >= p["fullBody"] * rng if rng > 0 else False
            dd = pos["d"]
            if body and (dd == 1 and C[i] > O[i] and C[i] > pos["ext"] and C[i] > pos["fill"] + TICK
                         or dd == -1 and C[i] < O[i] and C[i] < pos["ext"] and C[i] < pos["fill"] - TICK):
                pos["stp"] = max(pos["stp"], pos["fill"] + TICK) if dd == 1 else min(pos["stp"], pos["fill"] - TICK)
                pos["be"] = True

        # session end without a 15:55 bar (holiday early close): flat at this bar's last trade
        if pos is not None and (i + 1 == n or SES[i + 1] != SES[i] or (MIN[i + 1] - MIN[i]) > np.timedelta64(60, "m")):
            close_pos(i, C[i] - pos["d"] * p["slip"], "eod", tsA[tstart[i + 1] - 1])
        # ---- V1 leg state (Pine lines 448-501)
        v1New = False
        devVAH = devVAL = np.nan
        if p["useDevVA"] and inNY and v1dir == 0 and dayBias != 0 and dayProf:
            _, devVAH, devVAL = va(dayProf)
        if v1dir == 0 and inNY and canTrade:
            outL = not p["useDevVA"] or (not np.isnan(devVAH) and C[i] > devVAH)
            outS = not p["useDevVA"] or (not np.isnan(devVAL) and C[i] < devVAL)
            if dayBias == 1 and outL and cvdOkL:
                if lastPLbar >= 0 and lastPLbar != usedPL and i - lastPLbar <= p["maxLegBars"] and lastPL < C[i]:
                    v1dir, v1Start, v1StartBar, usedPL = 1, lastPL, lastPLbar, lastPLbar
                elif not firstUsedL:
                    v1dir, v1Start, v1StartBar, firstUsedL = 1, lowestL, lowestBar, True
                if v1dir == 1: v1Ext = H[i]; v1New = True
            elif dayBias == -1 and outS and cvdOkS:
                if lastPHbar >= 0 and lastPHbar != usedPH and i - lastPHbar <= p["maxLegBars"] and lastPH > C[i]:
                    v1dir, v1Start, v1StartBar, usedPH = -1, lastPH, lastPHbar, lastPHbar
                elif not firstUsedS:
                    v1dir, v1Start, v1StartBar, firstUsedS = -1, highestH, highestBar, True
                if v1dir == -1: v1Ext = L[i]; v1New = True
        elif v1dir == 1:
            if H[i] > v1Ext: v1Ext = H[i]; v1New = True
            if L[i] < v1Start or i - v1StartBar > p["maxLegBars"]: v1dir = 0
        elif v1dir == -1:
            if L[i] < v1Ext: v1Ext = L[i]; v1New = True
            if H[i] > v1Start or i - v1StartBar > p["maxLegBars"]: v1dir = 0
        if not inNY or (v1dir != 0 and dayBias != v1dir):
            v1dir = 0
        if v1New and v1dir != 0:
            v1Lvn = legscan(v1StartBar, i, v1Start if v1dir == 1 else v1Ext, v1Ext if v1dir == 1 else v1Start, v1dir)

        # ---- variant A signal at bar close (Pine lines 592-631, trigger = real big orders)
        if p["entry"] == "close" and pos is None and v1dir != 0 and not v1New and inNY and canTrade and not np.isnan(v1Lvn):
            zlo, zhi = v1Lvn - p["zoneTol"], v1Lvn + RS + p["zoneTol"]
            rng = H[i] - L[i]; edge = np.nan; how = ""
            if rng > 0 and L[i] <= zhi and H[i] >= zlo:
                bb = [b for b in bigbar.get(i, []) if zlo <= b.px <= zhi]
                ft = (C[i] > O[i] and C[i] - L[i] >= p["ftPct"] * rng) if v1dir == 1 else (C[i] < O[i] and H[i] - C[i] >= p["ftPct"] * rng)
                agg = [b for b in bb if b.side == v1dir]
                if agg and ft:
                    edge = min(b.px for b in agg) if v1dir == 1 else max(b.px for b in agg); how = "agg@LVN"
                elif p["useAbs"]:
                    opp = [b for b in bb if b.side == -v1dir]
                    held = (C[i] - L[i] >= p["absHold"] * rng and L[i] >= zlo) if v1dir == 1 else (H[i] - C[i] >= p["absHold"] * rng and H[i] <= zhi)
                    if opp and held:
                        edge = L[i] if v1dir == 1 else H[i]; how = "abs@LVN"
            if not np.isnan(edge):
                s1 = stopfor(v1dir, edge, C[i])
                if not np.isnan(s1):
                    dist = abs(C[i] - s1)
                    ok = not (p["houseMoney"] and lastWinDir == v1dir and dist * PV > dayPnl)
                    if ok and not np.isnan(target(v1dir, C[i], dist)):
                        pending = (v1dir, dist, how, v1Ext, target(v1dir, C[i], dist)); v1dir = 0
    return pd.DataFrame(trades)


def stats(t, label):
    if t.empty:
        return f"{label}: no trades"
    days = t.session.nunique(); w = t[t.pnl > 0]
    gp, gl = w.pnl.sum(), -t[t.pnl <= 0].pnl.sum()
    tt = t.R.mean() / (t.R.std(ddof=1) / np.sqrt(len(t))) if len(t) > 1 else np.nan
    eq = t.pnl.cumsum(); dd = (eq - eq.cummax()).min()
    return (f"{label}: {len(t)} trades on {days} days | win {len(w)/len(t):.0%} | avg R {t.R.mean():+.2f} (t={tt:.2f}) | "
            f"net ${t.pnl.sum():,.0f} | PF {gp/gl if gl else np.inf:.2f} | maxDD ${dd:,.0f} | exits {t.why.value_counts().to_dict()}")


if __name__ == "__main__":
    months = sys.argv[1].split(",") if len(sys.argv) > 1 else ["2025-06", "2025-07", "2025-08"]
    d = load(months)
    parts = build(d)
    for entry in ["close", "fast"]:
        for be in [True, False]:
            t = run(d, *parts, entry=entry, useBE=be)
            print(stats(t, f"V1 entry={entry:5s} BE={'his words' if be else 'off      '}"))
            t.to_csv(f"scalping_data/v1_{entry}_{'be' if be else 'nobe'}_{months[0]}_{months[-1]}.csv", index=False)
