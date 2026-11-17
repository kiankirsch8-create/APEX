# APEX NQ demo: setup guide (Databento live + IBKR paper)

The demo mirrors the backtest code on live NQ data. Every ~10 s (Valentini) or every minute (others) the unchanged backtest
functions run on the live tape. Whatever position they hold, the engine holds too, in MNQ, on an IBKR **paper** account.
It refuses to run on a live (non-paper) account.

## What you need to set up (one time)
1. **Databento**
   - Add a payment method and sign the **CME live data license** in the portal (Live data → CME Globex).
   - Check the plan price on their pricing page before subscribing.
   - Create an API key and set it as the environment variable `DATABENTO_API_KEY` on the VPS. Never put it in code.
   - First start backfills ~25 trading days of NQ trades (warm-up). The log prints the Databento cost estimate first,
     usually a few dollars.
2. **Interactive Brokers**
   - Open an IBKR account (the paper account comes with it), then log in to Client Portal → Settings → Paper Trading Account.
   - Set the paper balance to $100,000 so results compare with the backtests.
   - Market data: subscribe to CME real-time data on the live account and share it with the paper account (Settings →
     Paper Trading → share market data). Without it, paper fills use delayed prices.
   - Install **IB Gateway** (stable) on the VPS. Log in with the **paper** username, API port **4002**, "Read-only API" OFF,
     trusted IP 127.0.0.1. Turn on auto-restart.
3. **VPS**
   - `pip install -r scalping/live/requirements.txt`, then `python -m scalping.live.run --check`. That connects, backfills
     and prints each strategy's state without placing orders.
   - Then `python -m scalping.live.run` (run it as a service so it restarts after reboots).

## What it trades (risk tiers: fidelity + evidence, max 1.5% — see config.py)
| Strategy | Risk/trade | Why this tier |
|---|---|---|
| VALENTINI-1 (all his rules) + G4 | 0.50% | Partial copy, best tested portfolio (tick-level) |
| VALENTINI-4 (range bars) + G4 | 0.50% | same |
| WILLIAMS-1 down close + higher open | 0.50% | Partial, +0.27R, small sample |
| WILLIAMS-1 blastoff | 0.50% | Partial, +0.18R, small sample |
| TRULLAS-1 fast swings 61.8% | 0.25% | Partial, edge ~0 on the tick replay |
| TRULLAS-1 defaults 38.2% | 0.25% | Partial, weak |
| CREAMER-1 continuation | 0.25% | Partial, inconclusive |

Combined daily loss cap 3%. A broker-side disaster stop on the net position protects against an engine crash.

## Files
- Trades: `scalping_data/live/journal/YYYY-MM-DD.csv`. Every open/close shows the model price, the IB fill price and the
  slippage in points, so the demo measures exactly where live differs from the backtest.
- Logs: `scalping_data/live/logs/`. The live tape is saved per session in `scalping_data/live/tape/` (future research data).

## Known limits (v1)
- IBKR nets positions per account, so the engine tracks strategies virtually and sends net orders. Two strategies in
  opposite directions show as a smaller net position at IB.
- Entries chased after a restart only within 3 minutes of the model's entry; older ones are skipped and logged.
- Prop-firm demos (Topstep etc.) are not supported in v1 (Topstep bans VPS execution).
