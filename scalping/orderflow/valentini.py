"""VALENTINI suite (V1 trend NY, V2 mean reversion London, V4 squeeze NY) on Databento NQ trades.

Port of research/existing/pine/VALENTINI-suite.pine v5. Every change vs Pine is listed in
research/existing/audits/VALENTINI_audit_2026-11-15b_tradedata.md (A5-A7, B6-B7, K1-K2). Summary:
- Triggers use REAL single aggressor orders (same exchange timestamp + same side = one order):
  V1/V4 >= 30 lots (NY), V2 >= 20 lots (London) [A5].
- V1/V2 aggression = big order in direction inside the LVN zone + follow-through close (B2); V1 absorption (B1) =
  big order against the direction inside the zone while the bar holds.
- V4 protection level = >= 2 big orders of the trapped side inside the compression box while price holds [A7].
  V4 stop: "box" (approved proxy) or "big" (beyond the farthest absorbed trapped-side order) [A6].
- Stops 2 ticks inside an obvious prev-day/prev-week high/low when it lies between entry and stop [B6].
- Breakeven: V1 full-body close beyond the leg extreme (live rule, K1); V2 "additional breakout" (Pine);
  V4 full-body close or first failed test, exit after 3 failed tests (Pine A2).
- Entries on the bar close -> fill at the next bar's first trade + 1 tick; V4 first attempt = stop order, fills at the first
  trade through the level + 1 tick [B7]. Stops fill at the triggering trade -/+ 1 tick; targets only when traded through.
- 1 NQ contract per signal; account view re-sizes in MNQ to 0.25% risk (see account()).
Fidelity grade: Proxy (noise filter, active management, day-type sizing not in yet).
"""
import sys
import numpy as np, pandas as pd
from valentini_v1 import load, build as build1, va, TICK, PV, RS   # same data prep

P = dict(BIG_NY=30, BIG_LDN=20, pvLen=5, legLookback=30, edgePct=0.2, maxLegBars=240, zoneTol=4 * TICK, ftPct=0.6,
         absHold=0.5, useAbs=True, useCvdBreak=True, useDevVA=True, stopOff=2 * TICK, minStop=40 * TICK, maxStop=40.0,
         minRR=2.5, fallbackRR=5.0, v2MinRR=1.0, maxLosses=3, houseMoney=True, fullBody=0.5, boxLen=20, compK=0.6,
         rejMax=3, v4MinTrapped=2, v4stop="box", v4be="live", v3dir="long", v3Win=5, v3MinAbs=2, v3prof="session", v3gate="bias", insideLvl=True, cost=4.50, slip=TICK, models=("V1", "V2", "V4"))
NY0, NY1, FLAT = 9 * 60 + 45, 13 * 60 + 30, 15 * 60 + 55
LDN0, LDN1, V2FLAT = 3 * 60, 9 * 60 + 10, 9 * 60 + 25


def build(d):
    bars, rows, rstart, _, tstart = build1(d)
    ts = d.ts.values; px = d.price.values; sd = d.side.values.astype(np.int64); sz = d["size"].values.astype(np.int64)
    bar = np.repeat(np.arange(len(tstart) - 1), np.diff(tstart))
    ok = np.r_[True, (ts[1:] != ts[:-1]) | (sd[1:] != sd[:-1])]
    gs = np.flatnonzero(ok); ge = np.r_[gs[1:], len(ts)]
    osz = np.add.reduceat(sz, gs); m = osz >= min(P["BIG_NY"], P["BIG_LDN"])
    big = pd.DataFrame({"ts": ts[gs[m]], "side": sd[gs[m]], "size": osz[m], "px": px[ge[m] - 1], "bar": bar[gs[m]]})
    return bars, rows, rstart, big, tstart


from collections import Counter
FUN = Counter()


def run(d, bars, rows, rstart, big, tstart, **over):
    p = dict(P, **over)
    tsA, pxA = d.ts.values, d.price.values
    H, L, C, O = bars.h.values, bars.l.values, bars.c.values, bars.o.values
    DEL, MOD, SES = bars.delta.values, bars["mod"].values, bars.session.astype(str).values
    MIN = bars.minute.values
    WEEK = pd.to_datetime(bars.session).dt.isocalendar().week.values.astype(int)
    rk, rv = rows.key.values, rows.v.values
    bigbar = {}
    for r in big.itertuples():
        bigbar.setdefault(r.bar, []).append(r)
    n, pv, M = len(bars), p["pvLen"], set(p["models"])
    out = []
    # rolling box helpers for V4
    hh = pd.Series(H).rolling(p["boxLen"]).max().values; ll = pd.Series(L).rolling(p["boxLen"]).min().values
    avgBox = pd.Series(hh - ll).rolling(390, min_periods=60).mean().values
    S = dict(pPOC=np.nan, pVAH=np.nan, pVAL=np.nan, pHigh=np.nan, pLow=np.nan, olderPOC=[], dayProf={}, onProf={},
             dHi=np.nan, dLo=np.nan, onHigh=np.nan, onLow=np.nan, onPOC=np.nan, dayBias=0, cvd=0.0, dayStart=0,
             dayLosses=0, lastWinDir=0, dayPnl=0.0, firstUsedL=False, firstUsedS=False, dayFull=False,
             wHi=np.nan, wLo=np.nan, pwHigh=np.nan, pwLow=np.nan)
    cvdHist = np.zeros(n)
    lastPL = lastPH = np.nan; lastPLbar = lastPHbar = -1
    v1 = dict(dir=0, start=np.nan, ext=np.nan, lvn=np.nan, startBar=-1, usedPL=-1, usedPH=-1)
    v2 = dict(dir=0, swing=np.nan, swingBar=-1, ext=np.nan, lvn=np.nan)
    v4 = dict(dir=0, hi=np.nan, lo=np.nan, bar=-1, att=0, trapLvl=np.nan)
    v3 = dict(done=False, att=0, prof={}, cvd=0.0)                     # VALENTINI-3 AAA (rule sheet, Kian 2026-11-16)
    pos = None; pending = None; stopOrder = None; pendingExit = False

    def bigs(i, lo, hi, side=None, minsize=30):
        return [b for b in bigbar.get(i, []) if lo <= b.px <= hi and b.size >= minsize and (side is None or b.side == side)]

    def legscan(b0, b1, lo, hi):
        k0, k1 = int(np.floor(lo / RS)), int(np.floor(hi / RS)); span = k1 - k0 + 1
        if span < 5: return np.nan
        s, e = rstart[max(b0, 0)], rstart[b1 + 1]
        kk, vv = rk[s:e], rv[s:e]; m = (kk >= k0) & (kk <= k1)
        vols = np.bincount(kk[m] - k0, weights=vv[m], minlength=span)
        a = int(np.floor(span * p["edgePct"])); z = span - 1 - a
        if z <= a: return np.nan
        return (k0 + a + int(np.argmin(vols[a:z + 1]))) * RS

    def target(dd, entry, dist):
        best = np.nan
        for c in [S["pHigh"] if dd == 1 else S["pLow"], S["pPOC"], S["onPOC"]] + S["olderPOC"]:
            if np.isnan(c): continue
            if (c > entry + TICK if dd == 1 else c < entry - TICK) and (np.isnan(best) or abs(c - entry) < abs(best - entry)):
                best = c
        if np.isnan(best): return entry + dd * p["fallbackRR"] * dist
        return best if abs(best - entry) / dist >= p["minRR"] else np.nan

    def stopfor(dd, level, entry):
        s = level - p["stopOff"] if dd == 1 else level + p["stopOff"]
        if p["insideLvl"]:                                            # B6: 2 ticks inside an obvious high/low
            lv = [x for x in (S["pHigh"], S["pLow"], S["pwHigh"], S["pwLow"]) if not np.isnan(x)]
            inside = [x for x in lv if (s < x < entry if dd == 1 else entry < x < s)]
            if inside:
                x = min(inside) if dd == 1 else max(inside)          # the level nearest the stop
                s2 = x + 2 * TICK if dd == 1 else x - 2 * TICK
                if abs(entry - s2) >= p["minStop"]: s = s2
        s = min(s, entry - p["minStop"]) if dd == 1 else max(s, entry + p["minStop"])
        return np.nan if abs(entry - s) > p["maxStop"] else s

    def close_pos(i, px, why, tsx):
        nonlocal pos
        pnl = (px - pos["fill"]) * pos["d"] * PV - p["cost"]
        S["dayPnl"] += pnl
        if pnl < 0: S["dayLosses"] += 1
        else: S["lastWinDir"] = pos["d"]
        if pos["m"] == "V4" and v4["dir"] != 0: v4["att"] += 1
        if pos["m"] == "V3": v3["att"] += 1
        out.append(dict(model=pos["m"], session=SES[i], entry_ts=pos["ts"], exit_ts=tsx, d=pos["d"], how=pos["how"],
                        fill=pos["fill"], stop0=pos["stop0"], tgt=pos["tgt"], exit=px, why=why, dist=pos["dist"],
                        R=(px - pos["fill"]) * pos["d"] / pos["dist"], pnl=pnl, be=pos["be"]))
        pos = None

    def open_pos(i, k, fill, sig):
        nonlocal pos
        dd, dist = sig["d"], sig["dist"]
        stp = fill - dd * dist                                         # Pine posInit: re-anchor to the fill
        if sig["m"] == "V3":                                           # E1: session VAH frozen at signal
            t = sig["tgt"] if (sig["tgt"] - fill) * dd > 0 else fill + dd * p["minRR"] * dist
        elif sig["m"] == "V2":
            t = S["pPOC"] if (not np.isnan(S["pPOC"]) and (S["pPOC"] - fill) * dd > 0) else fill + dd * dist
        else:
            t = target(dd, fill, dist)
            if np.isnan(t): t = sig["tgt"]
        pos = dict(m=sig["m"], d=dd, fill=fill, ts=tsA[k], dist=dist, stp=stp, stop0=stp, tgt=t, how=sig["how"],
                   ext=sig.get("ext", np.nan), be=False, postExt=np.nan, rej=0, k=k, bar=i)

    def walk(i, k0):
        if pos is None: return
        for k in range(k0, tstart[i + 1]):
            x = pxA[k]; dd = pos["d"]
            if (x <= pos["stp"]) if dd == 1 else (x >= pos["stp"]):
                close_pos(i, x - dd * p["slip"], "be" if pos["be"] else "stop", tsA[k]); return
            if (x >= pos["tgt"] + TICK) if dd == 1 else (x <= pos["tgt"] - TICK):
                close_pos(i, pos["tgt"], "target", tsA[k]); return

    canTrade = True
    for i in range(n):
        newDay = i == 0 or SES[i] != SES[i - 1]
        if newDay:
            if S["dayFull"] and S["dayProf"]:
                pp, ph_, pl_ = va(S["dayProf"])
                if not np.isnan(S["pPOC"]): S["olderPOC"] = ([S["pPOC"]] + S["olderPOC"])[:10]
                S.update(pPOC=pp, pVAH=ph_, pVAL=pl_, pHigh=S["dHi"], pLow=S["dLo"])
            if i > 0 and WEEK[i] != WEEK[i - 1]:
                S.update(pwHigh=S["wHi"], pwLow=S["wLo"], wHi=np.nan, wLo=np.nan)
            S.update(dayFull=True, dayProf={}, onProf={}, dHi=np.nan, dLo=np.nan, onHigh=np.nan, onLow=np.nan, onPOC=np.nan,
                     dayBias=0, cvd=0.0, dayStart=i, dayLosses=0, lastWinDir=0, dayPnl=0.0, firstUsedL=False, firstUsedS=False)
            v3.update(done=False, att=0, prof={}, cvd=0.0)
        mod = MOD[i]
        inON = mod >= 18 * 60 or mod < 9 * 60 + 30
        inNY = NY0 <= mod < NY1
        inLdn = LDN0 <= mod < LDN1
        afterOpen = 9 * 60 + 30 <= mod < 17 * 60
        k0 = tstart[i]

        # ================= intrabar =================
        if pos is not None and (pendingExit or (mod >= FLAT and mod < 17 * 60) or (pos["m"] == "V2" and V2FLAT <= mod < 9 * 60 + 30)):
            close_pos(i, pxA[k0] - pos["d"] * p["slip"], "fail" if pendingExit else "eod", tsA[k0])
        pendingExit = False
        if pending is not None and pos is None:
            open_pos(i, k0, pxA[k0] + pending["d"] * p["slip"], pending); stopOrder = None
            walk(i, k0 + 1)
        elif pos is not None:
            walk(i, k0)
        elif stopOrder is not None:
            so = stopOrder; dd = so["d"]
            for k in range(k0, tstart[i + 1]):
                if (pxA[k] >= so["lvl"]) if dd == 1 else (pxA[k] <= so["lvl"]):
                    open_pos(i, k, pxA[k] + dd * p["slip"], so); walk(i, k + 1); break
        pending = None; stopOrder = None

        # ================= bar close: day state =================
        S["dHi"] = H[i] if np.isnan(S["dHi"]) else max(S["dHi"], H[i]); S["dLo"] = L[i] if np.isnan(S["dLo"]) else min(S["dLo"], L[i])
        S["wHi"] = H[i] if np.isnan(S["wHi"]) else max(S["wHi"], H[i]); S["wLo"] = L[i] if np.isnan(S["wLo"]) else min(S["wLo"], L[i])
        s0, e0 = rstart[i], rstart[i + 1]
        dp, op_ = S["dayProf"], S["onProf"]
        for kk, vv in zip(rk[s0:e0], rv[s0:e0]):
            dp[kk] = dp.get(kk, 0) + vv
            if inON: op_[kk] = op_.get(kk, 0) + vv
        if inON:
            S["onHigh"] = H[i] if np.isnan(S["onHigh"]) else max(S["onHigh"], H[i])
            S["onLow"] = L[i] if np.isnan(S["onLow"]) else min(S["onLow"], L[i])
        if not inON and i > 0 and not newDay and (MOD[i - 1] >= 18 * 60 or MOD[i - 1] < 9 * 60 + 30) and op_:
            S["onPOC"] = va(op_)[0]
        S["cvd"] += DEL[i]; cvdHist[i] = S["cvd"]
        if 9 * 60 + 30 <= mod < 16 * 60:                               # V3 B1 NY-session profile + A7 NY CVD
            v3["cvd"] += DEL[i]
            for kk, vv in zip(rk[s0:e0], rv[s0:e0]): v3["prof"][kk] = v3["prof"].get(kk, 0) + vv
        if afterOpen and not inON and not np.isnan(S["onHigh"]):
            if C[i] > S["onHigh"]: S["dayBias"] = 1
            elif C[i] < S["onLow"]: S["dayBias"] = -1
        bias = S["dayBias"]
        canTrade = S["dayLosses"] < p["maxLosses"]
        c = i - pv
        if c - pv >= 0:
            if L[c] < L[c - pv:c].min() and L[c] <= L[c + 1:i + 1].min(): lastPL, lastPLbar = L[c], c
            if H[c] > H[c - pv:c].max() and H[c] >= H[c + 1:i + 1].max(): lastPH, lastPHbar = H[c], c
        lb0 = max(0, i - p["legLookback"] + 1)
        lowestL, lowestBar = L[lb0:i + 1].min(), lb0 + int(np.argmin(L[lb0:i + 1]))
        highestH, highestBar = H[lb0:i + 1].max(), lb0 + int(np.argmax(H[lb0:i + 1]))
        cvdPH = cvdHist[lastPHbar] if lastPHbar >= S["dayStart"] and 0 < i - lastPHbar < 450 else np.nan
        cvdPL = cvdHist[lastPLbar] if lastPLbar >= S["dayStart"] and 0 < i - lastPLbar < 450 else np.nan
        cvdOkL = not p["useCvdBreak"] or np.isnan(cvdPH) or S["cvd"] > cvdPH
        cvdOkS = not p["useCvdBreak"] or np.isnan(cvdPL) or S["cvd"] < cvdPL
        rng = H[i] - L[i]; bodyOk = rng > 0 and abs(C[i] - O[i]) >= p["fullBody"] * rng

        # ================= management at bar close =================
        if pos is not None:
            dd, fill = pos["d"], pos["fill"]
            prevExt = pos["postExt"]
            if np.isnan(pos["postExt"]):
                pos["postExt"] = H[i] if dd == 1 else L[i]
            elif pos["m"] == "V4":
                if dd == 1:
                    if H[i] > pos["postExt"]: pos["postExt"] = H[i]
                    elif H[i] >= pos["postExt"] - 2 * TICK and C[i] < H[i] - 0.5 * rng: pos["rej"] += 1
                else:
                    if L[i] < pos["postExt"]: pos["postExt"] = L[i]
                    elif L[i] <= pos["postExt"] + 2 * TICK and C[i] > L[i] + 0.5 * rng: pos["rej"] += 1
            if not pos["be"]:
                fav = C[i] > fill + TICK if dd == 1 else C[i] < fill - TICK
                dirBar = C[i] > O[i] if dd == 1 else C[i] < O[i]
                if pos["m"] == "V4" and p["v4be"] == "pine":
                    hit = fav and ((bodyOk and dirBar) or pos["rej"] >= 1)
                elif pos["m"] == "V4":                                   # A8 live rule: a LATER candle accepting the high
                    acc = not np.isnan(prevExt) and bodyOk and dirBar and ((C[i] > prevExt) if dd == 1 else (C[i] < prevExt))
                    hit = fav and (acc or pos["rej"] >= 1)
                elif pos["m"] == "V3":                                   # D3 risk-free at the first close in profit
                    hit = fav
                elif pos["m"] == "V1":                                   # K1 live rule
                    hit = fav and bodyOk and dirBar and ((C[i] > pos["ext"]) if dd == 1 else (C[i] < pos["ext"]))
                else:                                                    # V2 "additional breakout"
                    hit = fav and ((H[i] > pos["ext"]) if dd == 1 else (L[i] < pos["ext"]))
                if hit:
                    pos["stp"] = max(pos["stp"], fill + TICK) if dd == 1 else min(pos["stp"], fill - TICK); pos["be"] = True
            elif pos["m"] == "V3":                                       # D4 trail behind each new wave in direction
                dirBar = C[i] > O[i] if dd == 1 else C[i] < O[i]
                if dirBar and any(b.side == dd and b.size >= p["BIG_NY"] for b in bigbar.get(i, [])):
                    ns = L[i] - 2 * TICK if dd == 1 else H[i] + 2 * TICK
                    pos["stp"] = max(pos["stp"], ns) if dd == 1 else min(pos["stp"], ns)
            if pos["m"] == "V4" and pos["rej"] >= p["rejMax"]:
                pendingExit = True
        if pos is not None and (i + 1 == n or SES[i + 1] != SES[i] or (MIN[i + 1] - MIN[i]) > np.timedelta64(60, "m")):
            close_pos(i, C[i] - pos["d"] * p["slip"], "eod", tsA[tstart[i + 1] - 1]); pendingExit = False

        # ================= V1 leg state (Pine 448-501) =================
        v1New = False
        if "V1" in M:
            devVAH = devVAL = np.nan
            if p["useDevVA"] and inNY and v1["dir"] == 0 and bias != 0 and dp:
                _, devVAH, devVAL = va(dp)
            if v1["dir"] == 0 and inNY and canTrade:
                outL = not p["useDevVA"] or (not np.isnan(devVAH) and C[i] > devVAH)
                outS = not p["useDevVA"] or (not np.isnan(devVAL) and C[i] < devVAL)
                if bias == 1 and outL and cvdOkL:
                    if lastPLbar >= 0 and lastPLbar != v1["usedPL"] and i - lastPLbar <= p["maxLegBars"] and lastPL < C[i]:
                        v1.update(dir=1, start=lastPL, startBar=lastPLbar, usedPL=lastPLbar)
                    elif not S["firstUsedL"]:
                        v1.update(dir=1, start=lowestL, startBar=lowestBar); S["firstUsedL"] = True
                    if v1["dir"] == 1: v1["ext"] = H[i]; v1New = True
                elif bias == -1 and outS and cvdOkS:
                    if lastPHbar >= 0 and lastPHbar != v1["usedPH"] and i - lastPHbar <= p["maxLegBars"] and lastPH > C[i]:
                        v1.update(dir=-1, start=lastPH, startBar=lastPHbar, usedPH=lastPHbar)
                    elif not S["firstUsedS"]:
                        v1.update(dir=-1, start=highestH, startBar=highestBar); S["firstUsedS"] = True
                    if v1["dir"] == -1: v1["ext"] = L[i]; v1New = True
            elif v1["dir"] == 1:
                if H[i] > v1["ext"]: v1["ext"] = H[i]; v1New = True
                if L[i] < v1["start"] or i - v1["startBar"] > p["maxLegBars"]: v1["dir"] = 0
            elif v1["dir"] == -1:
                if L[i] < v1["ext"]: v1["ext"] = L[i]; v1New = True
                if H[i] > v1["start"] or i - v1["startBar"] > p["maxLegBars"]: v1["dir"] = 0
            if not inNY or (v1["dir"] != 0 and bias != v1["dir"]): v1["dir"] = 0
            if v1New and v1["dir"] != 0:
                lo, hi = (v1["start"], v1["ext"]) if v1["dir"] == 1 else (v1["ext"], v1["start"])
                v1["lvn"] = legscan(v1["startBar"], i, lo, hi)

        # ================= V2 state (Pine 512-555) =================
        v2New = False
        if "V2" in M:
            if not np.isnan(S["pVAL"]) and inLdn:
                if v2["dir"] == 0 and canTrade:
                    if C[i] < S["pVAL"]: v2.update(dir=1, swing=L[i], swingBar=i)
                    elif C[i] > S["pVAH"]: v2.update(dir=-1, swing=H[i], swingBar=i)
                elif v2["dir"] == 1:
                    if L[i] < v2["swing"]: v2.update(swing=L[i], swingBar=i)
                    if C[i] > S["pVAL"]: v2.update(dir=2, ext=H[i]); v2New = True
                elif v2["dir"] == -1:
                    if H[i] > v2["swing"]: v2.update(swing=H[i], swingBar=i)
                    if C[i] < S["pVAH"]: v2.update(dir=-2, ext=L[i]); v2New = True
                elif v2["dir"] == 2:
                    if H[i] > v2["ext"]: v2["ext"] = H[i]; v2New = True
                    if L[i] < v2["swing"]: v2["dir"] = 0
                elif v2["dir"] == -2:
                    if L[i] < v2["ext"]: v2["ext"] = L[i]; v2New = True
                    if H[i] > v2["swing"]: v2["dir"] = 0
            if not inLdn: v2["dir"] = 0
            if v2New and abs(v2["dir"]) == 2:
                lo, hi = (v2["swing"], v2["ext"]) if v2["dir"] > 0 else (v2["ext"], v2["swing"])
                v2["lvn"] = legscan(v2["swingBar"], i, lo, hi)

        # ================= V4 box state (Pine 560-585, protection level A7) =================
        if "V4" in M and i >= p["boxLen"]:
            bHi, bLo = hh[i - 1], ll[i - 1]
            if v4["dir"] == 0 and pos is None and inNY and canTrade and bias != 0 and not np.isnan(avgBox[i]) \
                    and bHi - bLo <= p["compK"] * avgBox[i]:
                mid = (bHi + bLo) / 2
                trapped = [b for j in range(i - p["boxLen"], i) for b in bigbar.get(j, [])
                           if b.side == -bias and b.size >= p["BIG_NY"] and bLo <= b.px <= bHi]
                holds = (mid <= C[i] <= bHi) if bias == 1 else (bLo <= C[i] <= mid)
                if len(trapped) >= p["v4MinTrapped"] and holds:
                    tl = min(b.px for b in trapped) if bias == 1 else max(b.px for b in trapped)
                    v4.update(dir=bias, hi=bHi, lo=bLo, bar=i, att=0, trapLvl=tl)
            elif v4["dir"] != 0:
                dd = v4["dir"]
                if (dd == 1 and C[i] < v4["lo"]) or (dd == -1 and C[i] > v4["hi"]) or i - v4["bar"] > p["maxLegBars"] \
                        or not inNY or v4["att"] >= 2 or bias != dd:
                    v4["dir"] = 0

        # ================= entries (one position at a time; priority V1, V2, V4 as in Pine) =================
        if pos is None and canTrade:
            sig = None
            if "V1" in M and v1["dir"] != 0 and not v1New and inNY and not np.isnan(v1["lvn"]):
                dd = v1["dir"]; zlo, zhi = v1["lvn"] - p["zoneTol"], v1["lvn"] + RS + p["zoneTol"]
                if rng > 0 and L[i] <= zhi and H[i] >= zlo:
                    ft = (C[i] > O[i] and C[i] - L[i] >= p["ftPct"] * rng) if dd == 1 else (C[i] < O[i] and H[i] - C[i] >= p["ftPct"] * rng)
                    agg = bigs(i, zlo, zhi, dd, p["BIG_NY"]); edge = np.nan; how = ""
                    if agg and ft:
                        edge = min(b.px for b in agg) if dd == 1 else max(b.px for b in agg); how = "agg@LVN"
                    elif p["useAbs"] and bigs(i, zlo, zhi, -dd, p["BIG_NY"]):
                        held = (C[i] - L[i] >= p["absHold"] * rng and L[i] >= zlo) if dd == 1 else (H[i] - C[i] >= p["absHold"] * rng and H[i] <= zhi)
                        if held: edge = L[i] if dd == 1 else H[i]; how = "abs@LVN"
                    if not np.isnan(edge):
                        s1 = stopfor(dd, edge, C[i])
                        if not np.isnan(s1):
                            dist = abs(C[i] - s1); t1 = target(dd, C[i], dist)
                            if not np.isnan(t1): sig = dict(m="V1", d=dd, dist=dist, tgt=t1, how=how, ext=v1["ext"])
                if sig: v1["dir"] = 0
            if sig is None and "V2" in M and abs(v2["dir"]) == 2 and not v2New and inLdn and not np.isnan(v2["lvn"]):
                dd = 1 if v2["dir"] > 0 else -1; zlo, zhi = v2["lvn"] - p["zoneTol"], v2["lvn"] + RS + p["zoneTol"]
                if rng > 0 and L[i] <= zhi and H[i] >= zlo:
                    ft = (C[i] > O[i] and C[i] - L[i] >= p["ftPct"] * rng) if dd == 1 else (C[i] < O[i] and H[i] - C[i] >= p["ftPct"] * rng)
                    agg = bigs(i, zlo, zhi, dd, p["BIG_LDN"])
                    if agg and ft:
                        edge = min(b.px for b in agg) if dd == 1 else max(b.px for b in agg)
                        s2 = stopfor(dd, edge, C[i])
                        pocOk = not np.isnan(S["pPOC"]) and (S["pPOC"] - C[i]) * dd > 0
                        if not np.isnan(s2) and pocOk:
                            dist = abs(C[i] - s2)
                            if abs(S["pPOC"] - C[i]) / dist >= p["v2MinRR"]:
                                sig = dict(m="V2", d=dd, dist=dist, tgt=S["pPOC"], how="agg@LVN", ext=v2["ext"]); v2["dir"] = 0
            if sig is not None:
                if p["houseMoney"] and S["lastWinDir"] == sig["d"] and sig["dist"] * PV > S["dayPnl"]:
                    sig = None
                else:
                    pending = sig
            if "V3" in M and 10 * 60 <= mod < NY1: FUN["1 in 10:00-13:30, flat"] += 1
            if pending is None and "V3" in M and not v3["done"] and v3["att"] < 2 and 10 * 60 <= mod < NY1 and v3["prof"]:
                dd = bias if p["v3gate"] == "bias" else int(np.sign(v3["cvd"]))   # A4: bias break, or NY CVD direction
                if dd != 0 and (p["v3dir"] == "both" or dd == 1): FUN["2 day bias ok"] += 1
                if dd != 0 and (p["v3dir"] == "both" or dd == 1) and v3["cvd"] * dd > 0: FUN["3 NY CVD ok"] += 1
                if dd != 0 and (p["v3dir"] == "both" or dd == 1) and v3["cvd"] * dd > 0:      # A4/A5 day + A7 CVD
                    if p["v3prof"] == "expansion":                         # B2: profile of the expansion move A->now
                        o0 = S["dayStart"] + int(np.argmax((MOD[S["dayStart"]:i + 1] >= 9 * 60 + 30)))
                        a = o0 + (int(np.argmin(L[o0:i + 1])) if dd == 1 else int(np.argmax(H[o0:i + 1])))
                        kk, vv = rk[rstart[a]:rstart[i + 1]], rv[rstart[a]:rstart[i + 1]]
                        u, inv = np.unique(kk, return_inverse=True); vol = np.bincount(inv, weights=vv)
                        _, vah, val = va(dict(zip(u.tolist(), vol.tolist())))
                    else:                                                  # B1: NY session profile from 09:30
                        _, vah, val = va(v3["prof"])
                    zlo, zhi = (val - p["zoneTol"], val + RS + p["zoneTol"]) if dd == 1 else (vah - RS - p["zoneTol"], vah + p["zoneTol"])
                    w0 = max(i - p["v3Win"] + 1, S["dayStart"])
                    if (C[i] < zlo) if dd == 1 else (C[i] > zhi):
                        v3["done"] = True                                                     # C1/B5 level breached
                    else:
                        FUN["4 not breached"] += 1
                        if (L[i] <= zhi) if dd == 1 else (H[i] >= zlo): FUN["4b bar touches zone"] += 1
                        nabs = sum(1 for j in range(w0, i + 1) for b in bigbar.get(j, [])
                                   if b.side == -dd and b.size >= p["BIG_NY"] and zlo <= b.px <= zhi)  # C2
                        held = (C[w0:i + 1].min() >= zlo) if dd == 1 else (C[w0:i + 1].max() <= zhi)
                        if nabs >= 1: FUN["5a >=1 absorbed order"] += 1
                        if nabs >= p["v3MinAbs"]: FUN["5b >=2 absorbed orders"] += 1
                        if nabs >= p["v3MinAbs"] and held:
                            FUN["6 held"] += 1
                            lvl = H[i] + TICK if dd == 1 else L[i] - TICK                        # C4 buy stop
                            prot = L[w0:i + 1].min() if dd == 1 else H[w0:i + 1].max()          # D1
                            s3 = stopfor(dd, prot, lvl); tgt3 = vah if dd == 1 else val
                            if not np.isnan(s3):
                                FUN["7 stop <= 40 pts"] += 1
                                dist = abs(lvl - s3)
                                if (tgt3 - lvl) * dd / dist >= p["minRR"]: FUN["8 RR >= 2.5"] += 1
                                if (tgt3 - lvl) * dd / dist >= p["minRR"] and not (
                                        p["houseMoney"] and S["lastWinDir"] == dd and dist * PV > S["dayPnl"]):
                                    stopOrder = dict(m="V3", d=dd, lvl=lvl, dist=dist, tgt=tgt3, how="absorb@VAL" if dd == 1 else "absorb@VAH")
            if pending is None and stopOrder is None and "V4" in M and v4["dir"] != 0 and inNY:
                dd = v4["dir"]; lvl = v4["hi"] + TICK if dd == 1 else v4["lo"] - TICK
                prot = (v4["lo"] if dd == 1 else v4["hi"]) if p["v4stop"] == "box" else v4["trapLvl"]
                hm = lambda dist: p["houseMoney"] and S["lastWinDir"] == dd and dist * PV > S["dayPnl"]
                if v4["att"] == 0:
                    s4 = stopfor(dd, prot, lvl)
                    if not np.isnan(s4):
                        dist = abs(lvl - s4); t4 = target(dd, lvl, dist)
                        if not np.isnan(t4) and not hm(dist):
                            stopOrder = dict(m="V4", d=dd, lvl=lvl, dist=dist, tgt=t4, how=f"stop ({p['v4stop']})")
                elif (C[i] > v4["hi"] and C[i] > O[i] and bodyOk) if dd == 1 else (C[i] < v4["lo"] and C[i] < O[i] and bodyOk):
                    s4 = stopfor(dd, prot, C[i])
                    if not np.isnan(s4):
                        dist = abs(C[i] - s4); t4 = target(dd, C[i], dist)
                        if not np.isnan(t4) and not hm(dist):
                            pending = dict(m="V4", d=dd, dist=dist, tgt=t4, how=f"body close ({p['v4stop']})")
    return pd.DataFrame(out)


def stats(t, label):
    if t.empty: return f"{label:28s}: no trades"
    w = t[t.pnl > 0]; gp, gl = w.pnl.sum(), -t[t.pnl <= 0].pnl.sum()
    tt = t.R.mean() / (t.R.std(ddof=1) / np.sqrt(len(t))) if len(t) > 2 else np.nan
    eq = t.pnl.cumsum(); dd = (eq - eq.cummax()).min()
    nb = (t.why == "be").sum(); nl = len(t) - len(w) - nb
    return (f"{label:28s}: {len(t):3d} trades (W {len(w)}/BE {nb}/L {nl}) | avg R {t.R.mean():+.2f} (t={tt:+.2f}) | "
            f"net ${t.pnl.sum():+8,.0f} | PF {gp/gl if gl else np.inf:4.2f} | maxDD ${dd:,.0f} | {t.why.value_counts().to_dict()}")


def account(t, eq0=100_000.0, risk=0.0025, cap=0.02, mnq_cost=1.50):
    """MNQ sizing to `risk` of equity per trade ($2/pt), 2% daily loss cap. Returns (return %, maxDD %)."""
    eq = eq0; peak = eq0; mdd = 0.0; day = None; dstart = eq0
    for r in t.sort_values("entry_ts").itertuples():
        if r.session != day: day, dstart = r.session, eq
        if eq - dstart <= -cap * dstart: continue
        q = int(risk * eq // (r.dist * 2.0))
        if q < 1: continue
        eq += q * ((r.exit - r.fill) * r.d * 2.0 - mnq_cost)
        peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    return (eq / eq0 - 1) * 100, mdd * 100


if __name__ == "__main__":
    months = sys.argv[1].split(",") if len(sys.argv) > 1 else ["2025-06", "2025-07", "2025-08"]
    d = load(months); parts = build(d)
    res = {}
    for name, kw in [("V1 alone", dict(models=("V1",))), ("V2 alone", dict(models=("V2",))),
                     ("V4 alone (box stop)", dict(models=("V4",), v4stop="box")),
                     ("V4 alone (big-order stop)", dict(models=("V4",), v4stop="big")),
                     ("V4 alone (Pine BE)", dict(models=("V4",), v4stop="box", v4be="pine")),
                     ("ALL (V4 box stop)", dict(v4stop="box")), ("ALL (V4 big-order stop)", dict(v4stop="big")),
                     ("V3 alone (long only)", dict(models=("V3",))),
                     ("V3 alone (long + short)", dict(models=("V3",), v3dir="both")),
                     ("V3 B2 expansion (long)", dict(models=("V3",), v3prof="expansion")),
                     ("V3 B2 expansion (long+short)", dict(models=("V3",), v3prof="expansion", v3dir="both")),
                     ("ALL4 V1 V2 V3 V4", dict(models=("V1", "V2", "V3", "V4"))),
                     ("ALL4 with V3 B2 long+short", dict(models=("V1", "V2", "V3", "V4"), v3prof="expansion", v3dir="both"))]:
        t = run(d, *parts, **kw); res[name] = t
        r, mdd = account(t) if not t.empty else (0, 0)
        print(stats(t, name), f"| $100k@0.25%: {r:+.1f}% (DD {mdd:.1f}%)")
        tag = name.replace(" ", "_").replace("(", "").replace(")", "")
        t.to_csv(f"scalping_data/val_{tag}_{months[0]}_{months[-1]}.csv", index=False)
    for name in ["ALL4 with V3 B2 long+short"]:
        t = res[name]
        for m in ["V1", "V2", "V3", "V4"]:
            print("   ", stats(t[t.model == m], f"{name} -> {m}"))
