"""NQ demo configuration: strategies, risk tiers, connections. Secrets come from environment variables only."""
import os

# ---------------------------------------------------------------- connections (never hard-code keys)
DATABENTO_KEY = os.environ.get("DATABENTO_API_KEY", "")
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))         # IB Gateway paper = 4002 (TWS paper = 7497)
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "72"))
IB_SYMBOL = "MNQ"                                          # all demo orders in micros ($2/pt) for fine sizing
STORE = os.environ.get("APEX_LIVE_STORE", "scalping_data/live")
WARMUP_SESSIONS = 25                                       # Williams ATR10, Creamer 20-day percentile, 1h EMA200
VALENTINI_SESSIONS = 12                                    # prior day/week levels, 10 older POCs, rolling averages
RECOMPUTE_SEC = 10                                         # re-run the backtest code on the live tape every N seconds
ACT_WINDOW_SEC = 180                                       # only act on entries newer than this (no stale signals)
DAILY_LOSS_CAP = 0.03                                      # whole demo account, all strategies together
DISASTER_STOP_PTS = 120                                    # broker-side safety stop on the net position (engine crash)

# ---------------------------------------------------------------- risk tiers (Kian 2026-11-17: risk follows certainty, max 1.5%)
# score = fidelity (how 1:1 the copy is: Copyable 3, Partial 2, Proxy 1) + evidence (tick-verified t>=2: 3, t 1-2: 2, else 1)
TIER_RISK = {6: 0.015, 5: 0.010, 4: 0.005}                 # anything lower -> 0.25%
def risk_for(fidelity, evidence):
    return TIER_RISK.get(fidelity + evidence, 0.0025)

# name: family, params, fidelity, evidence (update evidence as demo/backtest data arrives; agents will own this later)
STRATEGIES = {
    "VALENTINI-1 (all his rules) +G4": dict(family="valentini", kw=dict(models=("V1",), g4=True, barKind="range", trig="effort",
                                         biasMode="pf", mgmt="his"), bars="range", fidelity=2, evidence=2),
    "VALENTINI-4 (range) +G4":         dict(family="valentini", kw=dict(models=("V4",), g4=True, barKind="range"), bars="range",
                                         fidelity=2, evidence=2),
    "TRULLAS-1 fast swings 61.8%":     dict(family="trullas1", kw=dict(piv_k=1, tgt_fib=0.618), fidelity=2, evidence=1),
    "TRULLAS-1 defaults 38.2%":        dict(family="trullas1", kw=dict(), fidelity=2, evidence=1),
    "CREAMER-1 continuation":          dict(family="creamer", kw=dict(mode="continuation"), fidelity=2, evidence=1),
    "WILLIAMS-1 down close + higher open": dict(family="williams", kw=dict(study="closeopen_dh"), fidelity=2, evidence=2),
    "WILLIAMS-1 blastoff":             dict(family="williams", kw=dict(study="blastoff"), fidelity=2, evidence=2),
}
# NY-time windows (minutes) in which a strategy can open positions; open positions are always managed
ACTIVE = {"valentini": (9 * 60 + 40, 13 * 60 + 35), "trullas1": (8 * 60 + 55, 10 * 60 + 35), "creamer": (9 * 60 + 25, 11 * 60 + 5),
          "williams": (9 * 60 + 30, 15 * 60 + 59)}
for s in STRATEGIES.values():
    s["risk"] = risk_for(s["fidelity"], s["evidence"])
    s["active"] = ACTIVE[s["family"]]
